"""One live realtime-STT connection and the workers that keep it alive.

Split out of ``app_controller.py`` (issue #48, seam 1). ``AppController`` keeps
the thread lifecycle, the shared capture/device layer and the segmented
pipeline; everything scoped to a *single* streaming connection lives here.

The boundary is deliberate: a ``StreamingSession`` never reads a private
attribute of the controller. It is handed the session's stop event, the two
GUI-facing queues and three callables, and owns the rest — the feed and
utterance queues, the connection handle and the lock serialising it, the
reconnect/outage state, the noise gate, the capture rate and the utterance
accumulator.

**One session per ``start()``.** The reset-per-session dance the controller
used to perform (fresh reconnect event, backoff, outage flag, fatal error,
noise gate, drained queues) is gone: every session builds that state fresh by
construction, and a session object is never reused.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections.abc import Callable

from audio.vad import StreamNoiseGate
from config import (
    STREAMING_COALESCE_HOLD_SECONDS,
    STREAMING_COALESCE_MIN_WORDS,
    STREAMING_MAX_UTTERANCE_SECONDS,
    STREAMING_RECONNECT_BASE_SECONDS,
    STREAMING_RECONNECT_MAX_SECONDS,
    STREAMING_STALL_GRACE_SECONDS,
    STREAMING_STALL_MIN_SPEECH_SECONDS,
    STREAMING_STALL_TIMEOUT_SECONDS,
    STREAMING_TURN_COMMIT_SECONDS,
)
from providers.base import StreamHandle, StreamingTranscriptionProvider
from translation.buffering import SENTENCE_ENDINGS, looks_semantically_complete
from translation.stt import has_min_letters
from utils.logging import log
from utils.settings import load_settings
from utils.user_messages import classify_error, get_user_message

# Connection generations are unique across every session in the process, not
# just within one. The counter exists so a late callback from an already
# replaced connection cannot trigger another reconnect — and a callback from a
# *previous* session must count as stale too. With a per-session counter the
# first connection of the next session would answer to the previous session's
# generation 1, so the counter deliberately lives on the module.
_GENERATIONS = itertools.count(1)

# Streaming providers whose interim transcript may be translated *before* the
# turn ends (issue #26). OpenAI Realtime is the only one that needs it AND the
# only one where it is safe:
#
# - It emits no final at all until the turn ends (``_COMPLETED_EVENT`` carries
#   the final transcript and the utterance-end signal together), so nothing
#   downstream can act mid-turn. Its deltas are append-only per conversation
#   item, so a sentence read out of an interim is already final text.
# - **Deepgram never had the bug.** ``is_final`` (endpointing) is a separate
#   signal from ``speech_final``/``UtteranceEnd``, so finals arrive during a
#   turn, ``_parts`` fills and STREAMING_MAX_UTTERANCE_SECONDS fires as
#   designed. Its interims are also revised hypotheses (``set_interim``
#   replaces rather than appends), so the word-prefix bookkeeping below would
#   not hold.
# - **Gemini Live had the bug and already fixes it in the provider** —
#   ``providers/gemini/realtime.py::_maybe_cut_turn``, measured live at 89 s
#   of speech in one turn. It cuts on the same 12 s cap at a sentence
#   boundary, and has to do so there: a cut anywhere else arrives a second
#   time when the turn finally completes.
_SENTENCE_FLUSH_PROVIDERS = frozenset({"openai_realtime"})


def _cut_complete_sentence(text: str) -> str:
    """The leading run of ``text`` ending on a sentence terminator, or ``""``.

    Cuts at the LAST terminator rather than the first, so several sentences
    that arrived between two checks leave as one translation call.
    """
    stripped = text.rstrip()
    end = max(stripped.rfind(mark) for mark in SENTENCE_ENDINGS)
    if end < 0:
        return ""
    return stripped[: end + 1].strip()


class UtteranceSession:
    """Accumulates final streaming transcripts between utterance-end signals.

    ``add_final``/``set_interim`` are called from the provider's receive
    thread; the rest is read from the streaming-processor thread — all
    access goes through a lock since the two run concurrently.

    Besides the utterance parts, the session publishes a *live text*: the
    accumulated finals plus the newest interim hypothesis, shown on the
    subtitle window while the speaker is still talking (Realtime subtitle
    mode). It carries a revision counter so the processor can clear it
    after emitting a translation — but only if no newer speech arrived in
    the meantime (compare-and-clear), so a pipelined next utterance is
    never blanked. A *settled* flag marks the moment the utterance is
    flushed for translation: the GUI recolors the live line in place
    ("finished") instead of it disappearing and reappearing.

    With ``sentence_flush`` on, the session also cuts finished *sentences* out
    of the running interim and hands them to translation without waiting for
    the turn to end (issue #26). A speaker with no pauses produces one very
    long server-VAD turn, and translating only at its end showed the audience
    nothing for a minute and then a wall of text. Every word that leaves this
    way is recorded, so the turn's final transcript contributes only its
    not-yet-translated tail. Only providers in ``_SENTENCE_FLUSH_PROVIDERS``
    may enable it.
    """

    def __init__(self, *, sentence_flush: bool = False) -> None:
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._first_part_time: float | None = None
        self._interim = ""
        self._live_text = ""
        self._live_settled = False
        self._live_rev = 0
        self._sentence_flush = sentence_flush
        # Words of the CURRENT turn already handed to translation by an
        # intra-turn flush. The words themselves, not a count, so the next
        # text can be checked for actually continuing them.
        self._emitted: list[str] = []

    def _publish_live_locked(self) -> None:
        pieces = [p for p in self._parts if p.strip()]
        if self._interim.strip():
            pieces.append(self._interim)
        self._live_text = " ".join(pieces)
        self._live_settled = False  # new speech: the line is in progress again
        self._live_rev += 1

    def _remainder_locked(self, text: str) -> str:
        """The words of the current turn not yet handed to translation."""
        words = text.split()
        if words[: len(self._emitted)] != self._emitted:
            # Not a continuation of what already went out: the engine opened a
            # new conversation item before the old one completed, or a
            # completed transcript is not the verbatim concatenation of its
            # deltas. Start the turn over rather than cut the wrong prefix off
            # — a duplicated sentence is recoverable, a missing one is a hole
            # in the khutbah.
            log(
                "STREAMING intra-turn flush: text no longer continues the "
                "flushed prefix — translating it whole",
                level="DEBUG",
            )
            self._emitted = []
        return " ".join(words[len(self._emitted) :])

    def _cut_flushable_locked(self, text: str) -> str:
        """The next chunk of the running turn worth translating, or ``""``.

        Records it as emitted, so neither a later interim nor the turn's final
        transcript hands the same words to the translator a second time.
        """
        remainder = self._remainder_locked(text)
        ready = _cut_complete_sentence(remainder)
        if not ready and looks_semantically_complete(remainder):
            # No terminator, but this much unpunctuated speech is already a
            # wall of text — the same word-count rule the segmented buffer
            # flushes on.
            #
            # The final word is held back because it is the one still being
            # revised. Deltas arrive mid-word, so an interim ends on a
            # fragment: "يشعر" becomes "يشعرون" becomes "يشعرون." over three
            # messages. Emitting that fragment made the next interim fail the
            # prefix check in _remainder_locked, which resets and re-flushes
            # the WHOLE turn — live 2026-08-13 a reciter with no pauses put the
            # same two ayat on screen three times inside 400 ms.
            #
            # Only this branch needs it: _cut_complete_sentence ends on a
            # terminator, so its last word is by definition finished.
            ready = " ".join(remainder.split()[:-1])
        if ready:
            self._emitted.extend(ready.split())
        return ready

    def add_final(self, text: str) -> None:
        with self._lock:
            if self._sentence_flush:
                remainder = self._remainder_locked(text)
                if not remainder:
                    # Every word of this turn already left sentence by
                    # sentence. Publish nothing: the live line must keep its
                    # revision so the last flush's compare-and-clear still
                    # takes it down when that translation lands.
                    self._interim = ""
                    return
                text = remainder
            if not self._parts:
                self._first_part_time = time.time()
            self._parts.append(text)
            self._interim = ""  # this hypothesis window just finalized
            self._publish_live_locked()

    def set_interim(self, text: str) -> tuple[str, int]:
        """Record the newest interim; returns (flushable text, live revision).

        The flushable text is a finished sentence cut out of the still-running
        turn, and is ``""`` unless ``sentence_flush`` is on and the turn has
        produced one. The caller queues it for translation exactly the way it
        queues an ended utterance. The live line is deliberately left carrying
        the whole turn: it renders only its last wrapped row
        (``REALTIME_LIVE_MAX_ROWS``), so trimming the flushed part would show
        the same newest words while risking a blank row between a flush and
        its translation arriving.
        """
        with self._lock:
            self._interim = text
            self._publish_live_locked()
            ready = self._cut_flushable_locked(text) if self._sentence_flush else ""
            return ready, self._live_rev

    def take_and_reset(self) -> tuple[str, int]:
        """Flush the utterance; returns (text, live revision at flush time).

        The live text is deliberately NOT cleared here — it is marked
        *settled* instead: the finished source stays on screen (recolored
        by the GUI) during the ~1-2s translation call, then
        ``clear_live_if_unchanged(rev)`` removes it as the translation
        subtitle appears.
        """
        with self._lock:
            text = " ".join(p for p in self._parts if p.strip())
            self._parts = []
            self._interim = ""
            self._first_part_time = None
            if not text and not self._emitted:
                # Nothing will be translated (only a never-finalized interim
                # got here) — clear the live text now or it would linger.
                self._live_text = ""
                self._live_settled = False
                self._live_rev += 1
            else:
                # No rev bump: newer speech must still win compare-and-clear.
                # An empty text with words already emitted is the intra-turn
                # case — the turn is fully translated but its last call is
                # still in flight, so the settled line stays up for that
                # call's compare-and-clear rather than being blanked here.
                self._live_settled = True
            self._emitted = []  # the turn is over
            return text, self._live_rev

    def get_live_state(self) -> tuple[str, bool]:
        """(live text, settled) — settled means the utterance is finished
        and its translation is in flight."""
        with self._lock:
            return self._live_text, self._live_settled

    def clear_live_if_unchanged(self, rev: int) -> None:
        with self._lock:
            if self._live_rev == rev:
                self._live_text = ""
                self._live_settled = False
                self._live_rev += 1

    def clear_live(self) -> None:
        with self._lock:
            self._live_text = ""
            self._live_settled = False
            self._live_rev += 1

    def has_pending(self) -> bool:
        with self._lock:
            return bool(self._parts)

    def seconds_since_first_part(self) -> float:
        """Age of the oldest accumulated part; 0 when empty.

        The forced-flush cap must use the FIRST part's age: continuous speech
        delivers new finals every few seconds, so a last-activity clock would
        keep resetting and the cap would never fire.
        """
        with self._lock:
            if self._first_part_time is None:
                return 0.0
            return time.time() - self._first_part_time


class StreamingSession:
    """A live streaming-STT connection plus its four worker threads.

    Args:
        provider: The realtime transcription provider to open against.
        provider_id: Its settings id — used only to label log lines.
        model: Provider-specific streaming model, already resolved.
        language: Source language code (streaming engines do not auto-detect).
        capture_rate: Sample rate the engine expects; the controller opens the
            microphone at this rate and the feeder converts bytes to seconds
            with it.
        stop_event: The session's stop event. Captured, not re-read: ``start()``
            creates a fresh one per session and a session is never reused, so
            a leftover worker can never be re-armed by the next session.
        stop_input: Signals the capture thread to stop. A callable rather than
            the event itself because ``change_input_device`` *replaces* the
            controller's input stop event — capturing it would leave a terminal
            error setting an event nothing is waiting on any more.
        translation_queue: Subtitle queue, for audience-facing error messages.
        error_queue: Machine-readable events for the GUI.
        translate: Translates one utterance and emits it. Raises on failure;
            the processor turns that into an error subtitle.
        on_activity: Called whenever the engine proves it is alive, so the
            controller's inactivity auto-stop clock advances too.
    """

    def __init__(
        self,
        provider: StreamingTranscriptionProvider,
        *,
        provider_id: str,
        model: str,
        language: str,
        capture_rate: int,
        stop_event: threading.Event,
        stop_input: Callable[[], None],
        translation_queue: queue.Queue[tuple[str, str | None]],
        error_queue: queue.Queue[str],
        translate: Callable[[str], None],
        on_activity: Callable[[], None],
    ) -> None:
        self._provider = provider
        self._provider_id = provider_id
        self._model = model
        self._language = language
        self.capture_rate = capture_rate
        self._stop_event = stop_event
        self._stop_input = stop_input
        self._translation_queue = translation_queue
        self._error_queue = error_queue
        self._translate = translate
        self._on_activity = on_activity

        self._feed_queue: queue.Queue[bytes] = queue.Queue()
        # Items are (utterance_text, live revision at flush time) — the rev
        # lets the processor compare-and-clear the live transcript afterwards.
        self._utterance_queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._utterances = UtteranceSession(
            sentence_flush=provider_id in _SENTENCE_FLUSH_PROVIDERS
        )

        self._handle: StreamHandle | None = None
        # Serialises every mutation of the streaming connection handle. The
        # error-triggered reconnect supervisor, the stall watchdog and the
        # terminal-error teardown all run on different threads; without this
        # two of them could each open a socket and orphan the loser's (left
        # open and billed, feeding nothing). See _swap_connection.
        self._lock = threading.Lock()
        # Zero-arg callable opening a stream; cleared to signal teardown, so a
        # swap that has not started yet bails instead of reopening.
        self._connect: Callable[[], StreamHandle] | None = None
        self._generation = 0
        self._reconnect_event = threading.Event()
        self._backoff = STREAMING_RECONNECT_BASE_SECONDS
        self._outage = False
        self._fatal_error: str | None = None
        # Built with the session and dying with it; the feeder consults the
        # noise_filter setting per chunk, so the settings checkbox toggles the
        # gate live mid-session.
        self._noise_gate = StreamNoiseGate(capture_rate)
        self._last_activity = time.time()
        # Seconds of speech-classified audio fed to the engine since the last
        # transcript. Arms the stall watchdog: silence produces no transcripts
        # on a healthy connection too, so only fed speech may count toward a
        # "stuck session" verdict.
        self._speech_fed_seconds = 0.0
        # True between the provider's own VAD reporting speech-start and
        # speech-stop — i.e. a turn is open server-side and its transcript
        # cannot arrive yet by design. Only providers that report speech
        # activity ever set it; for the rest it stays False and the watchdog
        # behaves exactly as before.
        self._turn_active = False
        # True from a forced turn commit until the transcript it asked for
        # arrives. A stall that finds it still set means the commit changed
        # nothing — the only case that still warrants swapping the connection.
        self._commit_pending = False

    # --- lifecycle -------------------------------------------------------

    def open(self) -> None:
        """Open the first connection. Raises whatever the provider raises.

        OpenAI Realtime waits for the server's session confirmation, so an
        invalid key is a synchronous startup failure here. A session that
        fails to open is simply discarded — no half-started state to unwind.
        """
        self._connect = self._open_stream
        self._handle = self._open_stream()

    def start_workers(self) -> list[threading.Thread]:
        """Spawn the four session threads; returns them for the caller to join."""
        threads = [
            threading.Thread(target=target, daemon=True, name=name)
            for target, name in (
                (self._feeder_thread, "streaming-feeder"),
                (self._process_utterances, "streaming-processor"),
                (self._reconnect_supervisor, "streaming-supervisor"),
                (self._stall_watchdog, "streaming-stall-watchdog"),
            )
        ]
        for thread in threads:
            thread.start()
        return threads

    def close_connection(self, reason: str) -> None:
        """Tear the connection down for good — no swap can reopen afterwards.

        Clearing the connect callable first makes a not-yet-started swap bail;
        grabbing the handle under the same lock captures one that an in-flight
        swap has just opened, so no socket is left open and billed. Bumping the
        generation makes every callback from the closed connection stale.
        """
        with self._lock:
            self._connect = None
            self._generation = next(_GENERATIONS)
            handle = self._handle
            self._handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception as exc:
                log(f"STREAMING error closing {reason} handle: {exc}", level="DEBUG")

    # --- audio in --------------------------------------------------------

    def feed(self, chunk: bytes) -> None:
        """Hand one block of captured PCM to the feeder thread."""
        try:
            self._feed_queue.put_nowait(chunk)
        except queue.Full:
            pass  # unbounded by default; defensive only

    # --- subtitles out ---------------------------------------------------

    def live_transcript(self) -> tuple[str, bool]:
        """In-progress (not yet translated) transcript as (text, settled)."""
        return self._utterances.get_live_state()

    # --- connection ------------------------------------------------------

    def _open_stream(self) -> StreamHandle:
        # Bump the generation BEFORE opening: an immediate connect error
        # from the new receive thread must count as current, not stale.
        self._generation = next(_GENERATIONS)
        generation = self._generation
        # A fresh connection has no turn open and owes no committed one; a swap
        # must not inherit the dead one's flags or the watchdog would stay
        # disarmed forever.
        self._turn_active = False
        self._commit_pending = False
        return self._provider.open_stream(
            model=self._model,
            language=self._language,
            on_transcript=self._on_transcript,
            on_utterance_end=self._on_utterance_end,
            on_error=lambda exc, gen=generation: self._on_stream_error(exc, gen),
            on_speech_activity=self._on_speech_activity,
        )

    def _swap_connection(self, reason: str) -> bool:
        """Atomically replace the live streaming connection with a fresh one.

        Both recovery paths — the error-triggered reconnect supervisor and the
        stall watchdog — call this from their own threads. The lock serialises
        them (and the terminal-error teardown, which takes the same lock) so two
        of them can never each open a socket and leave the loser's connection
        dangling — open and billed, feeding nothing.

        Returns True once a new connection is open, or False if the session is
        being torn down (``_connect`` was cleared by ``close_connection``). Any
        exception raised while opening propagates to the caller so it can
        classify it.

        Whatever the dying connection already transcribed is flushed first.
        Left in the accumulator it would survive, but only to be concatenated
        onto the *next* connection's first turn and translated as one block —
        speech from either side of an outage welded into a single subtitle.
        The old connection's words are a finished unit; they leave as one.
        """
        with self._lock:
            connect = self._connect
            if connect is None:
                return False  # stop()/terminal error already tore the session down
            # After the bail-out, so a teardown that won the race is not handed
            # a translation to run. Takes only the accumulator's own lock, which
            # is never held while acquiring this one.
            self._on_utterance_end()
            old = self._handle
            self._handle = None  # feeder drops chunks while down
            if old is not None:
                try:
                    old.close()
                except Exception as e:
                    log(f"STREAMING error closing {reason} handle: {e}", level="DEBUG")
            self._handle = connect()
            return True

    def _commit_open_turn(self) -> bool:
        """Ask the provider to end the turn it is holding open and transcribe it.

        The non-destructive half of stall recovery. Returns True if the commit
        was issued, meaning the caller must NOT reopen the connection.

        The turn flag deliberately stays ON. A forced commit bypasses the
        server's VAD, so no speech-stopped event arrives to clear it — but no
        fresh speech-STARTED arrives either, because from the server's point of
        view the speech never stopped. Clearing the flag therefore did not
        re-arm the watchdog for the next real stall, it guaranteed that the
        next stall found no turn in flight and swapped the connection. Measured
        live 2026-08-13 during pauseless recitation: commit, 21 s, reconnect,
        16 s, commit — five cycles 40.4 s apart to within 0.1 s, half of them
        tearing down a connection that answered every commit within 0.6 s. Each
        swap cost 2-3 ayat, because the feeder drops audio while the handle is
        down.

        Escalation is preserved by ``_commit_pending`` instead: a transcript
        clears it, so a commit that produced nothing leaves it set and the
        following stall recovers the old way.
        """
        handle = self._handle
        if handle is None:
            return False
        if self._commit_pending:
            # The last stall already committed this turn and no transcript came
            # back. The engine is not merely mid-turn, it is stuck — drop the
            # flag so the caller falls through to the swap.
            self._turn_active = False
            log(
                "STREAMING the previous turn commit produced no transcript — "
                "treating the session as stuck after all.",
                level="WARNING",
            )
            return False
        try:
            committed = handle.commit_turn()
        except Exception as e:
            log(f"STREAMING turn commit failed: {e}", level="WARNING")
            return False
        if not committed:
            return False
        self._commit_pending = True
        self._mark_activity()
        log(
            f"STREAMING No transcript for over "
            f"{STREAMING_TURN_COMMIT_SECONDS:.0f}s, but the engine reports a "
            "turn still open — committing it instead of reconnecting, so the "
            "buffered speech is transcribed rather than discarded.",
            level="INFO",
        )
        return True

    def _mark_activity(self) -> None:
        """The engine proved it is alive: restart both stall clocks."""
        self._last_activity = time.time()
        self._speech_fed_seconds = 0.0
        self._on_activity()

    # --- provider callbacks ----------------------------------------------

    def _on_transcript(self, text: str, is_final: bool) -> None:
        if not text.strip():
            return
        self._mark_activity()
        # The engine answered: whatever a forced commit asked for has arrived,
        # so the next stall may commit again instead of escalating to a swap.
        self._commit_pending = False
        if self._outage:
            # Proof of life after a reconnect: end the outage and reset
            # the backoff for the next disconnect.
            self._outage = False
            self._backoff = STREAMING_RECONNECT_BASE_SECONDS
        if is_final:
            self._utterances.add_final(text)
        else:
            ready, live_rev = self._utterances.set_interim(text)
            if ready:
                # A finished sentence inside a turn that is still running:
                # translate it now instead of waiting for the speaker to pause
                # (issue #26). Same queue as an ended utterance, so the
                # coalescing, the letter check and the block splitting
                # downstream all apply to it unchanged.
                self._utterance_queue.put((ready, live_rev))

    def _on_utterance_end(self) -> None:
        text, live_rev = self._utterances.take_and_reset()
        if text.strip():
            self._utterance_queue.put((text, live_rev))

    def _on_speech_activity(self, speaking: bool) -> None:
        """The provider's VAD heard speech start or stop.

        Both edges are proof the connection is alive, which is why each one
        restarts the stall clock. The flag matters more than the clock: while
        a turn is open the engine *cannot* have produced a transcript yet, so
        silence from the transcript side says nothing about its health.
        """
        self._turn_active = speaking
        self._mark_activity()

    def _on_stream_error(self, exc: Exception, generation: int) -> None:
        if generation != self._generation:
            # Late callback from an already-replaced connection (one
            # disconnect can produce several) — the reconnect it asks
            # for already happened.
            log(f"STREAMING stale connection error ignored: {exc}", level="DEBUG")
            return
        log(f"STREAMING connection error ({self._provider_id}): {exc}", level="ERROR")
        if self._handle_terminal_error(exc):
            return
        self._error_queue.put(f"transcription_error:{exc}")
        # The stream is dead — no more interims will correct the live
        # line, so take it down rather than leave stale text standing.
        self._utterances.clear_live()
        if not self._outage:
            # One audience-facing message per outage; the supervisor's
            # retries must not stack further error subtitles.
            self._outage = True
            self._translation_queue.put((get_user_message(classify_error(exc)), None))
        self._reconnect_event.set()

    def _handle_terminal_error(self, exc: Exception) -> bool:
        """Stop a stream that cannot recover without operator action.

        Invalid credentials do not improve with exponential backoff. Mark the
        session for GUI-side shutdown, close the current socket and emit one
        machine-readable error. The raw provider exception deliberately does
        not enter either queue because it may contain a masked key fragment.
        """
        error_kind = classify_error(exc)
        if error_kind != "invalid_api_key":
            return False

        if self._fatal_error is None:
            self._fatal_error = error_kind
            self._error_queue.put(f"fatal_transcription_error:{error_kind}")

        self._utterances.clear_live()

        # Quiesce every streaming worker while the GUI consumes the fatal
        # event and performs the normal controller.stop() cleanup. Crucially,
        # never wake the reconnect supervisor for an authentication failure.
        self._outage = True
        self._reconnect_event.clear()
        self._stop_input()
        self._stop_event.set()
        self.close_connection("terminally failed")
        return True

    # --- workers ---------------------------------------------------------

    def _feeder_thread(self) -> None:
        stop_event = self._stop_event
        while not stop_event.is_set():
            try:
                chunk = self._feed_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            # Local capture: the reconnect supervisor nulls/replaces the
            # handle concurrently during an outage — chunks captured while
            # the connection is down are dropped (replaying them later would
            # burst stale text onto live subtitles).
            handle = self._handle
            if handle is not None:
                # Noise gate: sustained non-speech (static from a muted mixer
                # channel) is fed as digital silence so the engine's server
                # VAD stops inventing turns. The settings read is cached, so
                # the checkbox toggles this live mid-session.
                gate = self._noise_gate
                is_speech = True
                if load_settings().noise_filter:
                    chunk = gate.process(chunk)
                    is_speech = not gate.is_zeroing
                try:
                    handle.feed(chunk)
                except Exception as e:
                    # A concurrent swap/stop can close this handle between the
                    # read above and here; feeding a closed connection then
                    # raises. Drop the chunk (same policy as chunks captured
                    # while down) rather than let it kill the feeder thread and
                    # silence the pipeline for the rest of the session.
                    log(f"STREAMING feeder dropped a chunk: {e}", level="DEBUG")
                else:
                    if is_speech:
                        # Arm the stall watchdog: only audio the gate passed
                        # through counts as something the engine should have
                        # transcribed. int16 mono bytes → seconds.
                        self._speech_fed_seconds += len(chunk) / 2 / self.capture_rate

    def _reconnect_supervisor(self) -> None:
        """Reconnect-with-backoff for streaming mode.

        When the connection dies (network blip, server-side session end) the
        stream used to stay dead until a manual Stop → Start — for a 1-2h
        khutbah the main operational risk. The error callback flags a
        reconnect; this thread closes the dead handle and opens a fresh one
        with the same engine/model/language and callbacks. Retries continue
        until stop() with exponential backoff capped at
        STREAMING_RECONNECT_MAX_SECONDS; the first transcript after a
        reconnect resets the backoff (see _on_transcript). Segmented mode
        never starts this thread — it self-heals per segment via the retry
        chain.
        """
        stop_event = self._stop_event
        reconnect_event = self._reconnect_event
        while not stop_event.is_set():
            if not reconnect_event.wait(timeout=0.2):
                continue
            # Back off BEFORE reconnecting: paces retry storms and coalesces
            # the duplicate error callbacks one disconnect can produce.
            delay = self._backoff
            self._backoff = min(delay * 2, STREAMING_RECONNECT_MAX_SECONDS)
            if stop_event.wait(delay):
                break
            reconnect_event.clear()
            try:
                opened = self._swap_connection("dead")
            except Exception as e:
                if self._handle_terminal_error(e):
                    break
                # open_stream rarely raises (connect failures arrive async
                # via on_error) — but if it does, queue another attempt.
                log(f"STREAMING Reconnect attempt failed: {e}", level="WARNING")
                reconnect_event.set()
                continue
            if not opened:
                break  # stop() already tore the session down
            log(
                f"STREAMING Reconnected after {delay:.0f}s backoff — "
                "new connection opened.",
                level="INFO",
            )
        log("STREAMING reconnect supervisor ended", level="DEBUG")

    def _stall_watchdog(self) -> None:
        """Silently reopen the connection if it stays up but stops producing
        transcripts while speech is being fed.

        Observed live: the connection reports no error and the reconnect
        supervisor above never fires, yet no interim/final transcript arrives
        for 15-26s straight during continuous speech, and content from that
        stretch is lost. Unlike an error-triggered reconnect, this never
        shows an audience-facing message and never backs off.

        Two guards keep a real pause in speech from triggering it (observed
        live 2026-07-24: the unconditional version reconnected during every
        quiet stretch >15s, and because the feeder drops audio while the
        handle is down, speech resuming into the swap window lost its first
        words — e.g. a swap fired 0.6s after "Speech resumed"):

        - Arm only after the feeder passed at least
          STREAMING_STALL_MIN_SPEECH_SECONDS of speech-classified audio to
          the engine since the last transcript — a healthy connection is
          just as silent as a stuck one when nothing transcribable is fed.
        - Once armed, wait up to STREAMING_STALL_GRACE_SECONDS for either a
          transcript (proof of life — cancel the swap entirely) or a
          noise-gate-closed moment (only silence is flowing — swap now,
          losing nothing). Only a gate that stays open through the whole
          grace forces a mid-speech swap: exactly the stuck-session case,
          where that speech is already being lost. With the noise filter
          off there is no gate signal and the swap happens immediately.

        Neither guard was enough, because both ask the *client* whether speech
        is flowing and neither asks the engine whether it is working. Measured
        live 2026-08-12 over a 43-minute khutbah: 53 stall reconnects, zero
        connection errors, ~25 minutes with no subtitles, and a 128 s hole
        where five consecutive reconnects each destroyed the turn the previous
        one was waiting on — a metronome at 23.9 s, the watchdog's own cycle
        time. A speaker the server-side VAD never hears pause produces one
        endless turn, and a turn produces no transcript until it commits, so
        "no transcript for 15 s" was indistinguishable from a dead socket.
        The watchdog was not failing to fix the outage; it *was* the outage.

        So a third guard, and it is the one that asks the engine:

        - While the provider reports a turn in flight, a missing transcript is
          expected rather than suspicious. Reopening the connection there
          throws away speech that was never transcribed, so instead ask the
          provider to end the turn and transcribe what it has
          (``commit_turn``) — the buffered audio is kept, not discarded. Only
          if the provider offers no such control, or the commit fails, does
          the swap still happen.

        The open-turn commit runs on its own clock, ``TURN_COMMIT_SECONDS``,
        because it is answering a different question. "How long may a turn run
        before we ask for its text" is display latency: with OpenAI silent for
        the whole turn, it alone sets how often subtitles appear. "How long may
        a connection produce nothing before we call it dead" is recovery. They
        were one number until 2026-08-13, which is why the only way to make
        subtitles arrive sooner was to make the session quicker to give up on
        its own socket.
        """
        stop_event = self._stop_event
        poll = min(
            2.0, STREAMING_TURN_COMMIT_SECONDS / 2, STREAMING_STALL_TIMEOUT_SECONDS / 2
        )
        while not stop_event.wait(timeout=poll):
            if self._outage or self._handle is None:
                continue  # the error-triggered supervisor already owns recovery
            stalled_since = self._last_activity
            idle = time.time() - stalled_since
            if self._speech_fed_seconds < STREAMING_STALL_MIN_SPEECH_SECONDS:
                continue  # a pause in the speech, not a stuck session
            if (
                self._turn_active
                and idle > STREAMING_TURN_COMMIT_SECONDS
                and self._commit_open_turn()
            ):
                continue  # the engine is working; it just has not committed yet
            if idle <= STREAMING_STALL_TIMEOUT_SECONDS:
                # Too early to call the connection dead. A commit that was
                # refused here is not evidence of anything: the provider may
                # simply not offer one (Deepgram, Gemini).
                continue
            grace_deadline = time.time() + STREAMING_STALL_GRACE_SECONDS
            cancelled = False
            while True:
                if self._last_activity != stalled_since or self._outage:
                    cancelled = True  # alive after all / error path owns recovery
                    break
                if (
                    not load_settings().noise_filter
                    or self._noise_gate.is_zeroing
                    or time.time() >= grace_deadline
                ):
                    break  # non-speech moment (or no gate signal / grace over)
                if stop_event.wait(timeout=0.2):
                    cancelled = True
                    break
            if cancelled:
                continue
            log(
                f"STREAMING No transcript for over "
                f"{STREAMING_STALL_TIMEOUT_SECONDS:.0f}s while speech was fed, "
                "with no error reported — reconnecting silently in case the "
                "session is stuck.",
                level="WARNING",
            )
            try:
                opened = self._swap_connection("stalled")
            except Exception as e:
                log(f"STREAMING Silent reconnect attempt failed: {e}", level="WARNING")
                continue
            if not opened:
                break  # stop() already tore the session down
            # Restart the timer now, not on the next transcript — otherwise a
            # connection that also takes a few seconds to speak would trip the
            # watchdog again before it gets a chance.
            self._mark_activity()
            log("STREAMING Silently reconnected after a stall.", level="INFO")
        log("STREAMING stall watchdog ended", level="DEBUG")

    def _process_utterances(self) -> None:
        stop_event = self._stop_event
        utterances = self._utterances

        def _emit(trans_text: str, live_rev: int) -> None:
            # Mirror _process_audio's per-item recovery: an unexpected error
            # must show a subtitle and keep the thread alive, not silently
            # kill all subtitles for the rest of the session.
            try:
                self._translate(trans_text)
            except Exception as e:
                log(f"STREAMING-PROCESSOR Error: {e}", level="ERROR")
                self._error_queue.put(f"translation_error:{e}")
                self._translation_queue.put(
                    (get_user_message(classify_error(e)), None)
                )
            finally:
                # The subtitle (or error message) for this utterance is out —
                # take its live transcript off screen unless newer speech has
                # already replaced it.
                utterances.clear_live_if_unchanged(live_rev)

        # Micro-utterance coalescing: hold a short utterance and merge it with
        # the next one so GPT translates a whole clause, not an isolated
        # fragment (see STREAMING_COALESCE_* in config). pending_rev tracks the
        # newest live revision in the buffer for compare-and-clear.
        pending = ""
        pending_rev = 0
        hold_deadline: float | None = None

        def _flush_pending() -> None:
            nonlocal pending, hold_deadline
            text = pending.strip()
            pending = ""
            hold_deadline = None
            if not text:
                return
            if has_min_letters(text):
                _emit(text, pending_rev)
            else:
                # Only sub-word fragments accumulated (e.g. "م"): never worth a
                # GPT call — just take its live line down.
                utterances.clear_live_if_unchanged(pending_rev)

        def _accept(trans_text: str, live_rev: int) -> None:
            nonlocal pending, pending_rev, hold_deadline
            trans_text = trans_text.strip()
            pending = f"{pending} {trans_text}".strip() if pending else trans_text
            pending_rev = live_rev
            # Word count alone, deliberately. Also requiring a sentence
            # boundary here was tried on 2026-08-14 and reverted: it cannot
            # act at this cadence. The hold deadline below is 1 s while
            # utterances arrive a median of 3 s apart, so the timer always
            # fires first and flushes the buffer anyway — measured across two
            # live runs of the same khutbah, blocks/min and words/block did not
            # move. Making it fire would need a 3-4 s hold, i.e. real latency.
            # The damage it aimed at (a fragment reading as a closed sentence)
            # is handled in the translation prompt instead, by mirroring the
            # source's end punctuation.
            if len(pending.split()) >= STREAMING_COALESCE_MIN_WORDS:
                _flush_pending()
            else:
                hold_deadline = time.time() + STREAMING_COALESCE_HOLD_SECONDS

        while not stop_event.is_set():
            try:
                trans_text, live_rev = self._utterance_queue.get(timeout=0.2)
                _accept(trans_text, live_rev)
                continue
            except queue.Empty:
                pass

            # Trailing clause: a held short utterance with no follow-up in
            # COALESCE_HOLD_SECONDS flushes on its own (end of a sentence).
            if pending and hold_deadline is not None and time.time() >= hold_deadline:
                _flush_pending()

            # Forced flush: continuous speech may never produce an
            # utterance-end, so cap how old accumulated text can get.
            if (
                utterances.has_pending()
                and utterances.seconds_since_first_part()
                > STREAMING_MAX_UTTERANCE_SECONDS
            ):
                capped, live_rev = utterances.take_and_reset()
                if capped.strip():
                    log("STREAMING-PROCESSOR Max-utterance flush", level="DEBUG")
                    _accept(capped, live_rev)
                    _flush_pending()  # long text: emit now, do not keep holding

        # Stop: merge any un-ended session tail into the held clause, flush all.
        remaining, live_rev = utterances.take_and_reset()
        if remaining.strip():
            _accept(remaining, live_rev)
        _flush_pending()

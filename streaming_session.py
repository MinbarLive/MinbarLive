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
)
from providers.base import StreamHandle, StreamingTranscriptionProvider
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
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._first_part_time: float | None = None
        self._interim = ""
        self._live_text = ""
        self._live_settled = False
        self._live_rev = 0

    def _publish_live_locked(self) -> None:
        pieces = [p for p in self._parts if p.strip()]
        if self._interim.strip():
            pieces.append(self._interim)
        self._live_text = " ".join(pieces)
        self._live_settled = False  # new speech: the line is in progress again
        self._live_rev += 1

    def add_final(self, text: str) -> None:
        with self._lock:
            if not self._parts:
                self._first_part_time = time.time()
            self._parts.append(text)
            self._interim = ""  # this hypothesis window just finalized
            self._publish_live_locked()

    def set_interim(self, text: str) -> None:
        with self._lock:
            self._interim = text
            self._publish_live_locked()

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
            if not text:
                # Nothing will be translated (only a never-finalized interim
                # got here) — clear the live text now or it would linger.
                self._live_text = ""
                self._live_settled = False
                self._live_rev += 1
            else:
                # No rev bump: newer speech must still win compare-and-clear.
                self._live_settled = True
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
        self._utterances = UtteranceSession()

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
        return self._provider.open_stream(
            model=self._model,
            language=self._language,
            on_transcript=self._on_transcript,
            on_utterance_end=self._on_utterance_end,
            on_error=lambda exc, gen=generation: self._on_stream_error(exc, gen),
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
        """
        with self._lock:
            connect = self._connect
            if connect is None:
                return False  # stop()/terminal error already tore the session down
            old = self._handle
            self._handle = None  # feeder drops chunks while down
            if old is not None:
                try:
                    old.close()
                except Exception as e:
                    log(f"STREAMING error closing {reason} handle: {e}", level="DEBUG")
            self._handle = connect()
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
        if self._outage:
            # Proof of life after a reconnect: end the outage and reset
            # the backoff for the next disconnect.
            self._outage = False
            self._backoff = STREAMING_RECONNECT_BASE_SECONDS
        if is_final:
            self._utterances.add_final(text)
        else:
            self._utterances.set_interim(text)

    def _on_utterance_end(self) -> None:
        text, live_rev = self._utterances.take_and_reset()
        if text.strip():
            self._utterance_queue.put((text, live_rev))

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
        """
        stop_event = self._stop_event
        while not stop_event.wait(timeout=min(2.0, STREAMING_STALL_TIMEOUT_SECONDS / 2)):
            if self._outage or self._handle is None:
                continue  # the error-triggered supervisor already owns recovery
            stalled_since = self._last_activity
            if time.time() - stalled_since <= STREAMING_STALL_TIMEOUT_SECONDS:
                continue
            if self._speech_fed_seconds < STREAMING_STALL_MIN_SPEECH_SECONDS:
                continue  # a pause in the speech, not a stuck session
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

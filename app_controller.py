"""Thread lifecycle controller for starting/stopping the pipeline."""

from __future__ import annotations

import os
import queue
import threading
import time

import numpy as np
import scipy.io.wavfile as wavfile
import sounddevice as sd

from audio.capture import (
    audio_callback,
    get_default_input_device,
    is_silence,
    reset_ring_buffer,
    write_samples_to_ring,
)
from audio.device_support import (
    AudioInputError,
    input_device_candidates,
    input_stream_kwargs,
)
from audio.level_meter import AudioLevelMeter, AudioLevelSnapshot
from audio.loopback import get_speaker as get_loopback_speaker
from audio.vad import StreamNoiseGate, has_speech
from audio.writer import async_write_audio, clear_write_queue, segment_writer
from config import (
    AUDIO_DIR,
    FS,
    LOOPBACK_CAPTURE_BUFFER_SECONDS,
    STREAMING_CHUNK_MS,
    STREAMING_COALESCE_HOLD_SECONDS,
    STREAMING_COALESCE_MIN_WORDS,
    STREAMING_MAX_UTTERANCE_SECONDS,
    STREAMING_RECONNECT_BASE_SECONDS,
    STREAMING_RECONNECT_MAX_SECONDS,
    STREAMING_STALL_TIMEOUT_SECONDS,
)
from providers import (
    get_streaming_capture_sample_rate,
    get_streaming_key_provider,
    get_streaming_transcription_provider,
    get_transcription_model_chain,
    get_transcription_provider,
    get_translation_model_chain,
    has_usable_key,
    resolve_streaming_transcription_model,
)
from translation.buffering import (
    AudioSegment,
    ChunkBasedStrategy,
    ProcessingStrategy,
    SemanticBufferingStrategy,
)
from translation.stt import (
    has_min_letters,
    maybe_arabic_retranscription,
    strip_overlap_prefix,
    transcribe_with_fallback,
)
from translation.translator import translate_text
from utils.context_manager import get_context_manager
from utils.history import log_transcription_and_translation
from utils.logging import log
from utils.settings import (
    PIPELINE_MODE_STREAMING,
    get_source_language_code,
    load_settings,
)
from utils.user_messages import classify_error, get_user_message

INPUT_STREAM_START_TIMEOUT_SECONDS = 6.0
INPUT_STREAM_OPEN_ATTEMPTS = 2
INPUT_STREAM_RETRY_DELAY_SECONDS = 0.18


class _StreamingUtteranceSession:
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


class AppController:
    def __init__(self):
        self.stop_event = threading.Event()
        self._input_stop_event = threading.Event()  # Separate stop for input stream
        self._input_thread: threading.Thread | None = None
        self._input_level_meter = AudioLevelMeter()
        self._input_level_test_stop_event = threading.Event()
        self._input_level_test_thread: threading.Thread | None = None
        self._current_device: int | None = None
        self._streaming_capture_rate: int = (
            FS  # rate used by the current streaming input thread
        )
        self.threads: list[threading.Thread] = []
        # Items are (display_text, source_text): source_text is the original
        # transcription for bilingual display, None when there is no separate
        # source (error messages, same-language mode).
        self.translation_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.error_queue: queue.Queue[str] = queue.Queue()
        self._running = False
        self.strategy: ProcessingStrategy | None = None
        # Streaming pipeline_mode state (unused in segmented mode)
        self._streaming_feed_queue: queue.Queue[bytes] = queue.Queue()
        # Items are (utterance_text, live revision at flush time) — the rev
        # lets the processor compare-and-clear the live transcript afterwards.
        self._streaming_utterance_queue: queue.Queue[tuple[str, int]] = queue.Queue()
        self._streaming_handle = None
        self._streaming_session: _StreamingUtteranceSession | None = None
        self._noise_gate: StreamNoiseGate | None = None
        # Reconnect-with-backoff state (streaming mode only). The generation
        # counter keeps late callbacks from an already-replaced connection
        # from triggering another reconnect; it is deliberately never reset,
        # so callbacks from a previous session are stale too.
        self._streaming_connect = None  # zero-arg callable opening a stream
        self._streaming_generation = 0
        self._streaming_reconnect_event = threading.Event()
        self._streaming_backoff = STREAMING_RECONNECT_BASE_SECONDS
        self._streaming_outage = False
        self._streaming_fatal_error: str | None = None
        # Last time a transcription arrived (either pipeline mode) — the GUI
        # polls this for the inactivity auto-stop.
        self._last_pipeline_activity = time.time()

    def _process_audio(self):
        context_mgr = get_context_manager()
        files_processed = 0
        # Session-local stop event: start() REPLACES self.stop_event, so a
        # thread that outlives stop()'s join timeout (e.g. a transcription
        # call in flight) must capture its own event — reading the attribute
        # live re-armed such a leftover thread on the next start(), where it
        # ran as a zombie inside a streaming session (strategy is None there)
        # and double-processed audio. Same pattern in every thread loop below.
        stop_event = self.stop_event
        # Raw transcription of the previous segment, for overlap dedup. Reset
        # to "" on any pause/skip (silence, non-speech, failure) so the dedup
        # only ever fires between two temporally adjacent speech segments.
        prev_transcription = ""

        while not stop_event.is_set():
            files = sorted([f for f in os.listdir(AUDIO_DIR) if f.endswith(".wav")])

            for file in files:
                file_path = os.path.join(AUDIO_DIR, file)
                start_time = time.time()
                active_error_role = "transcription"
                log(f"AUDIO-PROCESSOR File found: {file}", level="INFO")

                try:
                    _, data = wavfile.read(file_path)
                    audio_float = data.astype(np.float32) / 32767.0

                    if is_silence(audio_float):
                        log(
                            f"AUDIO-PROCESSOR Silence detected → deleted: {file}",
                            level="DEBUG",
                        )
                        os.remove(file_path)
                        prev_transcription = ""  # a pause breaks the overlap
                        continue

                    # Loud but not speech (static, hum): the RMS gate above
                    # can't tell — STT would hallucinate sentences from it.
                    if load_settings().noise_filter and not has_speech(audio_float):
                        log(
                            "AUDIO-PROCESSOR Non-speech audio (noise filter) "
                            f"→ deleted: {file}",
                            level="INFO",
                        )
                        os.remove(file_path)
                        prev_transcription = ""
                        continue

                    log(f"AUDIO-PROCESSOR Transcription started: {file}", level="INFO")

                    # Source language from settings; provider-aware model chain
                    settings = load_settings()
                    lang_code = get_source_language_code(settings.source_language)
                    models_to_try = get_transcription_model_chain()

                    with open(file_path, "rb") as audio_file:
                        audio_bytes = audio_file.read()

                    # Language hint if configured; None means auto-detect
                    transcription = transcribe_with_fallback(
                        get_transcription_provider(),
                        models_to_try,
                        audio_bytes,
                        lang_code,
                    )
                    if transcription is None:
                        os.remove(file_path)
                        prev_transcription = ""
                        continue  # Skip this audio file

                    log("AUDIO-PROCESSOR Transcription received", level="DEBUG")
                    self._last_pipeline_activity = time.time()

                    # Strip the OVERLAP-second repeat of the previous segment's
                    # tail (a visible duplicate on every boundary). Compare
                    # against the previous RAW transcription, then store this
                    # segment's raw text for the next comparison.
                    deduped = strip_overlap_prefix(prev_transcription, transcription)
                    prev_transcription = transcription
                    transcription = deduped

                    # Fragment gate: a sub-word residual ("م" → "h", a
                    # near-silent "Um") is never worth a translation call.
                    if not has_min_letters(transcription):
                        log(
                            f"AUDIO-PROCESSOR Fragment skipped: {transcription!r}",
                            level="DEBUG",
                        )
                        os.remove(file_path)
                        continue

                    # Secondary Arabic transcription for the Quran/Athan
                    # matchers (skip conditions documented in translation/stt).
                    arabic_transcription = maybe_arabic_retranscription(
                        get_transcription_provider(),
                        models_to_try[0],
                        audio_bytes,
                        transcription=transcription,
                        source_lang_code=lang_code,
                        source_language=settings.source_language,
                        target_language=settings.target_language,
                        islamic_mode=load_settings().islamic_mode,
                    )

                    # Create AudioSegment for strategy processing
                    segment = AudioSegment(
                        file_path=file_path,
                        transcription=transcription,
                        is_silent=False,
                        timestamp=time.time(),
                    )

                    transcriptions_to_translate = self.strategy.add_segment(segment)
                    log_transcriptions = []  # To store transcription-translation pairs
                    active_error_role = "translation"

                    for trans_text in transcriptions_to_translate:
                        # History is logged after the loop with the measured
                        # segment duration, not here.
                        translation = self._translate_and_queue(
                            context_mgr,
                            trans_text,
                            arabic_text=arabic_transcription,
                            log_history=False,
                        )
                        if translation.strip():
                            log_transcriptions.append((trans_text, translation))

                    try:
                        os.remove(file_path)
                    except Exception as e_del:
                        log(
                            f"AUDIO-PROCESSOR Delete error for {file}: {e_del}",
                            level="ERROR",
                        )

                    files_processed += 1
                    duration = time.time() - start_time
                    log(
                        f"AUDIO-PROCESSOR Processing complete in {duration:.2f}s",
                        level="INFO",
                    )

                    # Log all transcription-translation pairs
                    for trans_text, translation in log_transcriptions:
                        log_transcription_and_translation(
                            trans_text, translation, duration=duration
                        )

                except Exception as e:
                    log(f"AUDIO-PROCESSOR Error for {file}: {e}", level="ERROR")
                    self.error_queue.put(f"{active_error_role}_error:{e}")
                    prev_transcription = ""
                    # Delete file anyway to prevent buildup during network outages
                    try:
                        os.remove(file_path)
                        log(
                            f"AUDIO-PROCESSOR Deleted {file} after error", level="DEBUG"
                        )
                    except Exception:
                        pass
                    # Show the classified error in subtitles (target language)
                    self.translation_queue.put(
                        (get_user_message(classify_error(e)), None)
                    )

            # During pure silence no segments arrive (the writer skips
            # them), so the semantic buffer's timeout could never fire from
            # add_segment — a buffered incomplete sentence would sit until
            # speech resumes or stop. Flush it from here instead.
            if self.strategy is not None:
                for stale_text in self.strategy.flush_if_stale():
                    self._safe_translate_and_queue(context_mgr, stale_text)

            time.sleep(0.2)

        if self.strategy is not None:
            for transcription_text in self.strategy.flush():
                self._safe_translate_and_queue(context_mgr, transcription_text)

        log(f"AUDIO-PROCESSOR ended. Total processed: {files_processed}", level="INFO")

    def _translate_and_queue(
        self,
        context_mgr,
        trans_text: str,
        *,
        arabic_text: str = "",
        log_history: bool = True,
        log_prefix: str = "AUDIO-PROCESSOR",
    ) -> str:
        """Translate one transcription and emit it to the subtitle queue.

        The single copy of the emit sequence shared by the segmented
        per-segment path, the idle/stop buffer flushes and the streaming
        processor: same-language check → context → translation → bilingual
        source suppression → queue → history log. Callers that batch-log
        with a measured duration pass log_history=False.
        """
        settings = load_settings()
        same_language = settings.source_language == settings.target_language
        context_mgr.add_transcription(
            trans_text, enable_summarization=not same_language
        )
        context = "" if same_language else context_mgr.get_context()
        if same_language:
            log(f"{log_prefix} Same-language mode", level="INFO")
        else:
            log(f"{log_prefix} Translation started", level="INFO")
        translation = translate_text(trans_text, context, arabic_text=arabic_text)
        if not translation.strip():
            # GPT judged the input unintelligible (the system prompt returns an
            # empty string for that) — emit no subtitle rather than a blank
            # line, and log no empty pair.
            log(f"{log_prefix} Empty translation suppressed", level="DEBUG")
            return translation
        # No separate source line when the translation came back identical —
        # the per-segment bypass ("Automatic" source + Arabic target) and the
        # code-switching pass-through both return the input unchanged even
        # though the language *names* differ, and bilingual mode must not
        # render the same text twice.
        source_text = (
            None
            if same_language or translation.strip() == trans_text.strip()
            else trans_text
        )
        self.translation_queue.put((translation, source_text))
        if log_history:
            log_transcription_and_translation(trans_text, translation)
        return translation

    def _safe_translate_and_queue(self, context_mgr, trans_text: str) -> None:
        # The idle flush runs inside the processor loop for the whole
        # session — an unexpected error must show a subtitle and keep the
        # thread alive (mirrors the per-file recovery above).
        try:
            self._translate_and_queue(context_mgr, trans_text)
        except Exception as e:
            log(f"AUDIO-PROCESSOR Buffer flush error: {e}", level="ERROR")
            self.error_queue.put(f"translation_error:{e}")
            self.translation_queue.put((get_user_message(classify_error(e)), None))

    @staticmethod
    def _report_input_start(
        startup_result: queue.Queue[BaseException | None] | None,
        result: BaseException | None,
    ) -> None:
        if startup_result is None:
            return
        try:
            startup_result.put_nowait(result)
        except queue.Full:
            pass

    def get_input_level(self) -> AudioLevelSnapshot:
        """Return the latest local input level without exposing mutable state."""

        return self._input_level_meter.snapshot()

    def reset_input_level(self) -> None:
        """Clear the input meter immediately (for stop and device changes)."""

        self._input_level_meter.reset()

    def is_input_level_test_running(self) -> bool:
        """Whether a local meter-only capture thread is currently active."""

        thread = self._input_level_test_thread
        return bool(
            thread is not None
            and thread.is_alive()
            and not self._input_level_test_stop_event.is_set()
        )

    def start_input_level_test(self, input_device: int | None = None) -> None:
        """Open local meter-only capture and synchronously confirm the device.

        This preview never starts writers, providers, translation, history, or
        cost tracking. A live session and a preview cannot own the same input
        concurrently.
        """

        if self._running:
            raise RuntimeError("Cannot test the input level during a live session.")

        self.stop_input_level_test()
        if (
            self._input_level_test_thread is not None
            and self._input_level_test_thread.is_alive()
        ):
            raise AudioInputError("The previous input-level test is still stopping.")
        if input_device is None:
            input_device = get_default_input_device()

        self.reset_input_level()
        test_stop = threading.Event()
        startup_result: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
        thread = threading.Thread(
            target=self._input_level_test_capture_thread,
            args=(input_device, test_stop, startup_result),
            daemon=True,
            name="input-level-test",
        )
        self._input_level_test_stop_event = test_stop
        self._input_level_test_thread = thread
        thread.start()

        try:
            result = startup_result.get(timeout=INPUT_STREAM_START_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            test_stop.set()
            thread.join(timeout=0.5)
            if self._input_level_test_thread is thread and not thread.is_alive():
                self._input_level_test_thread = None
            self.reset_input_level()
            raise AudioInputError(
                "Audio input did not open within the startup timeout."
            ) from exc

        if result is not None:
            test_stop.set()
            thread.join(timeout=0.5)
            if self._input_level_test_thread is thread and not thread.is_alive():
                self._input_level_test_thread = None
            self.reset_input_level()
            raise AudioInputError(str(result)) from result

    def stop_input_level_test(self, timeout: float = 1.0) -> None:
        """Stop meter-only capture without touching a live pipeline."""

        thread = self._input_level_test_thread
        self._input_level_test_stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        if (
            self._input_level_test_thread is thread
            and (thread is None or not thread.is_alive())
        ):
            self._input_level_test_thread = None
        elif thread is not None and thread.is_alive():
            log("Input level test is still stopping", level="WARNING")
        self.reset_input_level()

    def _observe_input_level(self, mono, samplerate: int) -> None:
        """Publish mono PCM before VAD/noise-gate processing."""

        self._input_level_meter.observe(mono, sample_rate=samplerate)

    def _segmented_audio_callback(self, indata, frames, time_info, status) -> None:
        self._observe_input_level(indata[:, 0], FS)
        audio_callback(indata, frames, time_info, status)

    def _start_confirmed_input_thread(
        self,
        target,
        args: tuple,
        *,
        timeout: float = INPUT_STREAM_START_TIMEOUT_SECONDS,
    ) -> None:
        """Start capture and wait until the OS has actually opened the device.

        Previously ``start()`` returned as soon as the background thread was
        created.  A later PortAudio failure therefore left the GUI displaying
        a live session with no microphone.  The thread now reports either a
        successful context-manager entry or its opening exception first.
        """

        startup_result: queue.Queue[BaseException | None] = queue.Queue(maxsize=1)
        self._input_thread = threading.Thread(
            target=target,
            args=(*args, startup_result),
            daemon=True,
            name="input-stream",
        )
        self._input_thread.start()
        try:
            result = startup_result.get(timeout=timeout)
        except queue.Empty as exc:
            self._input_stop_event.set()
            self._input_thread.join(timeout=0.5)
            self._input_thread = None
            raise AudioInputError(
                "Audio input did not open within the startup timeout."
            ) from exc

        if result is not None:
            self._input_thread.join(timeout=0.5)
            self._input_thread = None
            raise AudioInputError(str(result)) from result

    def _sounddevice_input_loop(
        self,
        device: int,
        samplerate: int,
        stream_kwargs: dict,
        startup_result: queue.Queue[BaseException | None] | None,
        *,
        label: str,
    ) -> None:
        """Open a microphone with bounded retry and same-device fallbacks."""

        self._run_sounddevice_input_loop(
            device,
            samplerate,
            stream_kwargs,
            startup_result,
            stop_event=self.stop_event,
            input_stop=self._input_stop_event,
            label=label,
            track_current_device=True,
            report_runtime_error=True,
        )

    def _run_sounddevice_input_loop(
        self,
        device: int,
        samplerate: int,
        stream_kwargs: dict,
        startup_result: queue.Queue[BaseException | None] | None,
        *,
        stop_event: threading.Event,
        input_stop: threading.Event,
        label: str,
        track_current_device: bool,
        report_runtime_error: bool,
    ) -> None:
        """Shared PortAudio open/retry loop for sessions and local previews."""

        candidates = input_device_candidates(
            sd,
            device_index=device,
            samplerate=samplerate,
            channels=int(stream_kwargs.get("channels", 1)),
            dtype=stream_kwargs.get("dtype"),
        )
        last_error: BaseException | None = None

        for candidate in candidates:
            for attempt in range(1, INPUT_STREAM_OPEN_ATTEMPTS + 1):
                opened = False
                stream = None
                try:
                    kwargs = dict(stream_kwargs)
                    kwargs.update(input_stream_kwargs(sd, device_index=candidate))
                    stream = sd.InputStream(
                        samplerate=samplerate,
                        device=candidate,
                        **kwargs,
                    )
                    # Do not use ``with InputStream`` here: sounddevice's
                    # __enter__ calls start(), and Python never invokes
                    # __exit__ when that start raises. Explicit close avoids
                    # leaking the already-open PortAudio handle before retry.
                    stream.start()
                    if stop_event.is_set() or input_stop.is_set():
                        return
                    opened = True
                    if track_current_device:
                        self._current_device = candidate
                    if candidate != device:
                        log(
                            f"{label} using equivalent audio backend "
                            f"{candidate} after device {device} failed",
                            level="WARNING",
                        )
                    self._report_input_start(startup_result, None)
                    log(f"{label} started on device {candidate}", level="INFO")
                    while not stop_event.is_set() and not input_stop.is_set():
                        time.sleep(0.1)
                    log(f"{label} stopping on device {candidate}", level="DEBUG")
                    return
                except Exception as exc:
                    last_error = exc
                    if opened:
                        if track_current_device:
                            self._current_device = None
                        log(
                            f"Audio device error (device {candidate}): {exc}",
                            level="ERROR",
                        )
                        if (
                            report_runtime_error
                            and not stop_event.is_set()
                            and not input_stop.is_set()
                        ):
                            self.error_queue.put(f"audio_device_lost:{candidate}")
                        return

                    log(
                        f"{label} open attempt {attempt}/"
                        f"{INPUT_STREAM_OPEN_ATTEMPTS} failed on device "
                        f"{candidate}: {exc}",
                        level="WARNING",
                    )
                    if attempt < INPUT_STREAM_OPEN_ATTEMPTS:
                        if input_stop.wait(INPUT_STREAM_RETRY_DELAY_SECONDS * attempt):
                            break
                finally:
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception as close_exc:
                            log(
                                f"Error closing audio stream {candidate}: "
                                f"{close_exc}",
                                level="DEBUG",
                            )
                if stop_event.is_set() or input_stop.is_set():
                    break
            if stop_event.is_set() or input_stop.is_set():
                break

        error = last_error or RuntimeError("Audio input startup was cancelled.")
        if track_current_device:
            self._current_device = None
        log(f"Audio device error (device {device}): {error}", level="ERROR")
        if startup_result is not None:
            self._report_input_start(startup_result, error)
        elif (
            report_runtime_error
            and not stop_event.is_set()
            and not input_stop.is_set()
        ):
            self.error_queue.put(f"audio_device_lost:{device}")

    def _input_level_test_audio_callback(
        self, indata, frames, time_info, status
    ) -> None:
        if status:
            log(f"LEVEL-TEST-CALLBACK Status: {status}", level="DEBUG")
        self._observe_input_level(indata[:, 0], FS)

    def _input_level_test_capture_thread(
        self,
        device: int,
        test_stop: threading.Event,
        startup_result: queue.Queue[BaseException | None],
    ) -> None:
        speaker = get_loopback_speaker(device)
        if speaker is not None:
            self._loopback_input_level_test(
                device,
                speaker,
                test_stop,
                startup_result,
            )
            return

        self._run_sounddevice_input_loop(
            device,
            FS,
            {
                "channels": 1,
                "callback": self._input_level_test_audio_callback,
            },
            startup_result,
            stop_event=test_stop,
            input_stop=test_stop,
            label="Input level test",
            track_current_device=False,
            report_runtime_error=False,
        )

    def _loopback_input_level_test(
        self,
        device: int,
        speaker,
        test_stop: threading.Event,
        startup_result: queue.Queue[BaseException | None],
    ) -> None:
        """Capture loopback only for the local input-level preview."""

        started = False
        try:
            import soundcard as sc  # noqa: PLC0415

            block_frames = int(FS * 0.1)
            mic = sc.get_microphone(id=str(speaker.id), include_loopback=True)
            with mic.recorder(
                samplerate=FS, channels=2, blocksize=block_frames * 4
            ) as recorder:
                started = True
                self._report_input_start(startup_result, None)
                log(
                    f"Input level test started for loopback '{speaker.name}'",
                    level="INFO",
                )
                while not test_stop.is_set():
                    data = recorder.record(numframes=block_frames)
                    mono = data.mean(axis=1).astype(np.float32)
                    self._observe_input_level(mono, FS)
                log("Input level test loopback stopping", level="DEBUG")
        except Exception as exc:
            log(
                f"Input level test error (device {device}): {exc}",
                level="ERROR",
            )
            if not started:
                self._report_input_start(startup_result, exc)

    def _input_stream_thread(
        self,
        device: int,
        startup_result: queue.Queue[BaseException | None] | None = None,
    ):
        speaker = get_loopback_speaker(device)
        if speaker is not None:
            self._loopback_segmented_loop(
                device,
                speaker,
                startup_result=startup_result,
            )
            return
        self._sounddevice_input_loop(
            device,
            FS,
            {
                "channels": 1,
                "callback": self._segmented_audio_callback,
            },
            startup_result,
            label="InputStream",
        )

    def _loopback_segmented_loop(
        self,
        device: int,
        speaker,
        *,
        startup_result: queue.Queue[BaseException | None] | None = None,
    ) -> None:
        """Capture loopback audio from an output device into the ring buffer."""
        stop_event = self.stop_event  # session-local: see _process_audio
        input_stop = self._input_stop_event
        started = False
        try:
            import soundcard as sc  # noqa: PLC0415

            block_frames = int(FS * 0.1)  # 100 ms
            mic = sc.get_microphone(id=str(speaker.id), include_loopback=True)
            # channels=2: avoids the soundcard/WASAPI single-channel-garbage bug
            with mic.recorder(
                samplerate=FS, channels=2, blocksize=block_frames * 4
            ) as recorder:
                started = True
                self._report_input_start(startup_result, None)
                log(f"Loopback recorder started for '{speaker.name}'", level="INFO")
                while not stop_event.is_set() and not input_stop.is_set():
                    data = recorder.record(numframes=block_frames)
                    # Mix stereo to mono
                    chunk = data.mean(axis=1).astype(np.float32)
                    self._observe_input_level(chunk, FS)
                    write_samples_to_ring(chunk)
                log("Loopback recorder stopping", level="DEBUG")
        except Exception as e:
            log(f"Loopback device error (device {device}): {e}", level="ERROR")
            if not started and startup_result is not None:
                self._report_input_start(startup_result, e)
            elif not stop_event.is_set() and not input_stop.is_set():
                self.error_queue.put(f"audio_device_lost:{device}")

    def _streaming_audio_callback(self, indata, frames, time_info, status):
        if status:
            log(f"STREAMING-CALLBACK Status: {status}", level="DEBUG")
        mono = indata[:, 0]
        self._observe_input_level(mono, self._streaming_capture_rate)
        try:
            self._streaming_feed_queue.put_nowait(mono.tobytes())
        except queue.Full:
            pass  # unbounded by default; defensive only

    def _streaming_input_stream_thread(
        self,
        device: int,
        samplerate: int = FS,
        startup_result: queue.Queue[BaseException | None] | None = None,
    ):
        speaker = get_loopback_speaker(device)
        if speaker is not None:
            self._loopback_streaming_loop(
                device,
                speaker,
                samplerate,
                startup_result=startup_result,
            )
            return
        # The capture rate is engine-specific (Deepgram is told FS at connect;
        # OpenAI Realtime only accepts 24 kHz PCM).
        chunk_frames = max(1, int(samplerate * STREAMING_CHUNK_MS / 1000))
        self._sounddevice_input_loop(
            device,
            samplerate,
            {
                "channels": 1,
                "dtype": "int16",
                "blocksize": chunk_frames,
                "callback": self._streaming_audio_callback,
            },
            startup_result,
            label="Streaming InputStream",
        )

    def _loopback_streaming_loop(
        self,
        device: int,
        speaker,
        samplerate: int,
        *,
        startup_result: queue.Queue[BaseException | None] | None = None,
    ) -> None:
        """Feed loopback audio from an output device into the streaming pipeline."""
        stop_event = self.stop_event  # session-local: see _process_audio
        input_stop = self._input_stop_event
        started = False
        try:
            import soundcard as sc  # noqa: PLC0415

            chunk_frames = max(1, int(samplerate * STREAMING_CHUNK_MS / 1000))
            buffer_frames = max(
                chunk_frames, int(samplerate * LOOPBACK_CAPTURE_BUFFER_SECONDS)
            )
            mic = sc.get_microphone(id=str(speaker.id), include_loopback=True)
            with mic.recorder(
                samplerate=samplerate, channels=2, blocksize=buffer_frames
            ) as recorder:
                started = True
                self._report_input_start(startup_result, None)
                log(
                    f"Loopback streaming recorder started for '{speaker.name}' "
                    f"at {samplerate} Hz",
                    level="INFO",
                )
                while not stop_event.is_set() and not input_stop.is_set():
                    data = recorder.record(numframes=chunk_frames)
                    mono = data.mean(axis=1)  # stereo -> mono
                    self._observe_input_level(mono, samplerate)
                    # Convert float32 [-1,1] to int16 bytes (engine expects PCM16)
                    pcm = (mono * 32767).clip(-32768, 32767).astype(np.int16)
                    try:
                        self._streaming_feed_queue.put_nowait(pcm.tobytes())
                    except Exception:
                        pass
                log("Loopback streaming recorder stopping", level="DEBUG")
        except Exception as e:
            log(
                f"Loopback streaming device error (device {device}): {e}",
                level="ERROR",
            )
            if not started and startup_result is not None:
                self._report_input_start(startup_result, e)
            elif not stop_event.is_set() and not input_stop.is_set():
                self.error_queue.put(f"audio_device_lost:{device}")

    def _streaming_feeder_thread(self):
        stop_event = self.stop_event  # session-local: see _process_audio
        while not stop_event.is_set():
            try:
                chunk = self._streaming_feed_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            # Local capture: the reconnect supervisor nulls/replaces the
            # handle concurrently during an outage — chunks captured while
            # the connection is down are dropped (replaying them later would
            # burst stale text onto live subtitles).
            handle = self._streaming_handle
            if handle is not None:
                # Noise gate: sustained non-speech (static from a muted mixer
                # channel) is fed as digital silence so the engine's server
                # VAD stops inventing turns. The settings read is cached, so
                # the checkbox toggles this live mid-session.
                gate = self._noise_gate
                if gate is not None and load_settings().noise_filter:
                    chunk = gate.process(chunk)
                handle.feed(chunk)

    def _handle_terminal_stream_error(
        self,
        exc: Exception,
        session: _StreamingUtteranceSession | None = None,
    ) -> bool:
        """Stop a stream that cannot recover without operator action.

        Invalid credentials do not improve with exponential backoff. Mark the
        session for GUI-side shutdown, close the current socket and emit one
        machine-readable error. The raw provider exception deliberately does
        not enter either queue because it may contain a masked key fragment.
        """
        error_kind = classify_error(exc)
        if error_kind != "invalid_api_key":
            return False

        if self._streaming_fatal_error is None:
            self._streaming_fatal_error = error_kind
            self.error_queue.put(f"fatal_transcription_error:{error_kind}")

        active_session = session or self._streaming_session
        if active_session is not None:
            active_session.clear_live()

        # Quiesce every streaming worker while the GUI consumes the fatal
        # event and performs the normal controller.stop() cleanup. Crucially,
        # never wake the reconnect supervisor for an authentication failure.
        self._streaming_outage = True
        self._streaming_connect = None
        self._streaming_reconnect_event.clear()
        self._input_stop_event.set()
        self.stop_event.set()

        handle = self._streaming_handle
        self._streaming_handle = None
        if handle is not None:
            try:
                handle.close()
            except Exception as close_exc:
                log(
                    f"STREAMING error closing terminally failed handle: {close_exc}",
                    level="DEBUG",
                )
        return True

    def _streaming_reconnect_supervisor(self):
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
        stop_event = self.stop_event  # session-local: see _process_audio
        reconnect_event = self._streaming_reconnect_event
        while not stop_event.is_set():
            if not reconnect_event.wait(timeout=0.2):
                continue
            # Back off BEFORE reconnecting: paces retry storms and coalesces
            # the duplicate error callbacks one disconnect can produce.
            delay = self._streaming_backoff
            self._streaming_backoff = min(delay * 2, STREAMING_RECONNECT_MAX_SECONDS)
            if stop_event.wait(delay):
                break
            reconnect_event.clear()
            old = self._streaming_handle
            self._streaming_handle = None  # feeder drops chunks while down
            if old is not None:
                try:
                    old.close()
                except Exception as e:
                    log(f"STREAMING error closing dead handle: {e}", level="DEBUG")
            connect = self._streaming_connect
            if connect is None:
                break  # stop() already tore the session down
            try:
                self._streaming_handle = connect()
                log(
                    f"STREAMING Reconnected after {delay:.0f}s backoff — "
                    "new connection opened.",
                    level="INFO",
                )
            except Exception as e:
                if self._handle_terminal_stream_error(e):
                    break
                # open_stream rarely raises (connect failures arrive async
                # via on_error) — but if it does, queue another attempt.
                log(f"STREAMING Reconnect attempt failed: {e}", level="WARNING")
                reconnect_event.set()
        log("STREAMING reconnect supervisor ended", level="DEBUG")

    def _streaming_stall_watchdog(self):
        """Silently reopen the connection if it stays up but stops producing
        transcripts.

        Observed live: the connection reports no error and the reconnect
        supervisor above never fires, yet no interim/final transcript arrives
        for 15-26s straight during continuous speech, and content from that
        stretch is lost. Unlike an error-triggered reconnect, this never
        shows an audience-facing message and never backs off — a real long
        pause in speech looks identical from here, and reconnecting during
        genuine silence has no visible cost, so a false trigger is harmless.
        """
        stop_event = self.stop_event  # session-local: see _process_audio
        while not stop_event.wait(timeout=2.0):
            if self._streaming_outage or self._streaming_handle is None:
                continue  # the error-triggered supervisor already owns recovery
            if (
                time.time() - self._last_pipeline_activity
                <= STREAMING_STALL_TIMEOUT_SECONDS
            ):
                continue
            log(
                f"STREAMING No transcript for over "
                f"{STREAMING_STALL_TIMEOUT_SECONDS:.0f}s with no error reported "
                "— reconnecting silently in case the session is stuck.",
                level="WARNING",
            )
            old = self._streaming_handle
            self._streaming_handle = None  # feeder drops chunks during the swap
            if old is not None:
                try:
                    old.close()
                except Exception as e:
                    log(f"STREAMING error closing stalled handle: {e}", level="DEBUG")
            connect = self._streaming_connect
            if connect is None:
                break  # stop() already tore the session down
            try:
                self._streaming_handle = connect()
                # Restart the timer now, not on the next transcript — otherwise
                # a connection that also takes a few seconds to speak would
                # trip the watchdog again before it gets a chance.
                self._last_pipeline_activity = time.time()
                log("STREAMING Silently reconnected after a stall.", level="INFO")
            except Exception as e:
                log(f"STREAMING Silent reconnect attempt failed: {e}", level="WARNING")
        log("STREAMING stall watchdog ended", level="DEBUG")

    def _process_streaming_utterances(self, session: _StreamingUtteranceSession):
        context_mgr = get_context_manager()
        stop_event = self.stop_event  # session-local: see _process_audio

        def _emit(trans_text: str, live_rev: int) -> None:
            # Mirror _process_audio's per-item recovery: an unexpected error
            # must show a subtitle and keep the thread alive, not silently
            # kill all subtitles for the rest of the session.
            try:
                self._translate_and_queue(
                    context_mgr, trans_text, log_prefix="STREAMING-PROCESSOR"
                )
            except Exception as e:
                log(f"STREAMING-PROCESSOR Error: {e}", level="ERROR")
                self.error_queue.put(f"translation_error:{e}")
                self.translation_queue.put((get_user_message(classify_error(e)), None))
            finally:
                # The subtitle (or error message) for this utterance is out —
                # take its live transcript off screen unless newer speech has
                # already replaced it.
                session.clear_live_if_unchanged(live_rev)

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
                session.clear_live_if_unchanged(pending_rev)

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
                trans_text, live_rev = self._streaming_utterance_queue.get(timeout=0.2)
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
                session.has_pending()
                and session.seconds_since_first_part() > STREAMING_MAX_UTTERANCE_SECONDS
            ):
                capped, live_rev = session.take_and_reset()
                if capped.strip():
                    log("STREAMING-PROCESSOR Max-utterance flush", level="DEBUG")
                    _accept(capped, live_rev)
                    _flush_pending()  # long text: emit now, do not keep holding

        # Stop: merge any un-ended session tail into the held clause, flush all.
        remaining, live_rev = session.take_and_reset()
        if remaining.strip():
            _accept(remaining, live_rev)
        _flush_pending()

    def _start_streaming_threads(self, input_device: int, settings) -> None:
        """Open the streaming connection and spawn the streaming threads.

        Raises ValueError for conditions the GUI's on_start() already catches
        and shows to the user (same pattern as any other start() failure).
        Local validation and the provider's startup handshake complete before
        the context manager or audio workers start, so a rejected connection
        leaves no background pipeline behind.
        """
        provider_id = settings.transcription_provider
        lang_code = get_source_language_code(settings.source_language)
        if not lang_code:
            raise ValueError(
                "Real-time streaming mode needs a specific source language "
                "(not Automatic) — the streaming engines do not auto-detect "
                "the way the default transcription models do."
            )
        key_provider = get_streaming_key_provider(provider_id)
        key_name = {"deepgram": "Deepgram", "openai": "OpenAI", "gemini": "Gemini"}.get(
            key_provider, key_provider
        )
        if not has_usable_key(key_provider):
            raise ValueError(
                f"Real-time streaming mode needs a {key_name} API key. Add "
                "one in Advanced Settings before starting."
            )
        if lang_code != "ar":
            log(
                "STREAMING RAG/Athan Arabic-hint matching is unavailable for "
                "non-Arabic source languages in this phase (P7 phase 1).",
                level="INFO",
            )

        # Callbacks close over the session object directly (not the
        # self._streaming_session attribute) so a late provider message after
        # stop() nulls the attribute can never hit None — it just lands in an
        # abandoned session.
        session = _StreamingUtteranceSession()

        def _on_transcript(text: str, is_final: bool) -> None:
            if not text.strip():
                return
            self._last_pipeline_activity = time.time()
            if self._streaming_outage:
                # Proof of life after a reconnect: end the outage and reset
                # the backoff for the next disconnect.
                self._streaming_outage = False
                self._streaming_backoff = STREAMING_RECONNECT_BASE_SECONDS
            if is_final:
                session.add_final(text)
            else:
                session.set_interim(text)

        def _on_utterance_end() -> None:
            text, live_rev = session.take_and_reset()
            if text.strip():
                self._streaming_utterance_queue.put((text, live_rev))

        def _on_stream_error(exc: Exception, generation: int) -> None:
            if generation != self._streaming_generation:
                # Late callback from an already-replaced connection (one
                # disconnect can produce several) — the reconnect it asks
                # for already happened.
                log(f"STREAMING stale connection error ignored: {exc}", level="DEBUG")
                return
            log(f"STREAMING connection error ({provider_id}): {exc}", level="ERROR")
            if self._handle_terminal_stream_error(exc, session):
                return
            self.error_queue.put(f"transcription_error:{exc}")
            # The stream is dead — no more interims will correct the live
            # line, so take it down rather than leave stale text standing.
            session.clear_live()
            if not self._streaming_outage:
                # One audience-facing message per outage; the supervisor's
                # retries must not stack further error subtitles.
                self._streaming_outage = True
                self.translation_queue.put(
                    (get_user_message(classify_error(exc)), None)
                )
            self._streaming_reconnect_event.set()

        # Always created; the feeder consults the noise_filter setting per
        # chunk, so the settings checkbox toggles the gate live mid-session.
        self._streaming_capture_rate = get_streaming_capture_sample_rate(provider_id)
        self._noise_gate = StreamNoiseGate(self._streaming_capture_rate)

        streaming_model = resolve_streaming_transcription_model(
            provider_id, settings.transcription_model
        )
        log(f"Streaming transcription model: {streaming_model}", level="INFO")

        # Fresh reconnect state per session (the generation counter is
        # deliberately NOT reset — see __init__).
        self._streaming_reconnect_event = threading.Event()
        self._streaming_backoff = STREAMING_RECONNECT_BASE_SECONDS
        self._streaming_outage = False
        self._streaming_fatal_error = None
        streaming_provider = get_streaming_transcription_provider()

        def _connect():
            # Bump the generation BEFORE opening: an immediate connect error
            # from the new receive thread must count as current, not stale.
            self._streaming_generation += 1
            generation = self._streaming_generation
            return streaming_provider.open_stream(
                model=streaming_model,
                language=lang_code,
                on_transcript=_on_transcript,
                on_utterance_end=_on_utterance_end,
                on_error=lambda exc, gen=generation: _on_stream_error(exc, gen),
            )

        self._streaming_connect = _connect
        try:
            self._streaming_handle = _connect()
        except Exception:
            # OpenAI Realtime waits for the server's session confirmation, so
            # an invalid key is now a synchronous startup failure. Leave no
            # half-started state and let AppGUI show the normal Start error.
            self._streaming_connect = None
            self._streaming_handle = None
            self._streaming_session = None
            raise
        self._streaming_session = session

        try:
            self._start_confirmed_input_thread(
                self._streaming_input_stream_thread,
                (
                    input_device,
                    self._streaming_capture_rate,
                ),
            )

            context_mgr = get_context_manager()
            context_mgr.reset()
            context_mgr.start()
        except Exception:
            # A provider connection may already be open, but a session is not
            # live until the local microphone has opened too. Roll every
            # startup side effect back before returning the error to the GUI.
            self._input_stop_event.set()
            if self._input_thread is not None:
                self._input_thread.join(timeout=0.5)
                self._input_thread = None
            self._streaming_generation += 1
            handle = self._streaming_handle
            self._streaming_connect = None
            self._streaming_handle = None
            self._streaming_session = None
            self._noise_gate = None
            self._current_device = None
            if handle is not None:
                try:
                    handle.close()
                except Exception as close_exc:
                    log(
                        f"Error closing stream after audio startup failure: "
                        f"{close_exc}",
                        level="DEBUG",
                    )
            raise

        thread_defs = [
            (self._streaming_feeder_thread, (), "streaming-feeder"),
            (self._process_streaming_utterances, (session,), "streaming-processor"),
            (self._streaming_reconnect_supervisor, (), "streaming-supervisor"),
            (self._streaming_stall_watchdog, (), "streaming-stall-watchdog"),
        ]
        for target, args, name in thread_defs:
            t = threading.Thread(target=target, args=args, daemon=True, name=name)
            self.threads.append(t)
            t.start()

    def start(self, input_device: int | None = None):
        if self._running:
            return

        # A meter-only preview owns the same OS device. Release it before the
        # real pipeline attempts its synchronously-confirmed open.
        self.stop_input_level_test()
        if (
            self._input_level_test_thread is not None
            and self._input_level_test_thread.is_alive()
        ):
            raise AudioInputError("The input-level test did not stop in time.")
        self.stop_event = threading.Event()
        self._input_stop_event = threading.Event()
        self.threads = []

        # Reset shared audio state to ensure clean start
        reset_ring_buffer()
        self.reset_input_level()
        clear_write_queue()

        # Also clear the translation queue and any leftover streaming state —
        # an utterance flushed right as the previous session stopped must not
        # be replayed into this one (possibly under a different language pair)
        for q in (
            self.translation_queue,
            self._streaming_feed_queue,
            self._streaming_utterance_queue,
        ):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

        # Clean up any leftover audio files from previous session
        try:
            for f in os.listdir(AUDIO_DIR):
                if f.endswith(".wav") or f.endswith(".tmp"):
                    try:
                        os.remove(os.path.join(AUDIO_DIR, f))
                    except Exception:
                        pass
        except Exception as e:
            log(f"Error cleaning up audio files: {e}", level="DEBUG")

        if input_device is None:
            input_device = get_default_input_device()

        self._current_device = input_device
        log(f"Using input device index: {input_device}", level="INFO")

        # A session with no speech at all should still auto-stop 10 min in.
        self._last_pipeline_activity = time.time()

        # Log the provider and the models actually in use (the settings values
        # may belong to a different provider and would then be ignored)
        settings = load_settings()
        log(f"AI provider: {settings.ai_provider}", level="INFO")
        log(f"Translation model: {get_translation_model_chain()[0]}", level="INFO")

        if settings.pipeline_mode == PIPELINE_MODE_STREAMING:
            log(
                "Pipeline mode: STREAMING "
                f"({settings.transcription_provider} real-time, beta)",
                level="INFO",
            )
            self._start_streaming_threads(input_device, settings)
            self._running = True
            return

        log(
            f"Transcription model: {get_transcription_model_chain()[0]}",
            level="INFO",
        )

        # Initialize processing strategy
        if settings.processing_strategy == "semantic":
            self.strategy = SemanticBufferingStrategy()
            log("Using SEMANTIC buffering strategy", level="INFO")
        else:
            self.strategy = ChunkBasedStrategy()
            log("Using CHUNK-based strategy", level="INFO")

        self.strategy.reset()

        context_mgr = get_context_manager()
        context_started = False
        try:
            # Confirm that the OS really opened the microphone before the GUI
            # is allowed to transition to its live state.
            self._start_confirmed_input_thread(
                self._input_stream_thread,
                (input_device,),
            )

            # Start context manager (for async summarization)
            context_mgr.reset()  # Fresh context for new session
            context_mgr.start()
            context_started = True
        except Exception:
            self._input_stop_event.set()
            if self._input_thread is not None:
                self._input_thread.join(timeout=0.5)
                self._input_thread = None
            if context_started:
                try:
                    context_mgr.stop(timeout=0.5)
                except Exception:
                    pass
            self.strategy = None
            self._current_device = None
            raise

        # Start other threads
        thread_defs = [
            (segment_writer, (self.stop_event,), "segment-writer"),
            (async_write_audio, (self.stop_event,), "audio-writer"),
            (self._process_audio, (), "audio-processor"),
        ]

        for target, args, name in thread_defs:
            t = threading.Thread(target=target, args=args, daemon=True, name=name)
            self.threads.append(t)
            t.start()

        self._running = True

    def stop(self, timeout: float = 2.0):
        self.stop_input_level_test(timeout=min(timeout, 1.0))
        if not self._running:
            return

        self.stop_event.set()
        self._input_stop_event.set()  # Also stop input stream

        # Stop context manager
        get_context_manager().stop(timeout=timeout)

        # Close the streaming connection if streaming mode was active, so its
        # receive thread can exit. References are cleared only after the
        # joins below — the processor thread's final flush still needs them.
        if self._streaming_handle is not None:
            try:
                self._streaming_handle.close()
            except Exception as e:
                log(f"Error closing streaming handle: {e}", level="DEBUG")

        # Join input thread
        if self._input_thread is not None:
            try:
                self._input_thread.join(timeout=timeout)
            except Exception as e:
                log(f"Error joining input thread: {e}", level="DEBUG")
            self._input_thread = None

        # Join other threads
        for t in self.threads:
            try:
                t.join(timeout=timeout)
            except Exception as e:
                log(f"Error joining thread {t.name}: {e}", level="DEBUG")

        self.strategy = None
        self._streaming_handle = None
        self._streaming_session = None
        self._streaming_connect = None
        self._noise_gate = None
        self._current_device = None
        self._running = False
        self.reset_input_level()

    def restart(self, input_device: int | None = None) -> None:
        """Stop and re-start the pipeline so settings that can't change on a
        live connection take effect.

        In streaming mode the engine fixes the source language and
        transcription model when the WebSocket opens, so changing either
        (or the engine itself) means reconnecting.
        Segmented mode re-reads those per audio segment and never needs this.
        A brief audio gap is expected (same as a manual Stop → Start).
        """
        if not self._running:
            return
        self.stop()
        self.start(input_device=input_device)

    def get_live_transcript(self) -> tuple[str, bool]:
        """In-progress (not yet translated) streaming transcript for the
        live subtitle line as (text, settled) — settled means the utterance
        is finished and its translation is in flight. ("", False) when idle
        or in segmented mode."""
        session = self._streaming_session
        return session.get_live_state() if session is not None else ("", False)

    def seconds_since_last_activity(self) -> float:
        """Seconds since the last transcription arrived (either pipeline
        mode). The GUI polls this for the inactivity auto-stop."""
        return time.time() - self._last_pipeline_activity

    def change_input_device(self, new_device: int, timeout: float = 1.0) -> bool:
        """
        Hot-swap the input device without stopping the rest of the pipeline.

        Both pipeline modes only need the capture thread replaced: in
        streaming mode the connection stays open and keeps its original
        capture rate (_streaming_capture_rate), so the new thread must be
        started with that same rate rather than re-deriving it.

        Args:
            new_device: New device index to switch to.
            timeout: Max time to wait for old stream to close.

        Returns:
            True if switch succeeded, False otherwise.
        """
        if not self._running:
            log("Cannot change device: not running", level="WARNING")
            return False

        if new_device == self._current_device:
            log(f"Device {new_device} already active, no change needed", level="DEBUG")
            return True

        log(
            f"Hot-swapping input device from {self._current_device} to {new_device}",
            level="INFO",
        )

        # Stop the current input stream thread
        self._input_stop_event.set()
        if self._input_thread is not None:
            try:
                self._input_thread.join(timeout=timeout)
            except Exception as e:
                log(f"Error joining old input thread: {e}", level="DEBUG")

        # Reset and start new input stream
        self._input_stop_event = threading.Event()
        self._current_device = new_device
        self.reset_input_level()
        try:
            if self._streaming_handle is not None:
                self._start_confirmed_input_thread(
                    self._streaming_input_stream_thread,
                    (new_device, self._streaming_capture_rate),
                    timeout=max(timeout, 0.1),
                )
            else:
                self._start_confirmed_input_thread(
                    self._input_stream_thread,
                    (new_device,),
                    timeout=max(timeout, 0.1),
                )
        except Exception as exc:
            self._current_device = None
            self.reset_input_level()
            log(f"Input device switch failed for {new_device}: {exc}", level="ERROR")
            self.error_queue.put(f"audio_device_lost:{new_device}")
            return False

        log(f"Input device switched to {new_device}", level="INFO")
        return True

"""Thread lifecycle controller for starting/stopping the pipeline."""

from __future__ import annotations

import os
import queue
import threading
import time

import numpy as np
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
    usable_input_samplerate,
)
from audio.level_meter import AudioLevelMeter, AudioLevelSnapshot
from audio.loopback import get_speaker as get_loopback_speaker
from audio.resampler import StreamResampler
from audio.vad import has_speech
from audio.writer import async_write_audio, clear_write_queue, segment_writer
from config import (
    AUDIO_DIR,
    FS,
    LOOPBACK_CAPTURE_BUFFER_SECONDS,
    STREAMING_CHUNK_MS,
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
from streaming_session import StreamingSession
from translation import recitation
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


class AppController:
    def __init__(self):
        self.stop_event = threading.Event()
        self._input_stop_event = threading.Event()  # Separate stop for input stream
        self._input_thread: threading.Thread | None = None
        self._input_level_meter = AudioLevelMeter()
        self._input_level_test_stop_event = threading.Event()
        self._input_level_test_thread: threading.Thread | None = None
        self._current_device: int | None = None
        self.threads: list[threading.Thread] = []
        # Items are (display_text, source_text): source_text is the original
        # transcription for bilingual display, None when there is no separate
        # source (error messages, same-language mode).
        self.translation_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.error_queue: queue.Queue[str] = queue.Queue()
        self._running = False
        self.strategy: ProcessingStrategy | None = None
        # The live streaming connection and its workers (streaming mode only).
        # Built per start() and dropped by stop() — everything it owns dies
        # with it, so there is no per-session state to reset here. See
        # streaming_session.py.
        self._streaming: StreamingSession | None = None
        # Last time a transcription arrived (either pipeline mode) — the GUI
        # polls this for the inactivity auto-stop.
        self._last_pipeline_activity = time.time()

    def _process_audio(self):
        # Lazy import: scipy.io costs ~200 ms to import and is only needed once
        # audio is actually being processed, not at startup.
        import scipy.io.wavfile as wavfile  # noqa: PLC0415

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
        # Feed the output back, so the next call knows what the audience is
        # already reading. Skipped in same-language mode, which passes no
        # context at all. Suppressed empties never get here: an empty line is
        # not something to continue from.
        if not same_language:
            context_mgr.add_translation(translation)
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

    def _apply_capture_resampling(
        self,
        stream_kwargs: dict,
        *,
        open_rate: int,
        target_rate: int,
        channels: int,
    ) -> tuple[dict, StreamResampler | None]:
        """Wrap the callback to resample capture blocks to the pipeline rate.

        Returns the (possibly rewritten) stream kwargs and the resampler, or
        the kwargs unchanged with ``None`` when the device already runs at the
        pipeline rate (the common case, and every path on Windows/macOS)."""
        real_callback = stream_kwargs.get("callback")
        if open_rate == target_rate or real_callback is None:
            return stream_kwargs, None

        resampler = StreamResampler(open_rate, target_rate, channels)

        def _resampling_callback(
            indata, frames, time_info, status, _cb=real_callback, _rs=resampler
        ) -> None:
            out = _rs.process(indata)
            if out.shape[0]:
                _cb(out, out.shape[0], time_info, status)

        kwargs = dict(stream_kwargs)
        kwargs["callback"] = _resampling_callback
        blocksize = kwargs.get("blocksize")
        if blocksize:
            # blocksize is in frames at the stream's (open) rate; scale it so a
            # block still carries ~the same duration after resampling.
            kwargs["blocksize"] = max(1, round(blocksize * open_rate / target_rate))
        log(
            f"Capturing at {open_rate} Hz, resampling to {target_rate} Hz",
            level="INFO",
        )
        return kwargs, resampler

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

        channels = int(stream_kwargs.get("channels", 1))
        # Devices that cannot run at the pipeline rate (Linux/PipeWire exposes
        # named sources only at their native 48 kHz via JACK) are opened at a
        # supported rate and resampled to `samplerate` before the callback, so
        # everything downstream still sees the pipeline rate.
        open_rate = (
            usable_input_samplerate(
                sd, device_index=device, requested=samplerate, channels=channels
            )
            or samplerate
        )
        stream_kwargs, resampler = self._apply_capture_resampling(
            stream_kwargs, open_rate=open_rate, target_rate=samplerate, channels=channels
        )

        candidates = input_device_candidates(
            sd,
            device_index=device,
            samplerate=open_rate,
            channels=channels,
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
                    if resampler is not None:
                        resampler.reset()  # drop any state from a failed attempt
                    stream = sd.InputStream(
                        samplerate=open_rate,
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
        session = self._streaming
        if session is None:
            return  # capture outlived the session (stop, or a rolled-back start)
        mono = indata[:, 0]
        self._observe_input_level(mono, session.capture_rate)
        session.feed(mono.tobytes())

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
        session = self._streaming
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
                    session.feed(pcm.tobytes())
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
            # "an OpenAI" / "a Gemini": the article follows the provider name.
            article = "an" if key_name[:1].upper() in "AEIOU" else "a"
            raise ValueError(
                f"Real-time streaming mode needs {article} {key_name} API "
                "key. Add one in Advanced Settings before starting."
            )
        if lang_code != "ar":
            log(
                "STREAMING RAG/Athan Arabic-hint matching is unavailable for "
                "non-Arabic source languages in this phase (P7 phase 1).",
                level="INFO",
            )

        streaming_model = resolve_streaming_transcription_model(
            provider_id, settings.transcription_model
        )
        log(f"Streaming transcription model: {streaming_model}", level="INFO")

        session = StreamingSession(
            get_streaming_transcription_provider(),
            provider_id=provider_id,
            model=streaming_model,
            language=lang_code,
            capture_rate=get_streaming_capture_sample_rate(provider_id),
            stop_event=self.stop_event,
            # Read live, not captured: change_input_device() replaces the event.
            stop_input=lambda: self._input_stop_event.set(),
            translation_queue=self.translation_queue,
            error_queue=self.error_queue,
            translate=self._translate_streaming_text,
            on_activity=self._mark_pipeline_activity,
        )
        # A session that fails to open is simply never assigned: the state a
        # half-started connection used to leave on the controller now lives on
        # the discarded object, so there is nothing to unwind here.
        session.open()
        self._streaming = session

        try:
            self._start_confirmed_input_thread(
                self._streaming_input_stream_thread,
                (
                    input_device,
                    session.capture_rate,
                ),
            )

            context_mgr = get_context_manager()
            context_mgr.reset()
            recitation.reset()  # a recitation never spans two sessions
            context_mgr.start()
        except Exception:
            # A provider connection may already be open, but a session is not
            # live until the local microphone has opened too. Roll every
            # startup side effect back before returning the error to the GUI.
            self._input_stop_event.set()
            if self._input_thread is not None:
                self._input_thread.join(timeout=0.5)
                self._input_thread = None
            session.close_connection("startup-failed")
            self._streaming = None
            self._current_device = None
            raise

        self.threads.extend(session.start_workers())

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

        # Also clear the translation queue — a subtitle emitted right as the
        # previous session stopped must not be replayed into this one (possibly
        # under a different language pair). The streaming feed and utterance
        # queues need no draining: each StreamingSession builds its own.
        while not self.translation_queue.empty():
            try:
                self.translation_queue.get_nowait()
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
            recitation.reset()  # a recitation never spans two sessions
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

        # Tear the streaming connection down BEFORE joining: a supervisor or
        # watchdog blocked in a slow open_stream() would otherwise outlive the
        # join timeout and store a fresh handle that the cleanup below then
        # drops without closing — leaking an open, billed socket.
        # close_connection() takes the same lock the reconnect paths use, so it
        # captures one an in-flight swap just opened. Closing also lets the
        # streaming receive thread exit before the joins.
        if self._streaming is not None:
            self._streaming.close_connection("stopped")

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
        # The connection was closed above under the lock; the session is safe
        # to drop now that its workers are joined.
        self._streaming = None
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
        session = self._streaming
        return session.live_transcript() if session is not None else ("", False)

    def seconds_since_last_activity(self) -> float:
        """Seconds since the last transcription arrived (either pipeline
        mode). The GUI polls this for the inactivity auto-stop."""
        return time.time() - self._last_pipeline_activity

    def _mark_pipeline_activity(self) -> None:
        """A transcription arrived — restart the inactivity auto-stop clock."""
        self._last_pipeline_activity = time.time()

    def _translate_streaming_text(self, text: str) -> None:
        """Translate one streaming utterance and emit it to the subtitle queue.

        Raises on failure: the streaming processor turns that into an error
        subtitle, the way _process_audio does for a segment.
        """
        self._translate_and_queue(
            get_context_manager(), text, log_prefix="STREAMING-PROCESSOR"
        )

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
            # The session, not its handle: during an outage the handle is None
            # while the session is very much alive, and starting the segmented
            # capture thread there would feed the ring buffer nobody reads and
            # leave the reconnected stream silent for the rest of the session.
            if self._streaming is not None:
                self._start_confirmed_input_thread(
                    self._streaming_input_stream_thread,
                    (new_device, self._streaming.capture_rate),
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

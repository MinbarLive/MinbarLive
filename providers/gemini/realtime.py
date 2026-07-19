"""Gemini Live API streaming speech-to-text (input-transcription side channel).

Implements providers.base.StreamingTranscriptionProvider — the third streaming
backend (P7). Opens a Live session on the v1alpha Gemini Developer API and
uses ONLY input-audio transcription; the model itself is silenced (proactive
audio + a never-speak instruction, verified to produce zero audio output), so
the dialog session behaves like a streaming transcriber. The google-genai
live client is asyncio-only, so one daemon thread runs a private event loop;
``feed()`` marshals audio into it thread-safely — the rest of the app stays
thread-based.

Model whitelist (empirically probed against the live API, July 2026 — see
CLAUDE.md P7 phase 1.4): only the gemini-2.5-flash-native-audio family
streams input transcription incrementally AND emits turn-complete boundaries.
gemini-3.1-flash-live-preview delivers one batched transcript per utterance
with no turn boundary at all (unusable for subtitles), and
gemini-3.5-live-translate-preview is the translate-everything black box
(P7 phase 2 material) — both deliberately excluded from TRANSCRIPTION_MODELS.

Limitation: transcription language hints (language_codes) are rejected by the
Developer API (Enterprise-only), so the Live models auto-detect the spoken
language — the app's source-language setting still drives the translation
prompt as usual. Arabic auto-detection verified correct in the probe.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Callable

from config import FS, STREAMING_GEMINI_SILENCE_MS
from providers.gemini.client import get_live_client
from utils.cost_tracking import gemini_usage_values, record_live_usage_snapshot
from utils.logging import log

DEFAULT_REALTIME_MODEL = "gemini-2.5-flash-native-audio-latest"

# (display_name, model_id) choices for the streaming-model dropdown under the
# Gemini real-time engine. Deliberately ONLY the models that passed the
# July-2026 live probe (see module docstring).
TRANSCRIPTION_MODELS = [
    ("Gemini 2.5 Flash Native Audio", "gemini-2.5-flash-native-audio-latest"),
    (
        "Gemini 2.5 Native Audio (Dec 2025)",
        "gemini-2.5-flash-native-audio-preview-12-2025",
    ),
]

_AUDIO_MIME = f"audio/pcm;rate={FS}"


def _session_config() -> dict:
    """Live session config: transcription on, model output suppressed."""
    return {
        # Native-audio Live models only accept AUDIO response modality; with
        # proactive audio the model simply never answers (0 bytes in the
        # probe) — we discard anything it might still say.
        "response_modalities": ["AUDIO"],
        "system_instruction": "You are a silent transcription service. Never speak.",
        "proactivity": {"proactive_audio": True},
        "input_audio_transcription": {},
        "realtime_input_config": {
            "automatic_activity_detection": {
                "silence_duration_ms": STREAMING_GEMINI_SILENCE_MS
            }
        },
    }


class GeminiLiveStreamHandle:
    """Implements providers.base.StreamHandle."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._session = None
        self._ready = threading.Event()
        self._closed = threading.Event()
        # The receive thread, so callers (and tests) can join it after close().
        self._thread: threading.Thread | None = None

    def _bind(self, loop, queue, session) -> None:
        self._loop = loop
        self._queue = queue
        self._session = session
        self._ready.set()

    def feed(self, pcm_bytes: bytes) -> None:
        if self._closed.is_set():
            return
        # The receive thread needs a moment to open the session before the
        # first chunk can be sent; later calls return immediately.
        if not self._ready.wait(timeout=5) or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, pcm_bytes)
        except RuntimeError:
            pass  # loop already shut down

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._loop is None or self._session is None or self._loop.is_closed():
            return
        coro = self._session.close()
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except RuntimeError:
            coro.close()  # loop shut down mid-call; drop the never-run coroutine


class GeminiLiveTranscriptionProvider:
    """Implements providers.base.StreamingTranscriptionProvider."""

    def open_stream(
        self,
        *,
        model: str,
        language: str | None,
        on_transcript: Callable[[str, bool], None],
        on_utterance_end: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> GeminiLiveStreamHandle:
        handle = GeminiLiveStreamHandle()
        # UsageMetadata is cumulative for one physical Live connection.  A
        # reconnect gets a new id and is still aggregated into the same logical
        # MinbarLive Start -> Stop cost session by the global tracker.
        usage_stream_id = uuid.uuid4().hex
        if language:
            log(
                f"Gemini Live auto-detects the spoken language — the '{language}' "
                "hint is not forwarded (Developer API limitation).",
                level="DEBUG",
            )

        async def _main() -> None:
            client = get_live_client()
            audio_queue: asyncio.Queue = asyncio.Queue()
            async with client.aio.live.connect(
                model=model, config=_session_config()
            ) as session:
                handle._bind(asyncio.get_running_loop(), audio_queue, session)

                async def _sender() -> None:
                    while True:
                        chunk = await audio_queue.get()
                        await session.send_realtime_input(
                            audio={"data": chunk, "mime_type": _AUDIO_MIME}
                        )

                sender = asyncio.create_task(_sender())
                try:
                    # Transcription fragments accumulated within one turn.
                    accumulated = ""
                    # receive() ends at each turn boundary (SDK behavior);
                    # keep re-entering it until the socket actually closes.
                    # A pass that yields no messages at all means the
                    # connection is drained/closed — a live turn always
                    # carries at least its turn_complete message.
                    while not handle._closed.is_set():
                        got_any = False
                        async for message in session.receive():
                            got_any = True
                            metadata = getattr(message, "usage_metadata", None)
                            if metadata is not None:
                                record_live_usage_snapshot(
                                    stream_id=usage_stream_id,
                                    provider="gemini",
                                    role="transcription",
                                    model=model,
                                    usage=gemini_usage_values(
                                        metadata, role="transcription", live=True
                                    ),
                                )
                            content = getattr(message, "server_content", None)
                            if content is None:
                                continue
                            tx = content.input_transcription
                            if tx and tx.text:
                                accumulated += tx.text
                                if accumulated.strip():
                                    on_transcript(accumulated, False)
                            if content.turn_complete:
                                text = accumulated.strip()
                                accumulated = ""
                                if text:
                                    on_transcript(text, True)
                                on_utterance_end()
                        if not got_any:
                            break
                finally:
                    sender.cancel()

        def _run() -> None:
            try:
                asyncio.run(_main())
            except Exception as e:
                handle._ready.set()  # unblock any feed() calls waiting on us
                # A deliberate close() tears the session down mid-receive;
                # that is expected shutdown, not a stream failure.
                if not handle._closed.is_set():
                    on_error(e)
                return
            if not handle._closed.is_set():
                # The receive loop drained without close(): the server ended
                # the session. Report it so the controller's reconnect
                # supervisor can act — previously this died silently.
                on_error(RuntimeError("stream ended by server"))

        thread = threading.Thread(
            target=_run, daemon=True, name="gemini-live-receive"
        )
        handle._thread = thread
        thread.start()
        return handle

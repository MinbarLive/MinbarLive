"""OpenAI Realtime API streaming speech-to-text (transcription sessions).

Implements providers.base.StreamingTranscriptionProvider — the second
streaming backend alongside Deepgram (P7). Opens a transcription-only
Realtime session (``intent=transcription``) over the SDK's synchronous
WebSocket client; the receive loop (``for event in connection``) blocks, so
it runs in its own daemon thread and ``feed()`` is safe to call from a
different thread once the connection is open — same shape as the Deepgram
provider. Reuses the OpenAI client singleton, and therefore the existing
OpenAI API key (no separate key entry).

Event mapping onto the StreamingTranscriptionProvider callbacks:
- ``...transcription.delta`` events are append-only text fragments (unlike
  Deepgram's replace-the-hypothesis interims), accumulated per conversation
  item and reported as a growing interim transcript.
- ``...transcription.completed`` carries the full transcript of one server-VAD
  turn — reported as the final transcript, immediately followed by the
  utterance-end signal (one completed turn == one utterance; the API has no
  separate utterance-end event).
"""

from __future__ import annotations

import base64
import threading
import time
from collections.abc import Callable

from providers.openai.client import get_client
from utils.cost_tracking import record_openai_transcription_usage
from utils.logging import log

# Fixed by the API: Realtime transcription sessions accept mono PCM16 at a
# 24 kHz sample rate only (the SDK's AudioPCM type pins rate=24000), so the
# streaming capture runs at this rate instead of the pipeline-wide 16 kHz FS.
CAPTURE_SAMPLE_RATE = 24000

DEFAULT_REALTIME_MODEL = "gpt-4o-transcribe"

# (display_name, model_id) choices for the streaming-model dropdown under the
# OpenAI real-time engine. Same STT models as segmented mode — the Realtime
# API runs them server-side against the live audio buffer.
TRANSCRIPTION_MODELS = [
    ("GPT-4o Transcribe", "gpt-4o-transcribe"),
    ("GPT-4o Mini Transcribe", "gpt-4o-mini-transcribe"),
]

# ``client.realtime.connect()`` only completes the WebSocket handshake.  The
# server confirms authentication and the effective session configuration in a
# subsequent event, so returning before one of these arrives makes a rejected
# key look like a successfully started stream for a few seconds.
#
# Keep the provider deadline strictly above the transport deadline.  When both
# were 10 seconds, a cold DNS/TLS/WebSocket setup could make our outer wait win
# the race and report a misleading "session confirmation" timeout before the
# SDK had a chance to either finish or surface its precise connection error.
WEBSOCKET_OPEN_TIMEOUT_SECONDS = 20.0
WEBSOCKET_CLOSE_TIMEOUT_SECONDS = 3.0
STARTUP_TIMEOUT_SECONDS = 30.0

_SESSION_READY_EVENTS = frozenset(
    (
        "session.created",
        "session.updated",
        # Compatibility with the legacy/Beta transcription-only lifecycle.
        # The current GA endpoint emits the unified ``session.*`` names.
        "transcription_session.created",
        "transcription_session.updated",
    )
)

_DELTA_EVENT = "conversation.item.input_audio_transcription.delta"
_COMPLETED_EVENT = "conversation.item.input_audio_transcription.completed"
_FAILED_EVENT = "conversation.item.input_audio_transcription.failed"


class OpenAIRealtimeStreamHandle:
    """Implements providers.base.StreamHandle."""

    def __init__(self) -> None:
        self._connection = None
        self._ready = threading.Event()
        self._closed = threading.Event()

    def _attach(self, connection) -> None:
        """Retain the socket so a startup timeout can close it cleanly."""
        self._connection = connection

        # close() may have run while the socket was still connecting.  This is
        # primarily the bounded-startup timeout path: attach and immediately
        # tear down instead of leaving an orphaned receive thread.
        if self._closed.is_set():
            try:
                connection.close()
            except Exception:
                pass

    def _mark_ready(self) -> None:
        """Allow audio only after the server has confirmed the session."""
        self._ready.set()

    def feed(self, pcm_bytes: bytes) -> None:
        if self._closed.is_set():
            return
        # The receive thread needs a moment to open the socket before the
        # first chunk can be sent; later calls return immediately.
        if not self._ready.wait(timeout=5) or self._connection is None:
            return
        try:
            self._connection.input_audio_buffer.append(
                audio=base64.b64encode(pcm_bytes).decode("ascii")
            )
        except Exception as e:
            log(f"OpenAI realtime audio append failed: {e}", level="WARNING")

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._connection is not None:
            try:
                self._connection.close()
            except Exception:
                pass


class OpenAIRealtimeTranscriptionProvider:
    """Implements providers.base.StreamingTranscriptionProvider."""

    def open_stream(
        self,
        *,
        model: str,
        language: str | None,
        on_transcript: Callable[[str, bool], None],
        on_utterance_end: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> OpenAIRealtimeStreamHandle:
        handle = OpenAIRealtimeStreamHandle()
        startup_done = threading.Event()
        startup_errors: list[Exception] = []

        def _fail_startup(exc: Exception) -> None:
            # Only the receive thread writes this list and open_stream reads it
            # after the Event synchronization point, so no additional lock is
            # needed.
            if not startup_done.is_set():
                startup_errors.append(exc)
                startup_done.set()

        def _run() -> None:
            started_at = time.monotonic()
            try:
                client = get_client()
                log("OpenAI realtime WebSocket connecting", level="DEBUG")
                with client.realtime.connect(
                    extra_query={"intent": "transcription"},
                    websocket_connection_options={
                        "open_timeout": WEBSOCKET_OPEN_TIMEOUT_SECONDS,
                        "close_timeout": WEBSOCKET_CLOSE_TIMEOUT_SECONDS,
                    },
                ) as connection:
                    handle._attach(connection)
                    log(
                        "OpenAI realtime WebSocket connected in "
                        f"{time.monotonic() - started_at:.2f}s",
                        level="DEBUG",
                    )
                    transcription: dict = {"model": model}
                    if language:
                        transcription["language"] = language
                    # Events are processed in order server-side, so audio
                    # appended right after this update is already transcribed
                    # with the requested model/language.
                    connection.session.update(
                        session={
                            "type": "transcription",
                            "audio": {
                                "input": {
                                    "format": {
                                        "type": "audio/pcm",
                                        "rate": CAPTURE_SAMPLE_RATE,
                                    },
                                    "transcription": transcription,
                                    "turn_detection": {"type": "server_vad"},
                                }
                            },
                        }
                    )
                    log("OpenAI realtime session update sent", level="DEBUG")
                    # Interim text accumulated per conversation item id.
                    deltas: dict[str, str] = {}
                    for event in connection:
                        etype = getattr(event, "type", "")
                        if etype in _SESSION_READY_EVENTS:
                            if not handle._ready.is_set():
                                handle._mark_ready()
                                startup_done.set()
                                log(
                                    "OpenAI realtime session confirmed by "
                                    f"{etype} in {time.monotonic() - started_at:.2f}s",
                                    level="DEBUG",
                                )
                        elif etype == _DELTA_EVENT:
                            if event.delta:
                                text = deltas.get(event.item_id, "") + event.delta
                                deltas[event.item_id] = text
                                if text.strip():
                                    on_transcript(text, False)
                        elif etype == _COMPLETED_EVENT:
                            deltas.pop(event.item_id, None)
                            record_openai_transcription_usage(
                                getattr(event, "usage", None),
                                model=model,
                                event_id=getattr(event, "event_id", None),
                            )
                            text = (event.transcript or "").strip()
                            if text:
                                on_transcript(text, True)
                            on_utterance_end()
                        elif etype == _FAILED_EVENT:
                            deltas.pop(event.item_id, None)
                            log(
                                "OpenAI realtime transcription failed for one "
                                f"utterance: {getattr(event, 'error', None)}",
                                level="WARNING",
                            )
                            # Flush so an accumulated interim can't linger on
                            # screen (an empty flush clears the live line).
                            on_utterance_end()
                        elif etype == "error":
                            err = getattr(event, "error", None)
                            msg = getattr(err, "message", None) or str(err or event)
                            exc = RuntimeError(msg)
                            if not handle._ready.is_set():
                                _fail_startup(exc)
                                return
                            on_error(exc)
            except Exception as e:
                # A deliberate close() tears the socket down mid-recv; that is
                # expected shutdown, not a stream failure.
                if handle._closed.is_set():
                    return
                if not handle._ready.is_set():
                    _fail_startup(e)
                else:
                    on_error(e)
                return
            if not handle._closed.is_set():
                # The event iterator ended without close(): the server closed
                # the session (the SDK swallows ConnectionClosedOK). Report it
                # so the controller's reconnect supervisor can act.
                exc = RuntimeError("stream ended by server")
                if not handle._ready.is_set():
                    _fail_startup(exc)
                else:
                    on_error(exc)

        thread = threading.Thread(
            target=_run, daemon=True, name="openai-realtime-receive"
        )
        thread.start()

        if not startup_done.wait(timeout=STARTUP_TIMEOUT_SECONDS):
            handle.close()
            raise TimeoutError(
                "OpenAI realtime startup timed out before session confirmation "
                f"after {STARTUP_TIMEOUT_SECONDS:g} seconds."
            )
        if startup_errors:
            handle.close()
            raise startup_errors[0]
        return handle

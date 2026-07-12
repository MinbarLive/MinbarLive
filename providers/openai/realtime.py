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
from collections.abc import Callable

from providers.openai.client import get_client
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

_DELTA_EVENT = "conversation.item.input_audio_transcription.delta"
_COMPLETED_EVENT = "conversation.item.input_audio_transcription.completed"
_FAILED_EVENT = "conversation.item.input_audio_transcription.failed"


class OpenAIRealtimeStreamHandle:
    """Implements providers.base.StreamHandle."""

    def __init__(self) -> None:
        self._connection = None
        self._ready = threading.Event()
        self._closed = threading.Event()

    def _bind(self, connection) -> None:
        self._connection = connection
        self._ready.set()
        # close() may have run while the socket was still connecting — with
        # no connection to act on, it couldn't close the stream, and the
        # receive loop would keep an orphaned socket alive. Close now.
        if self._closed.is_set():
            try:
                connection.close()
            except Exception:
                pass

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

        def _run() -> None:
            try:
                client = get_client()
                with client.realtime.connect(
                    extra_query={"intent": "transcription"}
                ) as connection:
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
                    handle._bind(connection)
                    # Interim text accumulated per conversation item id.
                    deltas: dict[str, str] = {}
                    for event in connection:
                        etype = getattr(event, "type", "")
                        if etype == _DELTA_EVENT:
                            if event.delta:
                                text = deltas.get(event.item_id, "") + event.delta
                                deltas[event.item_id] = text
                                if text.strip():
                                    on_transcript(text, False)
                        elif etype == _COMPLETED_EVENT:
                            deltas.pop(event.item_id, None)
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
                            on_error(RuntimeError(msg))
            except Exception as e:
                handle._ready.set()  # unblock any feed() calls waiting on us
                # A deliberate close() tears the socket down mid-recv; that is
                # expected shutdown, not a stream failure.
                if not handle._closed.is_set():
                    on_error(e)
                return
            if not handle._closed.is_set():
                # The event iterator ended without close(): the server closed
                # the session (the SDK swallows ConnectionClosedOK). Report it
                # so the controller's reconnect supervisor can act.
                on_error(RuntimeError("stream ended by server"))

        thread = threading.Thread(
            target=_run, daemon=True, name="openai-realtime-receive"
        )
        thread.start()
        return handle

"""Deepgram real-time streaming speech-to-text (Nova-3, WebSocket).

Implements providers.base.StreamingTranscriptionProvider — distinct from the
whole-segment TranscriptionProvider used by segmented/batch mode. The socket
receive loop (``connection.start_listening()``) blocks, so it runs in its own
daemon thread; ``DeepgramStreamHandle.feed()`` is safe to call from a
different thread once the connection is open.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from config import (
    FS,
    STREAMING_ENDPOINTING_MS,
    STREAMING_UTTERANCE_END_MS,
)
from providers.deepgram.client import get_client
from utils.logging import log

_ENCODING = "linear16"


class DeepgramStreamHandle:
    """Implements providers.base.StreamHandle."""

    def __init__(self) -> None:
        self._connection = None
        self._ready = threading.Event()
        self._closed = threading.Event()

    def _bind(self, connection) -> None:
        self._connection = connection
        self._ready.set()
        # close() may have run while the socket was still connecting — with
        # no connection to act on, it couldn't send the close message, and
        # the receive loop would block forever. Close now instead.
        if self._closed.is_set():
            try:
                connection.send_close_stream()
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
            self._connection.send_media(pcm_bytes)
        except Exception as e:
            log(f"Deepgram send_media failed: {e}", level="WARNING")

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        if self._connection is not None:
            try:
                self._connection.send_close_stream()
            except Exception:
                pass


class DeepgramTranscriptionProvider:
    """Implements providers.base.StreamingTranscriptionProvider."""

    def open_stream(
        self,
        *,
        model: str,
        language: str | None,
        on_transcript: Callable[[str, bool], None],
        on_utterance_end: Callable[[], None],
        on_error: Callable[[Exception], None],
    ) -> DeepgramStreamHandle:
        handle = DeepgramStreamHandle()

        def _run() -> None:
            from deepgram.core.events import EventType
            from deepgram.listen.v1.types.listen_v1results import ListenV1Results
            from deepgram.listen.v1.types.listen_v1utterance_end import (
                ListenV1UtteranceEnd,
            )

            def _on_message(message) -> None:
                if isinstance(message, ListenV1Results):
                    alternatives = (
                        message.channel.alternatives if message.channel else []
                    )
                    text = (alternatives[0].transcript if alternatives else "") or ""
                    if text.strip():
                        on_transcript(text, bool(message.is_final))
                    if message.speech_final:
                        on_utterance_end()
                elif isinstance(message, ListenV1UtteranceEnd):
                    on_utterance_end()

            def _on_socket_error(exc) -> None:
                on_error(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))

            try:
                client = get_client()
                with client.listen.v1.connect(
                    model=model,
                    encoding=_ENCODING,
                    sample_rate=FS,
                    channels=1,
                    language=language,
                    interim_results=True,
                    vad_events=True,
                    utterance_end_ms=STREAMING_UTTERANCE_END_MS,
                    endpointing=STREAMING_ENDPOINTING_MS,
                ) as connection:
                    connection.on(EventType.MESSAGE, _on_message)
                    connection.on(EventType.ERROR, _on_socket_error)
                    handle._bind(connection)
                    connection.start_listening()  # blocks until the socket closes
            except Exception as e:
                handle._ready.set()  # unblock any feed() calls waiting on us
                # A deliberate close() can tear the socket down mid-listen;
                # that is expected shutdown, not a stream failure.
                if not handle._closed.is_set():
                    on_error(e)
                return
            if not handle._closed.is_set():
                # start_listening returned without close(): the server ended
                # the stream (session cap, idle policy). Report it so the
                # controller's reconnect supervisor can act — previously this
                # died silently and subtitles just stopped.
                on_error(RuntimeError("stream ended by server"))

        thread = threading.Thread(target=_run, daemon=True, name="deepgram-receive")
        thread.start()
        return handle

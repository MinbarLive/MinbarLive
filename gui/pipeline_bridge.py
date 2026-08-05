"""Controller → GUI handoff as Qt signals.

A worker thread *blocks* on the controller's queue and emits a signal per item;
Qt delivers it to the GUI thread through the event loop (a queued connection
across threads). So there is no polling interval to tune, no latency floor of
half a poll period, and no timer job to leak on close — the panel used to poll
``translation_queue`` every 50 ms and ``error_queue`` every second, and a missed
cancellation of one of those jobs is what printed ``invalid command name
<lambda>`` on shutdown.

The controller knows nothing about this: it still just fills the queues. The one
exception is the in-progress live transcript, which it exposes as a getter
(``get_live_transcript``) rather than a queue, so that one really is sampled on
a timer.
"""

from __future__ import annotations

import queue
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from utils.logging import log
from utils.settings import PIPELINE_MODE_STREAMING

# How often the in-progress transcript is sampled. It is a pull API, so this
# one really is a poll; 100 ms matches the Tk panel and is imperceptible
# against speech.
LIVE_TEXT_INTERVAL_MS = 100
# Blocking get() timeout, so a stopping worker notices promptly.
_QUEUE_WAIT_S = 0.2


class PipelineBridge(QObject):
    """Emits pipeline output as Qt signals on the GUI thread."""

    translation = Signal(str, object)  # display text, source text or None
    pipeline_error = Signal(str)
    live_text = Signal(str, bool)  # text, settled
    audio_device_lost = Signal()

    def __init__(self, controller, parent: QObject | None = None):
        super().__init__(parent)
        self._controller = controller
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._live_timer = QTimer(self)
        self._live_timer.setInterval(LIVE_TEXT_INTERVAL_MS)
        self._live_timer.timeout.connect(self._sample_live_text)
        self._streaming = False
        self._show_interim = False

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, *, streaming: bool, show_interim: bool) -> None:
        # Kept apart: only one of the two can change mid-session, and folding
        # them into a single flag at start time is what made the live-transcript
        # switch inert until the next run.
        self._streaming = streaming
        self._show_interim = show_interim
        self._stop.clear()
        self._threads = [
            threading.Thread(
                target=self._drain,
                args=(self._controller.translation_queue, self._emit_translation),
                name="qt-translation-drain",
                daemon=True,
            ),
            threading.Thread(
                target=self._drain,
                args=(self._controller.error_queue, self._emit_error),
                name="qt-error-drain",
                daemon=True,
            ),
        ]
        for t in self._threads:
            t.start()
        self._sync_live_timer()

    def stop(self) -> None:
        self._live_timer.stop()
        self._stop.set()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []
        self.live_text.emit("", False)

    def set_show_interim(self, enabled: bool) -> None:
        """Turn the live transcript on or off during a running session.

        The Tk panel re-reads the setting on every poll tick, so its switch has
        always taken effect immediately; here the sampling timer is what starts
        and stops. Switching it off also clears the line already on the overlay
        — no further interims will arrive to replace it, so it would otherwise
        stay frozen on screen for the rest of the session.
        """
        if enabled == self._show_interim:
            return
        self._show_interim = enabled
        self._sync_live_timer()
        if not enabled:
            self.live_text.emit("", False)

    def _sync_live_timer(self) -> None:
        """Sample the live transcript only while it is wanted and streaming."""
        if self._threads and self._streaming and self._show_interim:
            self._live_timer.start()
        else:
            self._live_timer.stop()

    # ── workers ──────────────────────────────────────────────────────────
    def _drain(self, q: queue.Queue, emit) -> None:
        """Block on ``q`` until stopped, handing each item to ``emit``.

        Mirrors the controller's own per-item recovery: one bad item logs and
        the thread survives, rather than silently ending the session's output.
        """
        while not self._stop.is_set():
            try:
                item = q.get(timeout=_QUEUE_WAIT_S)
            except queue.Empty:
                continue
            try:
                emit(item)
            except Exception as exc:  # noqa: BLE001 - never kill the drain thread
                log(f"Qt bridge emit failed: {exc}", level="ERROR")

    def _emit_translation(self, item) -> None:
        text, source_text = item
        self.translation.emit(text, source_text)

    def _emit_error(self, item) -> None:
        error = str(item)
        if error.startswith("audio_device_lost:"):
            self.audio_device_lost.emit()
        else:
            log(f"Controller error: {error}", level="ERROR")
            self.pipeline_error.emit(error)

    def _sample_live_text(self) -> None:
        try:
            text, settled = self._controller.get_live_transcript()
        except Exception as exc:  # noqa: BLE001
            log(f"Live transcript sample failed: {exc}", level="DEBUG")
            return
        self.live_text.emit(text, settled)


def streaming_enabled(settings) -> bool:
    """Whether the live transcript line applies to this configuration."""
    return settings.pipeline_mode == PIPELINE_MODE_STREAMING

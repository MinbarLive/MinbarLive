"""Qt batch / file window: turn a recording into subtitles.

Port of ``gui/batch_view.py``. The pipeline itself is ``batch/processor.py``,
reused unchanged — this only picks a file, runs the processor off the GUI
thread and reports progress.

The processor is imported lazily so GUI startup does not pay for it, matching
the Tk view.
"""

from __future__ import annotations

import os
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from gui_qt.widgets import SegmentedControl
from utils.logging import log
from utils.settings import SOURCE_LANGUAGES, TARGET_LANGUAGE_NAMES

# (settings value, translation key, English fallback) per output format.
_OUTPUT_FORMATS = (
    ("srt", "batch_output_srt", "Subtitles (.srt)"),
    ("txt", "batch_output_text", "Transcript (.txt)"),
    ("both", "batch_output_both", "Both (.srt + .txt)"),
)


class _Worker(QObject):
    """Runs process_file on a plain thread and reports back as Qt signals."""

    progress = Signal(int, int)
    finished = Signal(str)  # output path, "" when cancelled
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, **kwargs) -> None:
        self.cancel_event.clear()

        def _run() -> None:
            try:
                from batch.processor import process_file  # lazy: heavy import

                path = process_file(
                    progress_callback=lambda done, total: self.progress.emit(
                        done, total
                    ),
                    cancel_event=self.cancel_event,
                    **kwargs,
                )
                self.finished.emit(path or "")
            except Exception as exc:  # noqa: BLE001 - reported to the operator
                log(f"Batch run failed: {exc}", level="ERROR")
                self.failed.emit(str(exc))

        self._thread = threading.Thread(target=_run, daemon=True, name="qt-batch")
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class BatchWindow(QDialog):
    def __init__(self, translate, settings, parent=None):
        super().__init__(parent)
        self._t = translate
        self.settings = settings
        self._input_path = ""
        self.setWindowTitle(self._t("batch_file", "File / Batch"))
        self.resize(620, 520)

        self.worker = _Worker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        subtitle = QLabel(
            self._t("batch_file_sub", "Create translated subtitles from a recording")
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        outer.addWidget(subtitle)

        outer.addWidget(self._file_card())
        outer.addWidget(self._options_card())

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        self.status = QLabel(self._t("batch_no_file", "No file selected"))
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton(self._t("batch_start", "Start"))
        self.start_btn.setObjectName("accent")
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setEnabled(False)
        self.cancel_btn = QPushButton(self._t("batch_cancel", "Cancel"))
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.cancel_btn)
        outer.addLayout(buttons)
        outer.addStretch(1)

    # ── build ────────────────────────────────────────────────────────────
    def _file_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(18, 16, 18, 16)
        self.pick_btn = QPushButton(self._t("batch_pick_file", "Choose file…"))
        self.pick_btn.clicked.connect(self._on_pick)
        self.file_label = QLabel(self._t("batch_no_file", "No file selected"))
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        row.addWidget(self.pick_btn)
        row.addWidget(self.file_label, 1)
        return card

    def _options_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(18, 16, 18, 16)
        form.setSpacing(12)

        self.source_combo = QComboBox()
        self.source_combo.addItems([name for name, _ in SOURCE_LANGUAGES])
        self._select(self.source_combo, self.settings.source_language)

        self.target_combo = QComboBox()
        self.target_combo.addItems(TARGET_LANGUAGE_NAMES)
        self._select(self.target_combo, self.settings.target_language)

        self.output_segment = SegmentedControl(
            [self._t(key, fallback) for _, key, fallback in _OUTPUT_FORMATS], 0
        )

        self.bilingual_check = QCheckBox(
            self._t("batch_bilingual_srt", "Bilingual subtitles (original + translation)")
        )

        form.addRow(
            QLabel(self._t("batch_source_language", "Spoken language")),
            self.source_combo,
        )
        form.addRow(
            QLabel(self._t("batch_target_language", "Subtitle language")),
            self.target_combo,
        )
        form.addRow(
            QLabel(self._t("batch_output_format", "Output")), self.output_segment
        )
        form.addRow(QLabel(""), self.bilingual_check)
        return card

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # ── actions ──────────────────────────────────────────────────────────
    def _on_pick(self) -> None:
        media = self._t("batch_media_files", "Audio/Video")
        all_files = self._t("batch_all_files", "All files")
        path, _ = QFileDialog.getOpenFileName(
            self,
            self._t("batch_pick_file", "Choose file…"),
            "",
            f"{media} (*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.mp4 *.mkv "
            f"*.mov *.avi *.webm);;{all_files} (*)",
        )
        if not path:
            return
        self._input_path = path
        self.file_label.setText(os.path.basename(path))
        self.status.setText("")
        self.start_btn.setEnabled(True)

    def _output_format(self) -> str:
        return _OUTPUT_FORMATS[self.output_segment.current_index()][0]

    def _on_start(self) -> None:
        if not self._input_path or self.worker.is_running():
            return
        self._set_running(True)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.worker.start(
            input_path=self._input_path,
            source_language=self.source_combo.currentText(),
            target_language=self.target_combo.currentText(),
            output_format=self._output_format(),
            bilingual_srt=self.bilingual_check.isChecked(),
        )

    def _on_cancel(self) -> None:
        self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self.status.setText(self._t("batch_cancelled", "Cancelled"))

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running and bool(self._input_path))
        self.cancel_btn.setEnabled(running)
        self.pick_btn.setEnabled(not running)

    # ── worker signals (delivered on the GUI thread) ─────────────────────
    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setValue(round(done * 100 / total))
        self.status.setText(
            self._t("batch_progress", "Segment {current}/{total}").format(
                current=done, total=total
            )
        )

    def _on_finished(self, path: str) -> None:
        self._set_running(False)
        self.progress.setVisible(False)
        if not path:
            # Cancelled: process_file writes nothing in that case.
            self.status.setText(self._t("batch_cancelled", "Cancelled"))
            return
        self.status.setText(
            self._t("batch_done", "Saved next to your file: {name}").format(
                name=os.path.basename(path)
            )
        )

    def _on_failed(self, message: str) -> None:
        self._set_running(False)
        self.progress.setVisible(False)
        self.status.setText(
            self._t("batch_error", "Failed: {error}").format(error=message)
        )

    def closeEvent(self, event) -> None:
        # A run keeps going in its own thread otherwise, writing files after
        # the window is gone.
        if self.worker.is_running():
            self.worker.cancel()
        super().closeEvent(event)

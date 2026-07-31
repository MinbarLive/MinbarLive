"""Qt batch / file window: turn a recording into subtitles.

Port of ``gui/batch_view.py``, including its control ORDER: settings first
(languages, output format, bilingual toggle, then the collapsed "More
settings" expander with the four engine dropdowns), THEN the file picker, then
progress, status and the action buttons. Picking a file is the last thing you
do before pressing Start, so it sits next to Start — not at the top.

The batch job is configured independently of the live app: nothing here writes
to the main settings. The pipeline itself is ``batch/processor.py``, reused
unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui_qt.api_keys import ensure_keys
from gui_qt.widgets import Dropdown
from providers import (
    PROVIDER_CHOICES,
    TRANSCRIPTION_PROVIDER_CHOICES,
    get_default_model,
    get_model_choices,
    ranked_keyed_provider,
)
from utils.logging import log
from utils.settings import (
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    TARGET_LANGUAGE_NAMES,
    language_canonical_name,
    language_display_name,
)

# Segmented sibling for each streaming engine. Duplicated from
# gui/batch_view.py rather than imported: importing that module would pull
# customtkinter into the Qt process, and the two toolkits never share one.
_BATCH_STT_FALLBACKS = {"openai_realtime": "openai", "gemini_realtime": "gemini"}


def _batch_stt_fallback(provider_id: str) -> str:
    """Segmented sibling for a streaming engine. Deepgram has no segmented
    mode and no shared key family — pick the highest-ranked segmented STT
    provider the user actually holds a key for."""
    if provider_id == "deepgram":
        return ranked_keyed_provider(["openai", "gemini"])
    return _BATCH_STT_FALLBACKS.get(provider_id, provider_id)


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
        self._panel = parent
        self.settings = settings
        self._input_path = ""
        self._output_path = ""
        self._more_open = False
        self.setWindowTitle(self._t("batch_file", "Batch / File"))
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.resize(640, 700)

        self.worker = _Worker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        title = QLabel("▦  " + self._t("batch_file", "Batch / File"))
        title.setObjectName("hero")
        subtitle = QLabel(
            self._t("batch_file_sub", "Create translated subtitles from a recording")
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(subtitle)

        outer.addWidget(self._options_card())
        outer.addWidget(self._file_card())

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        outer.addWidget(self.progress)

        # Starts empty: the file card already says "no file selected", and
        # repeating it right below reads as a duplicated widget.
        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.start_btn = QPushButton(self._t("batch_start", "Start"))
        self.start_btn.setObjectName("accent")
        self.start_btn.setMinimumHeight(44)
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setEnabled(False)
        self.cancel_btn = QPushButton(self._t("batch_cancel", "Cancel"))
        self.cancel_btn.setMinimumHeight(44)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        actions.addWidget(self.start_btn)
        actions.addWidget(self.cancel_btn)
        outer.addLayout(actions)

        followups = QHBoxLayout()
        followups.setSpacing(8)
        self.history_btn = QPushButton(self._t("batch_open_history", "Show in history"))
        self.history_btn.clicked.connect(self._on_open_history)
        self.history_btn.setEnabled(False)
        self.folder_btn = QPushButton(self._t("batch_open_folder", "Open folder"))
        self.folder_btn.clicked.connect(self._on_open_folder)
        self.folder_btn.setEnabled(False)
        followups.addWidget(self.history_btn)
        followups.addWidget(self.folder_btn)
        outer.addLayout(followups)
        outer.addStretch(1)

    # ── build ────────────────────────────────────────────────────────────
    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("field")
        return label

    def _options_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        grid = QGridLayout(card)
        grid.setContentsMargins(18, 16, 18, 16)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        source_names = [name for name, _ in SOURCE_LANGUAGES]
        self.source_combo = self._combo(
            [language_display_name(n) for n in source_names]
        )
        self.source_combo.setCurrentText(
            language_display_name(
                self.settings.source_language
                if self.settings.source_language in source_names
                else source_names[0]
            )
        )
        self.target_combo = self._combo(
            [language_display_name(n) for n in TARGET_LANGUAGE_NAMES]
        )
        self.target_combo.setCurrentText(
            language_display_name(self.settings.target_language)
        )
        grid.addWidget(
            self._caption(self._t("batch_source_language", "Spoken language")), 0, 0
        )
        grid.addWidget(
            self._caption(self._t("batch_target_language", "Subtitle language")), 0, 1
        )
        grid.addWidget(self.source_combo, 1, 0)
        grid.addWidget(self.target_combo, 1, 1)

        # Output format stays visible rather than hiding behind "More
        # settings": it is the primary deliverable. Both is the default — the
        # extra file costs nothing.
        self.output_combo = self._combo(
            [self._t(key, fallback) for _, key, fallback in _OUTPUT_FORMATS]
        )
        self.output_combo.setCurrentIndex(2)
        self.output_combo.currentIndexChanged.connect(self._sync_bilingual_state)
        grid.addWidget(
            self._caption(self._t("batch_output_format", "Output")), 2, 0, 1, 2
        )
        grid.addWidget(self.output_combo, 3, 0, 1, 2)

        self.bilingual_check = QCheckBox(
            self._t(
                "batch_bilingual_srt", "Bilingual subtitles (original + translation)"
            )
        )
        grid.addWidget(self.bilingual_check, 4, 0, 1, 2)

        self.more_btn = QPushButton(self._more_text())
        self.more_btn.setObjectName("row")
        self.more_btn.clicked.connect(self._toggle_more)
        grid.addWidget(self.more_btn, 5, 0, 1, 2)

        self.more_widget = self._more_settings()
        self.more_widget.setVisible(False)
        grid.addWidget(self.more_widget, 6, 0, 1, 2)
        self._sync_bilingual_state()
        return card

    def _more_settings(self) -> QWidget:
        """Engine + model pickers. Batch always runs the SEGMENTED engine, so
        the real-time-only engines are not offered here — the models shown are
        what the run actually uses."""
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 10, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        stt_choices = [
            (name, pid)
            for name, pid in TRANSCRIPTION_PROVIDER_CHOICES
            if pid not in STREAMING_TRANSCRIPTION_PROVIDERS
        ]
        self._stt_ids = [pid for _n, pid in stt_choices]
        effective = _batch_stt_fallback(self.settings.transcription_provider)
        if effective not in self._stt_ids:
            effective = self._stt_ids[0]
        self.stt_provider_combo = self._combo([n for n, _p in stt_choices])
        self.stt_provider_combo.setCurrentIndex(self._stt_ids.index(effective))
        self.stt_provider_combo.currentIndexChanged.connect(self._on_stt_provider)
        self.stt_model_combo = self._combo([])
        grid.addWidget(
            self._caption(self._t("batch_transcription_model", "Transcription")),
            0, 0, 1, 2,
        )
        grid.addWidget(self.stt_provider_combo, 1, 0)
        grid.addWidget(self.stt_model_combo, 1, 1)

        self._translation_ids = [pid for _n, pid in PROVIDER_CHOICES]
        provider = (
            self.settings.ai_provider
            if self.settings.ai_provider in self._translation_ids
            else self._translation_ids[0]
        )
        self.translation_provider_combo = self._combo(
            [n for n, _p in PROVIDER_CHOICES]
        )
        self.translation_provider_combo.setCurrentIndex(
            self._translation_ids.index(provider)
        )
        self.translation_provider_combo.currentIndexChanged.connect(
            self._on_translation_provider
        )
        self.translation_model_combo = self._combo([])
        grid.addWidget(
            self._caption(self._t("batch_translation_model", "Translation")),
            2, 0, 1, 2,
        )
        grid.addWidget(self.translation_provider_combo, 3, 0)
        grid.addWidget(self.translation_model_combo, 3, 1)

        defaults_btn = QPushButton(self._t("batch_defaults", "Use default"))
        defaults_btn.clicked.connect(self._on_defaults)
        grid.addWidget(defaults_btn, 4, 1)

        self._fill_models(
            self.stt_model_combo, self._selected_stt_provider(), "transcription"
        )
        self._fill_models(
            self.translation_model_combo,
            self._selected_translation_provider(),
            "translation",
        )
        return holder

    def _file_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 14, 18, 14)
        box.setSpacing(8)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.pick_btn = QPushButton("▤  " + self._t("batch_pick_file", "Choose file…"))
        self.pick_btn.clicked.connect(self._on_pick)
        self.clear_btn = QPushButton("✕")
        self.clear_btn.setFixedWidth(46)
        self.clear_btn.clicked.connect(self._on_clear)
        self.clear_btn.setEnabled(False)
        row.addWidget(self.pick_btn, 1)
        row.addWidget(self.clear_btn)
        box.addLayout(row)
        self.file_label = QLabel(self._t("batch_no_file", "No file selected"))
        self.file_label.setObjectName("muted")
        self.file_label.setWordWrap(True)
        box.addWidget(self.file_label)
        return card

    @staticmethod
    def _combo(items: list[str]) -> Dropdown:
        return Dropdown(items)

    # ── option handlers ──────────────────────────────────────────────────
    def _more_text(self) -> str:
        arrow = "▾" if self._more_open else "▸"
        return f"{arrow}  {self._t('batch_more_settings', 'More settings')}"

    def _toggle_more(self) -> None:
        self._more_open = not self._more_open
        self.more_widget.setVisible(self._more_open)
        self.more_btn.setText(self._more_text())

    def _sync_bilingual_state(self) -> None:
        # Transcript-only writes no SRT, so a bilingual SRT is meaningless.
        self.bilingual_check.setEnabled(self._output_format() != "txt")

    def _selected_stt_provider(self) -> str:
        return self._stt_ids[self.stt_provider_combo.currentIndex()]

    def _selected_translation_provider(self) -> str:
        return self._translation_ids[self.translation_provider_combo.currentIndex()]

    @staticmethod
    def _fill_models(combo: QComboBox, provider: str, kind: str) -> None:
        blocked = combo.blockSignals(True)
        combo.clear()
        for name, model_id in get_model_choices(provider, kind):
            combo.addItem(name, model_id)
        index = combo.findData(get_default_model(provider, kind))
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(blocked)

    def _on_stt_provider(self, _index: int) -> None:
        self._fill_models(
            self.stt_model_combo, self._selected_stt_provider(), "transcription"
        )

    def _on_translation_provider(self, _index: int) -> None:
        self._fill_models(
            self.translation_model_combo,
            self._selected_translation_provider(),
            "translation",
        )

    def _on_defaults(self) -> None:
        """Reset engines and output to what the app is configured to use."""
        effective = _batch_stt_fallback(self.settings.transcription_provider)
        if effective in self._stt_ids:
            self.stt_provider_combo.setCurrentIndex(self._stt_ids.index(effective))
        if self.settings.ai_provider in self._translation_ids:
            self.translation_provider_combo.setCurrentIndex(
                self._translation_ids.index(self.settings.ai_provider)
            )
        self._on_stt_provider(0)
        self._on_translation_provider(0)
        self.output_combo.setCurrentIndex(2)

    def _output_format(self) -> str:
        return _OUTPUT_FORMATS[self.output_combo.currentIndex()][0]

    # ── file + run ───────────────────────────────────────────────────────
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
        self.clear_btn.setEnabled(True)

    def _on_clear(self) -> None:
        self._input_path = ""
        self.file_label.setText(self._t("batch_no_file", "No file selected"))
        self.start_btn.setEnabled(False)
        self.clear_btn.setEnabled(False)

    def _on_start(self) -> None:
        if not self._input_path or self.worker.is_running():
            return
        # Both chosen engines need a key; ask now rather than failing inside
        # the worker thread half a file in.
        providers = [self._selected_stt_provider(), self._selected_translation_provider()]
        if not ensure_keys(list(dict.fromkeys(providers)), {}, self):
            return
        self._set_running(True)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.worker.start(
            input_path=self._input_path,
            source_language=language_canonical_name(self.source_combo.currentText()),
            target_language=language_canonical_name(self.target_combo.currentText()),
            transcription_provider=self._selected_stt_provider(),
            transcription_model=self.stt_model_combo.currentData(),
            translation_provider=self._selected_translation_provider(),
            translation_model=self.translation_model_combo.currentData(),
            output_format=self._output_format(),
            bilingual_srt=self.bilingual_check.isChecked()
            and self.bilingual_check.isEnabled(),
        )

    def _on_cancel(self) -> None:
        self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self.status.setText(self._t("batch_cancelled", "Cancelled"))

    def _set_running(self, running: bool) -> None:
        self.start_btn.setEnabled(not running and bool(self._input_path))
        self.cancel_btn.setEnabled(running)
        self.pick_btn.setEnabled(not running)
        self.clear_btn.setEnabled(not running and bool(self._input_path))

    def _on_open_history(self) -> None:
        # Straight to the Batch tab: the run just finished is there, not in the
        # session list the viewer opens on by default.
        if self._panel is not None:
            self._panel.open_history("batch")

    def _on_open_folder(self) -> None:
        if not self._output_path:
            return
        folder = os.path.dirname(self._output_path)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606 - a folder the user just wrote to
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", folder])  # noqa: S603,S607
        except OSError as exc:
            log(f"Opening the output folder failed: {exc}", level="WARNING")

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
        self._output_path = path
        self.history_btn.setEnabled(True)
        self.folder_btn.setEnabled(True)
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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # A run keeps going in its own thread otherwise, writing files after
        # the window is gone.
        if self.worker.is_running():
            self.worker.cancel()
        super().closeEvent(event)

"""Qt control panel — start/stop and the settings a session actually needs.

Deliberately a subset of the 4,425-line Tk panel: enough to run a real
session end-to-end so the overlay can be judged against live audio instead of
render harnesses. Settings/history/batch/announcement windows are not ported
yet and are reached from the Tk panel meanwhile.

Layout is Qt layouts, not measured pixels — no ``_responsive_scale``, no
``_align_advanced_card`` correction loop (the one that divided by the wrong
scale factor and froze the panel in PR #43), no ``window_work_area`` clamp.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gui.control_state import required_key_providers
from gui.device_list import find_input_device_position, get_input_devices
from gui_qt.api_keys import activate_stored_keys, ensure_keys
from gui_qt.i18n import load_gui_translations
from gui_qt.pipeline_bridge import PipelineBridge, streaming_enabled
from gui_qt.subtitle_window import SubtitleWindow
from gui_qt.widgets import Stepper
from utils.logging import log
from utils.settings import (
    SOURCE_LANGUAGES,
    SUBTITLE_MODES,
    TARGET_LANGUAGE_NAMES,
    load_settings,
    save_settings,
)


class ControlPanel(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.settings = load_settings()
        self.texts = load_gui_translations(self.settings.gui_language)
        self.subtitle_window: SubtitleWindow | None = None
        self._running = False
        activate_stored_keys()

        self.bridge = PipelineBridge(controller, self)
        self.bridge.translation.connect(self._on_translation)
        self.bridge.live_text.connect(self._on_live_text)
        self.bridge.audio_device_lost.connect(self._on_device_lost)

        self.setWindowTitle("MinbarLive (Qt)")
        self._build()
        self.resize(560, 420)

    def _t(self, key: str, fallback: str) -> str:
        return self.texts.get(key, fallback)

    # ── build ────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(16)

        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(12)

        (
            self.device_names,
            self.device_base_names,
            self.device_indices,
            self.device_loopback_flags,
        ) = get_input_devices()
        self.device_combo = QComboBox()
        self.device_combo.addItems(self.device_names or ["(no input devices)"])
        # Restore the saved device by NAME, not position: indices shift when
        # hardware is plugged in, and loopback entries carry synthetic negative
        # indices. Without this the combo silently defaults to the first device,
        # so a loopback setup captures a real microphone instead.
        saved = self.settings.input_device_name
        if saved:
            pos = find_input_device_position(saved, self.device_base_names)
            if pos is None and saved in self.device_names:
                pos = self.device_names.index(saved)
            if pos is not None:
                self.device_combo.setCurrentIndex(pos)

        self.source_combo = QComboBox()
        # SOURCE_LANGUAGES is (display name, code) pairs; the settings field
        # stores the name.
        self.source_combo.addItems([name for name, _ in SOURCE_LANGUAGES])
        self._select(self.source_combo, self.settings.source_language)

        self.target_combo = QComboBox()
        self.target_combo.addItems(TARGET_LANGUAGE_NAMES)
        self._select(self.target_combo, self.settings.target_language)

        # Display translated mode names but keep the raw values for storage:
        # "realtime" must never reach the user, and must never be replaced by
        # its label on the way back into Settings.
        self.mode_combo = QComboBox()
        for mode in SUBTITLE_MODES:
            self.mode_combo.addItem(self._t(f"subtitle_mode_{mode}", mode), mode)
        idx = self.mode_combo.findData(self.settings.subtitle_mode)
        if idx >= 0:
            self.mode_combo.setCurrentIndex(idx)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)

        form.addRow(QLabel(self._t("input_device", "Input device:")), self.device_combo)
        form.addRow(QLabel(self._t("source", "Spoken language:")), self.source_combo)
        form.addRow(QLabel(self._t("target", "Subtitle language:")), self.target_combo)
        form.addRow(QLabel(self._t("subtitles", "Subtitles:")), self.mode_combo)
        outer.addWidget(card)
        outer.addWidget(self._display_card())

        self.settings_btn = QPushButton("⚙  " + self._t("settings_title", "Settings"))
        self.settings_btn.clicked.connect(self.open_settings)
        outer.addWidget(self.settings_btn)

        buttons = QHBoxLayout()
        self.start_btn = QPushButton(self._t("start", "Start"))
        self.start_btn.setObjectName("accent")
        self.start_btn.clicked.connect(self.on_start)
        self.stop_btn = QPushButton(self._t("stop", "Stop"))
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        outer.addLayout(buttons)

        self.status = QLabel(self._t("status_stopped", "Stopped"))
        self.status.setObjectName("muted")
        self.status.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.status)
        outer.addStretch(1)

        self.setCentralWidget(root)

    def _display_card(self) -> QFrame:
        """Controls that take effect on the live overlay mid-session."""
        card = QFrame()
        card.setObjectName("card")
        form = QFormLayout(card)
        form.setContentsMargins(18, 18, 18, 18)
        form.setSpacing(12)

        # Monitors come straight from Qt, already in logical coordinates.
        self.monitor_combo = QComboBox()
        for i, screen in enumerate(QGuiApplication.screens()):
            g = screen.geometry()
            self.monitor_combo.addItem(f"{i + 1}. {screen.name()} ({g.width()}x{g.height()})")
        self.monitor_combo.setCurrentIndex(
            min(self.settings.monitor_index, self.monitor_combo.count() - 1)
        )
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)

        # Stepper with a visible value, matching the Tk font/scroll rows.
        self.font_stepper = Stepper(
            lambda: self._step_font(smaller=True),
            lambda: self._step_font(smaller=False),
            self._font_value_text(),
        )

        self.bilingual_check = QCheckBox(
            self._t("bilingual_mode", "Show original text")
        )
        self.bilingual_check.setChecked(self.settings.bilingual_mode)
        self.bilingual_check.toggled.connect(self._on_bilingual_toggled)

        form.addRow(QLabel(self._t("subtitle_monitor", "Monitor:")), self.monitor_combo)
        form.addRow(QLabel(self._t("font", "Font size:")), self.font_stepper)
        form.addRow(QLabel(""), self.bilingual_check)
        return card

    def _on_monitor_changed(self, index: int) -> None:
        self.settings.monitor_index = index
        if self.subtitle_window:
            self.subtitle_window.set_monitor(index)

    def _font_value_text(self) -> str:
        # font_size_base is a DIVISOR, so a smaller base is a larger font.
        # Showing the raw divisor would read backwards; show the step instead.
        return f"{self.settings.font_size_base}"

    def _step_font(self, *, smaller: bool) -> None:
        base = self.settings.font_size_base
        # Mirrors SubtitleWindow.increase_font/decrease_font so the stepper
        # works before an overlay exists (settings still persist).
        self.settings.font_size_base = (
            min(80, base + 5) if smaller else max(20, base - 5)
        )
        if self.subtitle_window:
            if smaller:
                self.subtitle_window.decrease_font()
            else:
                self.subtitle_window.increase_font()
            self.settings.font_size_base = self.subtitle_window.get_font_size_base()
        self.font_stepper.set_value_text(self._font_value_text())

    def open_settings(self) -> None:
        """Open the settings window, reusing it if already open."""
        existing = getattr(self, "_settings_window", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        from gui_qt.settings_window import SettingsWindow

        self._settings_window = SettingsWindow(self)
        self._settings_window.show()

    def apply_theme_mode(self, theme_mode: str) -> None:
        """Re-theme the whole application.

        One stylesheet call — the Tk tree walks per-widget registries
        (_cards, _labels, _buttons, _combos, ...) to recolour every widget.
        """
        from PySide6.QtWidgets import QApplication

        from gui_qt.theme import apply_theme

        apply_theme(QApplication.instance(), theme_mode)

    def _on_bilingual_toggled(self, checked: bool) -> None:
        self.settings.bilingual_mode = checked
        if self.subtitle_window:
            self.subtitle_window.set_bilingual_mode(checked)

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # ── session ──────────────────────────────────────────────────────────
    def on_start(self) -> None:
        if self._running:
            return
        self._persist()
        # Prompt for anything missing before touching the pipeline, so a
        # missing key is a dialog rather than a failure from inside start().
        if not ensure_keys(required_key_providers(self.settings), self.texts, self):
            return
        try:
            device = (
                self.device_indices[self.device_combo.currentIndex()]
                if self.device_indices
                else None
            )
            self._ensure_subtitle_window()
            self.controller.start(input_device=device)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            log(f"Start failed: {exc}", level="ERROR")
            QMessageBox.critical(self, "MinbarLive", str(exc))
            self._teardown_subtitle_window()
            return

        self._running = True
        if self.subtitle_window:
            self.subtitle_window.set_stopped_hint(False)
        self.bridge.start(
            streaming=streaming_enabled(self.settings),
            show_interim=self.settings.show_interim_transcript,
        )
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status.setText(self._t("status_running", "Running"))

    def on_stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self.bridge.stop()
        try:
            self.controller.stop()
        except Exception as exc:  # noqa: BLE001
            log(f"Stop failed: {exc}", level="ERROR")
        if self.subtitle_window:
            self.subtitle_window.set_live_text(None)
            self.subtitle_window.set_stopped_hint(True)
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status.setText(self._t("status_stopped", "Stopped"))

    def _on_device_lost(self) -> None:
        if self._running:
            self.on_stop()

    # ── subtitle window ──────────────────────────────────────────────────
    def _ensure_subtitle_window(self) -> None:
        if self.subtitle_window is not None:
            return
        s = self.settings
        self.subtitle_window = SubtitleWindow(
            on_stop=self.on_stop,
            monitor_index=s.monitor_index,
            font_size_base=s.font_size_base,
            source_font_size_base=s.source_font_size_base,
            translation_text_color=s.translation_text_color,
            source_text_color=s.source_text_color,
            target_language=s.target_language,
            subtitle_mode=s.subtitle_mode,
            scroll_speed=s.scroll_speed,
            transparent_static=s.transparent_static,
            window_height_percent=s.window_height_percent,
            show_footer=s.show_footer,
            theme_mode=s.subtitle_theme_mode,
            bilingual_mode=s.bilingual_mode,
            adaptive_catchup=s.adaptive_subtitle_catchup,
        )
        self.subtitle_window.show()

    def _teardown_subtitle_window(self) -> None:
        if self.subtitle_window is not None:
            self.subtitle_window.destroy()
            self.subtitle_window = None

    def _current_mode(self) -> str:
        return self.mode_combo.currentData() or SUBTITLE_MODES[0]

    def _on_mode_changed(self, _index: int) -> None:
        if self.subtitle_window:
            self.subtitle_window.set_subtitle_mode(self._current_mode())

    # ── pipeline signals (already on the GUI thread) ─────────────────────
    def _on_translation(self, text: str, source_text) -> None:
        if self.subtitle_window:
            self.subtitle_window.add_subtitle(text, source_text=source_text)

    def _on_live_text(self, text: str, settled: bool) -> None:
        if self.subtitle_window:
            self.subtitle_window.set_live_text(text, settled)

    # ── persistence / shutdown ───────────────────────────────────────────
    def _persist(self) -> None:
        self.settings.source_language = self.source_combo.currentText()
        self.settings.target_language = self.target_combo.currentText()
        self.settings.subtitle_mode = self._current_mode()
        self.settings.monitor_index = self.monitor_combo.currentIndex()
        self.settings.bilingual_mode = self.bilingual_check.isChecked()
        pos = self.device_combo.currentIndex()
        if 0 <= pos < len(self.device_base_names):
            self.settings.input_device_name = self.device_base_names[pos]
        save_settings(self.settings)

    def closeEvent(self, event) -> None:
        if self._running:
            self.on_stop()
        self._teardown_subtitle_window()
        super().closeEvent(event)

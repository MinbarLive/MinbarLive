"""Qt settings window.

Port of the controls in ``gui/settings_view.py`` that affect a running
session. Every control applies to the live overlay immediately where that is
possible, and writes through to ``Settings`` — there is no Save/Cancel, which
matches how the Tk settings window behaves.

Reuses the existing GUI translation keys rather than inventing new ones, so
this needs no additions to data/translations/gui/*.json.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QLabel,
    QScrollArea,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from utils.settings import (
    ALWAYS_ON_TOP_MODES,
    SUBTITLE_HIDE_MODES,
    THEME_MODES,
    save_settings,
)

# Window height is a percentage of the screen; the overlay clamps to 5-100.
_HEIGHT_MIN, _HEIGHT_MAX = 5, 100
# Scroll speed range mirrors the Tk stepper bounds.
_SPEED_MIN, _SPEED_MAX = 0.25, 5.0

# Translation keys for the 3-way selectors, in the order of the mode tuples.
# Both tuples start with "never" but diverge after it, so they need separate
# maps rather than one shared list.
_HIDE_MODE_KEYS = {
    "never": ("mode_never", "Never"),
    "stopped": ("mode_when_stopped", "When stopped"),
    "always": ("mode_always", "Always"),
}
_AOT_MODE_KEYS = {
    "never": ("mode_never", "Never"),
    "running": ("mode_when_running", "While running"),
    "always": ("mode_always", "Always"),
}


class SettingsWindow(QDialog):
    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel
        self.settings = panel.settings
        self._t = panel._t
        self.setWindowTitle(self._t("settings_title", "Settings"))
        self.resize(560, 680)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        layout.addWidget(self._appearance_card())
        layout.addWidget(self._subtitle_card())
        layout.addWidget(self._general_card())
        layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll)

    # ── helpers ──────────────────────────────────────────────────────────
    def _card(self, title: str) -> tuple[QFrame, QFormLayout]:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        heading = QLabel(title)
        heading.setObjectName("heading")
        box.addWidget(heading)
        form = QFormLayout()
        form.setSpacing(12)
        box.addLayout(form)
        return card, form

    def _overlay(self):
        return self._panel.subtitle_window

    def _save(self) -> None:
        save_settings(self.settings)

    # ── cards ────────────────────────────────────────────────────────────
    def _appearance_card(self) -> QFrame:
        card, form = self._card(self._t("settings_appearance", "Appearance"))

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(
            [self._t(f"theme_{m}", m.title()) for m in THEME_MODES]
        )
        if self.settings.theme_mode in THEME_MODES:
            self.theme_combo.setCurrentIndex(THEME_MODES.index(self.settings.theme_mode))
        self.theme_combo.currentIndexChanged.connect(self._on_theme)

        self.subtitle_theme_combo = QComboBox()
        self.subtitle_theme_combo.addItems(
            [self._t(f"theme_{m}", m.title()) for m in THEME_MODES]
        )
        if self.settings.subtitle_theme_mode in THEME_MODES:
            self.subtitle_theme_combo.setCurrentIndex(
                THEME_MODES.index(self.settings.subtitle_theme_mode)
            )
        self.subtitle_theme_combo.currentIndexChanged.connect(self._on_subtitle_theme)

        # Row labels must not repeat the card heading, so these are plain
        # descriptive fallbacks rather than the section keys.
        form.addRow(QLabel(self._t("control_panel", "Control panel:")), self.theme_combo)
        form.addRow(QLabel(self._t("subtitles", "Subtitles:")), self.subtitle_theme_combo)
        return card

    def _subtitle_card(self) -> QFrame:
        card, form = self._card(self._t("subtitle_appearance", "Subtitles"))

        self.height_slider = QSlider(Qt.Horizontal)
        self.height_slider.setRange(_HEIGHT_MIN, _HEIGHT_MAX)
        self.height_slider.setValue(
            max(_HEIGHT_MIN, min(_HEIGHT_MAX, self.settings.window_height_percent))
        )
        self.height_label = QLabel(f"{self.height_slider.value()} %")
        self.height_label.setObjectName("muted")
        self.height_slider.valueChanged.connect(self._on_height)

        self.speed_slider = QSlider(Qt.Horizontal)
        # Sliders are integral: store speed in hundredths.
        self.speed_slider.setRange(int(_SPEED_MIN * 100), int(_SPEED_MAX * 100))
        self.speed_slider.setValue(int(self.settings.scroll_speed * 100))
        self.speed_label = QLabel(f"{self.settings.scroll_speed:.2f}x")
        self.speed_label.setObjectName("muted")
        self.speed_slider.valueChanged.connect(self._on_speed)

        self.transparent_check = QCheckBox(self._t("transparent", "Transparent"))
        self.transparent_check.setChecked(self.settings.transparent_static)
        self.transparent_check.toggled.connect(self._on_transparent)

        self.footer_check = QCheckBox(self._t("show_footer", "Footer"))
        self.footer_check.setChecked(self.settings.show_footer)
        self.footer_check.toggled.connect(self._on_footer)

        self.catchup_check = QCheckBox(
            self._t("adaptive_catchup", "Speed up when subtitles fall behind")
        )
        self.catchup_check.setChecked(self.settings.adaptive_subtitle_catchup)
        self.catchup_check.toggled.connect(self._on_catchup)

        self.interim_check = QCheckBox(
            self._t("show_interim_transcript", "Show live transcript")
        )
        self.interim_check.setChecked(self.settings.show_interim_transcript)
        self.interim_check.toggled.connect(self._on_interim)

        self.hide_combo = QComboBox()
        self.hide_combo.addItems(
            [self._t(*_HIDE_MODE_KEYS[m]) for m in SUBTITLE_HIDE_MODES]
        )
        self.hide_combo.setCurrentIndex(
            SUBTITLE_HIDE_MODES.index(self.settings.subtitle_hide_mode)
            if self.settings.subtitle_hide_mode in SUBTITLE_HIDE_MODES
            else 0
        )
        self.hide_combo.currentIndexChanged.connect(self._on_hide_mode)

        self.aot_combo = QComboBox()
        self.aot_combo.addItems(
            [self._t(*_AOT_MODE_KEYS[m]) for m in ALWAYS_ON_TOP_MODES]
        )
        self.aot_combo.setCurrentIndex(
            ALWAYS_ON_TOP_MODES.index(self.settings.always_on_top_mode)
            if self.settings.always_on_top_mode in ALWAYS_ON_TOP_MODES
            else 0
        )
        self.aot_combo.currentIndexChanged.connect(self._on_aot_mode)

        form.addRow(
            QLabel(self._t("height", "Height:")),
            self._with(self.height_slider, self.height_label),
        )
        # No dedicated key exists for a scroll-speed LABEL — only the log
        # message "Scroll-Geschwindigkeit geaendert zu: {speed}", which reads
        # wrong as a form label. Plain fallback until a key is added.
        form.addRow(
            QLabel(self._t("scroll_speed_label", "Scroll speed:")),
            self._with(self.speed_slider, self.speed_label),
        )
        form.addRow(QLabel(self._t("hide_subtitle_label", "Hide subtitles:")), self.hide_combo)
        form.addRow(QLabel(self._t("window_on_top_label", "Always on top:")), self.aot_combo)
        form.addRow(QLabel(""), self.transparent_check)
        form.addRow(QLabel(""), self.footer_check)
        form.addRow(QLabel(""), self.catchup_check)
        form.addRow(QLabel(""), self.interim_check)
        return card

    def _general_card(self) -> QFrame:
        card, form = self._card(self._t("settings_general", "General"))

        self.islamic_check = QCheckBox(self._t("islamic_mode", "Islamic mode"))
        self.islamic_check.setChecked(self.settings.islamic_mode)
        self.islamic_check.toggled.connect(self._on_islamic)

        self.noise_check = QCheckBox(self._t("noise_filter", "Noise filter"))
        self.noise_check.setChecked(self.settings.noise_filter)
        self.noise_check.toggled.connect(self._on_noise)

        self.updates_check = QCheckBox(
            self._t("check_updates_on_launch", "Check for updates on launch")
        )
        self.updates_check.setChecked(self.settings.check_for_updates)
        self.updates_check.toggled.connect(self._on_updates)

        form.addRow(QLabel(""), self.islamic_check)
        form.addRow(QLabel(""), self.noise_check)
        form.addRow(QLabel(""), self.updates_check)
        return card

    @staticmethod
    def _with(slider: QSlider, label: QLabel) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(slider)
        lay.addWidget(label)
        return box

    # ── handlers ─────────────────────────────────────────────────────────
    def _on_theme(self, index: int) -> None:
        self.settings.theme_mode = THEME_MODES[index]
        self._panel.apply_theme_mode(self.settings.theme_mode)
        self._save()

    def _on_subtitle_theme(self, index: int) -> None:
        self.settings.subtitle_theme_mode = THEME_MODES[index]
        if self._overlay():
            self._overlay().set_theme(self.settings.subtitle_theme_mode)
        self._save()

    def _on_height(self, value: int) -> None:
        self.settings.window_height_percent = value
        self.height_label.setText(f"{value} %")
        if self._overlay():
            self._overlay().set_window_height_percent(value)
        self._save()

    def _on_speed(self, value: int) -> None:
        speed = value / 100.0
        self.settings.scroll_speed = speed
        self.speed_label.setText(f"{speed:.2f}x")
        if self._overlay():
            self._overlay()._scroll_speed = speed
        self._save()

    def _on_transparent(self, checked: bool) -> None:
        self.settings.transparent_static = checked
        if self._overlay():
            self._overlay().set_transparent_static(checked)
        self._save()

    def _on_footer(self, checked: bool) -> None:
        self.settings.show_footer = checked
        if self._overlay():
            self._overlay().set_show_footer(checked)
        self._save()

    def _on_catchup(self, checked: bool) -> None:
        self.settings.adaptive_subtitle_catchup = checked
        if self._overlay():
            self._overlay().set_adaptive_catchup(checked)
        self._save()

    def _on_interim(self, checked: bool) -> None:
        # Takes effect on the next Start: the bridge decides at start time
        # whether to sample the live transcript at all.
        self.settings.show_interim_transcript = checked
        self._save()

    def _on_hide_mode(self, index: int) -> None:
        self.settings.subtitle_hide_mode = SUBTITLE_HIDE_MODES[index]
        self._save()

    def _on_aot_mode(self, index: int) -> None:
        self.settings.always_on_top_mode = ALWAYS_ON_TOP_MODES[index]
        if self._overlay():
            self._overlay().set_always_on_top(
                self.settings.always_on_top_mode != "never"
            )
        self._save()

    def _on_islamic(self, checked: bool) -> None:
        self.settings.islamic_mode = checked
        self._save()

    def _on_noise(self, checked: bool) -> None:
        # Read per segment/chunk by both pipelines, so this applies live.
        self.settings.noise_filter = checked
        self._save()

    def _on_updates(self, checked: bool) -> None:
        self.settings.check_for_updates = checked
        self._save()

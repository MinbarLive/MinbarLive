"""Qt settings window.

Card order and contents follow ``gui/settings_view.py`` exactly — hero, then
General, Appearance, Islamic mode, API keys. That is deliberate: everything
about a *running session* (subtitle mode, height, hide policy, providers,
models) lives on the control panel in the Tk tree, and moving half of it in
here would leave operators hunting for controls that used to be one glance
away.

There is no Save/Cancel: every control writes through immediately and applies
to the live overlay where it can, which is how the Tk window behaves.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui_qt.dialogs import ask_yes_no, show_message
from gui_qt.widgets import Dropdown, SegmentedControl
from providers import (
    PROVIDER_CHOICES,
    clear_api_key,
    get_stored_api_key,
    save_api_key,
)
from utils.settings import (
    GUI_LANGUAGE_CODES,
    GUI_LANGUAGES,
    THEME_MODES,
    save_settings,
)
from version import __version__

# Deepgram has no translation capability so it is absent from PROVIDER_CHOICES,
# but it can still hold a streaming-transcription key that needs managing.
_KEY_PROVIDERS = [*PROVIDER_CHOICES, ("Deepgram", "deepgram")]


class SettingsWindow(QDialog):
    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel
        self.settings = panel.settings
        self._t = panel._t
        self.setWindowTitle(self._t("settings_title", "Settings"))
        self.setWindowIcon(panel.windowIcon())
        self.resize(560, 720)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        hero = QLabel(f"MinbarLive  —  v{__version__}")
        hero.setObjectName("hero")
        subtitle = QLabel(self._t("hero_subtitle", "Live translation control centre"))
        subtitle.setObjectName("muted")
        layout.addWidget(hero)
        layout.addWidget(subtitle)

        layout.addWidget(self._general_card())
        layout.addWidget(self._appearance_card())
        layout.addWidget(self._islamic_card())
        layout.addWidget(self._api_key_card())
        layout.addStretch(1)

        scroll.setWidget(body)
        outer.addWidget(scroll)

        bottom = QHBoxLayout()
        bottom.setContentsMargins(18, 10, 18, 14)
        bottom.addStretch(1)
        close_btn = QPushButton(self._t("dlg_cancel", "Close"))
        close_btn.clicked.connect(self.close)
        bottom.addWidget(close_btn)
        outer.addLayout(bottom)

    # ── helpers ──────────────────────────────────────────────────────────
    def _card(self, symbol: str, title: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(10)
        heading = QLabel(f"{symbol}  {title}")
        heading.setObjectName("card_title")
        box.addWidget(heading)
        return card, box

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("field")
        return label

    def _hint(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("muted")
        label.setWordWrap(True)
        return label

    def _overlay(self):
        return self._panel.subtitle_window

    def _save(self) -> None:
        save_settings(self.settings)

    # ── cards ────────────────────────────────────────────────────────────
    def _general_card(self) -> QFrame:
        card, box = self._card("◎", self._t("settings_general", "General"))

        self.gui_lang_combo = Dropdown()
        for code, name in GUI_LANGUAGES:
            self.gui_lang_combo.addItem(name, code)
        if self.settings.gui_language in GUI_LANGUAGE_CODES:
            self.gui_lang_combo.setCurrentIndex(
                GUI_LANGUAGE_CODES.index(self.settings.gui_language)
            )
        self.gui_lang_combo.currentIndexChanged.connect(self._on_gui_language)
        box.addWidget(self._caption(self._t("language", "Language")))
        box.addWidget(self.gui_lang_combo)

        box.addWidget(self._caption(self._t("updates_section", "Updates")))
        self.updates_check = QCheckBox(
            self._t("check_updates_on_launch", "Check for updates on launch")
        )
        self.updates_check.setChecked(self.settings.check_for_updates)
        self.updates_check.toggled.connect(self._on_updates)
        box.addWidget(self.updates_check)
        return card

    def _appearance_card(self) -> QFrame:
        card, box = self._card("☾", self._t("settings_appearance", "Appearance"))

        theme_labels = [self._t(f"theme_{m}", m.title()) for m in THEME_MODES]
        self.theme_segment = SegmentedControl(
            theme_labels, self._theme_index(self.settings.theme_mode)
        )
        self.theme_segment.changed.connect(self._on_theme)
        box.addWidget(self._caption(self._t("theme_mode", "Control panel")))
        box.addWidget(self.theme_segment)

        self.subtitle_theme_segment = SegmentedControl(
            theme_labels, self._theme_index(self.settings.subtitle_theme_mode)
        )
        self.subtitle_theme_segment.changed.connect(self._on_subtitle_theme)
        box.addWidget(self._caption(self._t("subtitle_theme_mode", "Subtitles")))
        box.addWidget(self.subtitle_theme_segment)

        # Backdrop opacity lives in the control panel's Display & audio card,
        # beside the height and font-size controls it belongs with.
        return card

    def _islamic_card(self) -> QFrame:
        card, box = self._card("☪", self._t("islamic_mode", "Islamic mode"))
        self.islamic_check = QCheckBox(self._t("islamic_mode_enabled", "Enabled"))
        self.islamic_check.setChecked(self.settings.islamic_mode)
        self.islamic_check.toggled.connect(self._on_islamic)
        box.addWidget(self.islamic_check)
        box.addWidget(
            self._hint(
                self._t(
                    "islamic_mode_hint",
                    "Quran verse & Athan recognition and Islamic translation "
                    "style. Off = general translator for any content.",
                )
            )
        )
        return card

    def _api_key_card(self) -> QFrame:
        card, box = self._card("⚿", self._t("api_key_section", "API key"))

        self.key_provider_combo = Dropdown()
        # Starts unselected: Change/Remove act on the chosen provider, and a
        # pre-selected one invites removing the wrong key.
        self.key_provider_combo.addItem(
            self._t("api_key_select_provider", "Please select"), ""
        )
        for name, pid in _KEY_PROVIDERS:
            self.key_provider_combo.addItem(name, pid)
        self.key_provider_combo.currentIndexChanged.connect(self._refresh_key_status)
        box.addWidget(self.key_provider_combo)

        self.key_status = QLabel("—")
        self.key_status.setObjectName("muted")
        box.addWidget(self.key_status)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.change_key_btn = QPushButton("⚿  " + self._t("change_key", "Change key"))
        self.change_key_btn.clicked.connect(self._on_change_key)
        self.remove_key_btn = QPushButton("✕  " + self._t("remove_key", "Remove key"))
        self.remove_key_btn.clicked.connect(self._on_remove_key)
        buttons.addWidget(self.change_key_btn)
        buttons.addWidget(self.remove_key_btn)
        box.addLayout(buttons)
        self._refresh_key_status()
        return card

    @staticmethod
    def _theme_index(mode: str) -> int:
        return THEME_MODES.index(mode) if mode in THEME_MODES else 0

    # ── handlers ─────────────────────────────────────────────────────────
    def _on_gui_language(self, _index: int) -> None:
        code = self.gui_lang_combo.currentData()
        if not code or code == self.settings.gui_language:
            return
        # Rebuilding the panel closes this window, so read nothing after it.
        self._panel.apply_gui_language(code)

    def _on_updates(self, checked: bool) -> None:
        self.settings.check_for_updates = checked
        self._save()

    def _on_theme(self, index: int) -> None:
        self.settings.theme_mode = THEME_MODES[index]
        self._panel.apply_theme_mode(self.settings.theme_mode)
        self._save()

    def _on_subtitle_theme(self, index: int) -> None:
        self.settings.subtitle_theme_mode = THEME_MODES[index]
        if self._overlay():
            self._overlay().set_theme(self.settings.subtitle_theme_mode)
        self._save()

    def _on_islamic(self, checked: bool) -> None:
        # Turning it OFF asks for confirmation (it changes what the pipeline
        # does with every segment); turning it ON is instant.
        if not checked:
            confirmed = ask_yes_no(
                self,
                self._t("islamic_mode", "Islamic mode"),
                self._t(
                    "islamic_mode_off_confirm",
                    "Turn off Quran and Athan recognition?",
                ),
                default_yes=False,
                translate=self._t,
            )
            if not confirmed:
                self.islamic_check.blockSignals(True)
                self.islamic_check.setChecked(True)
                self.islamic_check.blockSignals(False)
                return
        self.settings.islamic_mode = checked
        self._save()

    # ── API keys ─────────────────────────────────────────────────────────
    def _selected_key_provider(self) -> str:
        return self.key_provider_combo.currentData() or ""

    def _refresh_key_status(self) -> None:
        provider = self._selected_key_provider()
        enabled = bool(provider)
        self.change_key_btn.setEnabled(enabled)
        self.remove_key_btn.setEnabled(enabled)
        if not enabled:
            self.key_status.setText("—")
            return
        stored = bool(get_stored_api_key(provider))
        self.key_status.setText(
            self._t("api_key_status_saved", "A key is saved.")
            if stored
            else self._t("api_key_status_none", "No key saved.")
        )

    def _on_change_key(self) -> None:
        provider = self._selected_key_provider()
        if not provider:
            return
        if self._panel._running:
            show_message(
                self,
                self._t("api_key_section", "API key"),
                self._t(
                    "dlg_stop_before_change_key", "Stop the session before changing keys."
                ),
                kind="info",
                translate=self._t,
            )
            return
        from gui_qt.api_keys import ApiKeyDialog

        dialog = ApiKeyDialog(provider, self._panel.texts, self)
        if dialog.exec() != QDialog.Accepted or not dialog.key():
            return
        if not save_api_key(provider, dialog.key()):
            show_message(
                self,
                "MinbarLive",
                self._t(
                    "dlg_key_session_only_warning",
                    "No system keychain was available, so this key is only "
                    "active until you close MinbarLive.",
                ),
                translate=self._t,
            )
        self._refresh_key_status()

    def _on_remove_key(self) -> None:
        provider = self._selected_key_provider()
        if not provider:
            return
        if self._panel._running:
            show_message(
                self,
                self._t("api_key_section", "API key"),
                self._t("dlg_stop_before_remove", "Stop the session before removing keys."),
                kind="info",
                translate=self._t,
            )
            return
        if not ask_yes_no(
            self,
            self._t("remove_key", "Remove key"),
            self._t("dlg_remove_key_question", "Remove the stored API key?"),
            default_yes=False,
            translate=self._t,
        ):
            return
        clear_api_key(provider)
        show_message(
            self,
            "MinbarLive",
            self._t("dlg_key_removed", "API key removed."),
            kind="info",
            translate=self._t,
        )
        self._refresh_key_status()

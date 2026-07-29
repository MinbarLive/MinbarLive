"""Qt first-run setup wizard.

Port of ``gui/onboarding.py``. Five steps: appearance, languages, input
device, provider + API key, disclaimer.

The finish logic is a close port rather than a fresh design — it encodes
decisions that were expensive to reach:

* **Keys decide the provider, not the dropdown's last position.** Browsing to
  a provider without entering its key must not select it, so the choice runs
  through ``resolve_provider_by_keys``.
* **The realtime engine follows the CHOSEN provider**, so the key just entered
  is the one the pipeline authenticates with. A pinned engine previously
  prompted OpenAI-only users for a Gemini key on first Start.
* **"Use default" is only ticked when the value IS the default** — a greyed
  non-default provider beside a ticked "Standard" reads as broken.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gui.device_list import get_input_devices
from gui_qt.i18n import load_gui_translations
from gui_qt.theme import apply_theme
from gui_qt.widgets import SegmentedControl
from providers import (
    PROVIDER_CHOICES,
    get_default_model,
    get_stored_api_key,
    get_streaming_key_provider,
    resolve_provider_by_keys,
    save_api_key,
)
from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    GUI_LANGUAGES,
    PIPELINE_MODE_STREAMING,
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    TARGET_LANGUAGE_NAMES,
    THEME_MODES,
    load_settings,
    save_settings,
)

# Providers that have a realtime engine of their own. Anthropic has none.
_REALTIME_ENGINE_FOR_PROVIDER = {
    "gemini": "gemini_realtime",
    "openai": "openai_realtime",
}


class OnboardingWizard(QDialog):
    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        settings = load_settings()
        self._gui_language = settings.gui_language
        self._theme = settings.theme_mode
        self.texts = load_gui_translations(self._gui_language)
        self.completed = False

        self.setWindowTitle("MinbarLive")
        self.resize(620, 640)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(16)

        self.step_label = QLabel("")
        self.step_label.setObjectName("muted")
        outer.addWidget(self.step_label)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._step_appearance())
        self.stack.addWidget(self._step_languages())
        self.stack.addWidget(self._step_device())
        self.stack.addWidget(self._step_provider())
        self.stack.addWidget(self._step_disclaimer())
        outer.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.back_btn = QPushButton(self._t("wizard_back", "Back"))
        self.back_btn.clicked.connect(self._on_back)
        self.next_btn = QPushButton(self._t("wizard_next", "Next"))
        self.next_btn.setObjectName("accent")
        self.next_btn.clicked.connect(self._on_next)
        nav.addWidget(self.back_btn)
        nav.addStretch(1)
        nav.addWidget(self.next_btn)
        outer.addLayout(nav)

        self._sync_nav()

    def _t(self, key: str, fallback: str) -> str:
        return self.texts.get(key, fallback)

    def _card(self, title: str, subtitle: str = "") -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("heading")
        layout.addWidget(heading)
        if subtitle:
            sub = QLabel(subtitle)
            sub.setObjectName("muted")
            sub.setWordWrap(True)
            layout.addWidget(sub)
        card = QFrame()
        card.setObjectName("card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(18, 16, 18, 16)
        inner.setSpacing(12)
        layout.addWidget(card)
        layout.addStretch(1)
        return page, inner

    # ── steps ────────────────────────────────────────────────────────────
    def _step_appearance(self) -> QWidget:
        page, inner = self._card(self._t("wizard_theme_label", "Appearance"))

        # GUI language first: changing it re-renders every later step.
        self.gui_lang_combo = QComboBox()
        for code, name in GUI_LANGUAGES:
            self.gui_lang_combo.addItem(name, code)
        idx = self.gui_lang_combo.findData(self._gui_language)
        if idx >= 0:
            self.gui_lang_combo.setCurrentIndex(idx)
        self.gui_lang_combo.currentIndexChanged.connect(self._on_gui_language)
        inner.addWidget(QLabel(self._t("language", "Language")))
        inner.addWidget(self.gui_lang_combo)

        self.theme_segment = SegmentedControl(
            [self._t(f"theme_{m}", m.title()) for m in THEME_MODES],
            THEME_MODES.index(self._theme) if self._theme in THEME_MODES else 0,
        )
        self.theme_segment.changed.connect(self._on_theme)
        inner.addWidget(QLabel(self._t("wizard_theme_label", "Appearance")))
        inner.addWidget(self.theme_segment)
        return page

    def _step_languages(self) -> QWidget:
        page, inner = self._card(self._t("wizard_languages_title", "Translation languages"))
        settings = load_settings()

        self.source_combo = QComboBox()
        self.source_combo.addItems([name for name, _ in SOURCE_LANGUAGES])
        self._select(self.source_combo, settings.source_language)
        self.target_combo = QComboBox()
        self.target_combo.addItems(TARGET_LANGUAGE_NAMES)
        self._select(self.target_combo, settings.target_language)

        inner.addWidget(QLabel(self._t("source", "Spoken language")))
        inner.addWidget(self.source_combo)
        inner.addWidget(QLabel(self._t("target", "Subtitle language")))
        inner.addWidget(self.target_combo)
        return page

    def _step_device(self) -> QWidget:
        page, inner = self._card(self._t("wizard_audio_title", "Input device"))
        self.device_names, self.device_base_names, _, _ = get_input_devices()
        self.device_combo = QComboBox()
        self.device_combo.addItems(self.device_names or ["(no input devices)"])
        inner.addWidget(QLabel(self._t("input_device", "Input device")))
        inner.addWidget(self.device_combo)
        return page

    def _step_provider(self) -> QWidget:
        page, inner = self._card(
            self._t("wizard_provider_title", "AI provider"),
            self._t(
                "wizard_keys_info",
                "With OpenAI, one key covers both translation and real-time "
                "transcription.",
            ),
        )
        self.provider_combo = QComboBox()
        for name, pid in PROVIDER_CHOICES:
            label = name
            if pid == DEFAULT_AI_PROVIDER:
                label = f"{name} {self._t('provider_default_tag', '(Default)')}"
            self.provider_combo.addItem(label, pid)
        self.provider_combo.currentIndexChanged.connect(self._on_provider)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_hint = QLabel("")
        self.key_hint.setObjectName("muted")
        self.key_hint.setWordWrap(True)

        # Row label, not the card heading — repeating it reads as a bug.
        inner.addWidget(QLabel(self._t("wizard_provider", "Provider")))
        inner.addWidget(self.provider_combo)
        inner.addWidget(QLabel(self._t("wizard_api_key", "API key")))
        inner.addWidget(self.key_edit)
        inner.addWidget(self.key_hint)

        # Keys entered this session, per provider — browsing away and back must
        # not lose one, and finish resolves the provider from this map.
        self._provider_keys: dict[str, str] = {}
        self._on_provider(0)
        return page

    def _step_disclaimer(self) -> QWidget:
        page, inner = self._card(self._t("wizard_disclaimer_title", "Please note"))
        body = QLabel(
            self._t(
                "wizard_disclaimer_text",
                "MinbarLive uses AI. Translations can be wrong or incomplete "
                "and must not be relied on for religious rulings.",
            )
        )
        body.setWordWrap(True)
        inner.addWidget(body)
        self.disclaimer_check = QCheckBox(
            self._t("wizard_disclaimer_accept", "I understand")
        )
        self.disclaimer_check.toggled.connect(lambda _: self._sync_nav())
        inner.addWidget(self.disclaimer_check)
        return page

    @staticmethod
    def _select(combo: QComboBox, value: str) -> None:
        idx = combo.findText(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # ── handlers ─────────────────────────────────────────────────────────
    def _on_gui_language(self, _index: int) -> None:
        self._gui_language = self.gui_lang_combo.currentData()
        self.texts = load_gui_translations(self._gui_language)
        self._retranslate()

    def _on_theme(self, index: int) -> None:
        self._theme = THEME_MODES[index]
        apply_theme(self._app, self._theme)

    def _current_provider(self) -> str:
        return self.provider_combo.currentData() or DEFAULT_AI_PROVIDER

    def _on_provider(self, _index: int) -> None:
        """Remember the key typed for the previous provider, show the next."""
        provider = self._current_provider()
        typed = self.key_edit.text().strip()
        previous = getattr(self, "_last_provider", None)
        if previous and typed:
            self._provider_keys[previous] = typed
        self._last_provider = provider
        self.key_edit.setText(self._provider_keys.get(provider, ""))
        self.key_edit.setPlaceholderText("sk-..." if provider == "openai" else "")
        if get_stored_api_key(provider):
            self.key_hint.setText(
                self._t("wizard_key_saved_hint", "A key is already saved for this provider.")
            )
        else:
            self.key_hint.setText("")

    def _capture_current_key(self) -> None:
        typed = self.key_edit.text().strip()
        if typed:
            self._provider_keys[self._current_provider()] = typed

    def _retranslate(self) -> None:
        self.back_btn.setText(self._t("wizard_back", "Back"))
        self.next_btn.setText(self._t("wizard_next", "Next"))
        self.disclaimer_check.setText(
            self._t("wizard_disclaimer_accept", "I understand")
        )
        self._sync_nav()

    # ── navigation ───────────────────────────────────────────────────────
    def _sync_nav(self) -> None:
        index = self.stack.currentIndex()
        last = self.stack.count() - 1
        self.back_btn.setEnabled(index > 0)
        self.step_label.setText(
            self._t("wizard_step_of", "Step {current} of {total}").format(
                current=index + 1, total=self.stack.count()
            )
        )
        if index == last:
            self.next_btn.setText(self._t("wizard_finish", "Finish"))
            self.next_btn.setEnabled(self.disclaimer_check.isChecked())
        else:
            self.next_btn.setText(self._t("wizard_next", "Next"))
            self.next_btn.setEnabled(True)

    def _on_back(self) -> None:
        if self.stack.currentIndex() > 0:
            self._capture_current_key()
            self.stack.setCurrentIndex(self.stack.currentIndex() - 1)
            self._sync_nav()

    def _on_next(self) -> None:
        if self.stack.currentIndex() < self.stack.count() - 1:
            self._capture_current_key()
            self.stack.setCurrentIndex(self.stack.currentIndex() + 1)
            self._sync_nav()
            return
        self._finish()

    # ── finish ───────────────────────────────────────────────────────────
    def _finish(self) -> None:
        self._capture_current_key()
        settings = load_settings()
        settings.gui_language = self._gui_language
        # One appearance answer drives both windows.
        settings.theme_mode = self._theme
        settings.subtitle_theme_mode = self._theme
        settings.source_language = self.source_combo.currentText()
        settings.target_language = self.target_combo.currentText()
        pos = self.device_combo.currentIndex()
        if 0 <= pos < len(self.device_base_names):
            settings.input_device_name = self.device_base_names[pos]

        # Keys decide the provider, never the dropdown's last position.
        provider = resolve_provider_by_keys(self._provider_keys)
        settings.ai_provider = provider
        settings.translation_model = get_default_model(provider, "translation")
        settings.use_default_translation_model = provider == DEFAULT_AI_PROVIDER

        # Land on real-time streaming, on the engine belonging to the chosen
        # provider. Anthropic has none: use the first engine whose key exists.
        engine = _REALTIME_ENGINE_FOR_PROVIDER.get(provider)
        if engine is None:
            engine = DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
            for candidate in STREAMING_TRANSCRIPTION_PROVIDERS:
                key_provider = get_streaming_key_provider(candidate)
                if self._provider_keys.get(key_provider) or get_stored_api_key(
                    key_provider
                ):
                    engine = candidate
                    break
        settings.transcription_provider = engine
        settings.pipeline_mode = PIPELINE_MODE_STREAMING
        settings.transcription_model = get_default_model(engine, "transcription")
        settings.use_default_transcription_model = (
            engine == DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
        )

        settings.disclaimer_accepted = True
        settings.onboarding_completed = True
        save_settings(settings)

        # Persist every key entered this session. Only the active provider's
        # key surfaces the session-only warning, so several keys cannot stack
        # dialogs.
        session_only = False
        for pid, key in self._provider_keys.items():
            if not key:
                continue
            if not save_api_key(pid, key) and pid == provider:
                session_only = True
        if session_only:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                "MinbarLive",
                self._t(
                    "dlg_key_session_only_warning",
                    "No system keychain was available, so this key is only "
                    "active until you close MinbarLive.",
                ),
            )

        self.completed = True
        self.accept()


def run_onboarding(app) -> bool:
    """Run the wizard if first-run setup is outstanding.

    Returns False when the user cancelled, so the caller can exit without
    opening the control panel. Already-completed setups return True without
    showing anything.
    """
    if load_settings().onboarding_completed:
        return True
    wizard = OnboardingWizard(app)
    wizard.exec()
    return wizard.completed

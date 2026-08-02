"""Qt first-run setup wizard.

Port of ``gui/onboarding.py``. Five steps: appearance, languages, input
device, provider + API key, disclaimer.

Layout follows the Tk wizard: a centred title and step counter as persistent
chrome, and every step's own heading, sub-line and controls *inside* one card
that fills the remaining height. The two deliberate departures are the
appearance choice (a segmented Dark|Light control rather than a dropdown) and
the provider step's general note, which sits at the top of the card instead of
below the caveats.

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

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
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

from gui.device_list import BLACKHOLE_URL, get_input_devices, loopback_supported
from gui_qt.dialogs import show_message
from gui_qt.i18n import load_gui_translations
from gui_qt.icons import app_icon
from gui_qt.levels import level_fill
from gui_qt.theme import apply_theme, current_colors
from gui_qt.widgets import AudioLevelBar, Dropdown, SegmentedControl, warning_box
from providers import (
    PROVIDER_CHOICES,
    get_default_model,
    get_key_placeholder,
    get_stored_api_key,
    get_streaming_key_provider,
    resolve_provider_by_keys,
    save_api_key,
)
from utils.logging import log
from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    GUI_LANGUAGES,
    PIPELINE_MODE_STREAMING,
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    TARGET_LANGUAGE_NAMES,
    THEME_MODES,
    language_canonical_name,
    language_display_name,
    load_settings,
    save_settings,
)

# Stack index of the input-device step, which owns the level meter.
_DEVICE_STEP = 2
# Meter cadence. Unlike the control panel's meter (and the Tk wizard's, which
# releases the device again after 10s), the preview here runs until it is
# stopped: setup is exactly when someone talks into the microphone to see the
# bar move, and having it die mid-test reads as a broken meter.
_LEVEL_POLL_MS = 50

# Providers that have a realtime engine of their own. Anthropic has none.
_REALTIME_ENGINE_FOR_PROVIDER = {
    "gemini": "gemini_realtime",
    "openai": "openai_realtime",
}

# Video walkthroughs for obtaining a key, and the provider's console page.
# Duplicated from gui/onboarding.py rather than imported: that module is
# CustomTkinter, and the two toolkits never share a process.
_KEY_HELP_LINKS: dict[str, dict[str, str]] = {
    "openai": {
        "en": "https://youtu.be/OB99E7Y1cMA",
        "de": "https://youtu.be/SISlgzB_qpQ",
    },
    "gemini": {
        "en": "https://www.youtube.com/watch?v=Cl4XKgz6EJQ",
        "de": "https://www.youtube.com/watch?v=alNk5N-pv7Y",
    },
    "anthropic": {
        "en": "https://www.youtube.com/watch?v=e4yLquSc6Lw",
        "de": "https://www.youtube.com/watch?v=qAUAE2jkzpQ",
    },
    "deepgram": {"en": "https://www.youtube.com/watch?v=FVJEkE69ei0"},
}
_KEY_SITE_LINKS: dict[str, str] = {
    "gemini": "https://aistudio.google.com/api-keys",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "deepgram": "https://console.deepgram.com/",
}


class OnboardingWizard(QDialog):
    def __init__(self, app, controller=None, parent=None):
        super().__init__(parent)
        self._app = app
        # Only the input-level meter needs it; the wizard still builds without
        # one so it stays constructible in tests.
        self._controller = controller
        settings = load_settings()
        self._gui_language = settings.gui_language
        self._theme = settings.theme_mode
        self.texts = load_gui_translations(self._gui_language)
        self.completed = False
        # Every label whose text comes from the translation files, so switching
        # the GUI language on step 1 re-labels the steps behind it too.
        self._i18n: list[tuple[QWidget, str, str, str]] = []

        self.setWindowTitle("MinbarLive")
        icon = app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(680, 660)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 24)
        outer.setSpacing(10)

        self.title_label = QLabel("")
        self.title_label.setObjectName("wizard_title")
        self.title_label.setAlignment(Qt.AlignCenter)
        self._tr(self.title_label, "wizard_title", "Welcome to MinbarLive")
        outer.addWidget(self.title_label)

        self.step_label = QLabel("")
        self.step_label.setObjectName("muted")
        self.step_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.step_label)
        outer.addSpacing(8)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._step_appearance())
        self.stack.addWidget(self._step_languages())
        self.stack.addWidget(self._step_device())
        self.stack.addWidget(self._step_provider())
        self.stack.addWidget(self._step_disclaimer())
        outer.addWidget(self.stack, 1)

        # A meter preview holds the OS input device open, so it runs only while
        # the device step is on screen.
        self.stack.currentChanged.connect(self._on_step_changed)

        nav = QHBoxLayout()
        nav.setSpacing(16)
        self.back_btn = QPushButton("")
        self.back_btn.setMinimumHeight(44)
        self.back_btn.clicked.connect(self._on_back)
        self._tr(self.back_btn, "wizard_back", "Back")
        self.next_btn = QPushButton("")
        self.next_btn.setObjectName("accent")
        self.next_btn.setMinimumHeight(44)
        self.next_btn.clicked.connect(self._on_next)
        # Equal halves, as the Tk nav bar is — not a small Back beside a stretch.
        nav.addWidget(self.back_btn, 1)
        nav.addWidget(self.next_btn, 1)
        outer.addLayout(nav)

        self._sync_nav()

    def _t(self, key: str, fallback: str) -> str:
        return self.texts.get(key, fallback)

    def _tr(self, widget, key: str, fallback: str, prefix: str = ""):
        """Set a widget's text from the translations and remember it."""
        self._i18n.append((widget, key, fallback, prefix))
        widget.setText(prefix + self._t(key, fallback))
        return widget

    def _label(self, key: str, fallback: str, object_name: str = "wizard_sub") -> QLabel:
        label = QLabel("")
        label.setObjectName(object_name)
        label.setWordWrap(True)
        return self._tr(label, key, fallback)

    def _card(
        self,
        title_key: str,
        title_fallback: str,
        sub_key: str = "",
        sub_fallback: str = "",
    ) -> tuple[QWidget, QVBoxLayout]:
        """A step page: one card filling the height, headed by its own title.

        The Tk wizard puts the step heading and its sub-line inside the card
        rather than above it, and lets the card fill the window — matched here
        so the two trees read the same.
        """
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("card")
        inner = QVBoxLayout(card)
        inner.setContentsMargins(22, 18, 22, 18)
        inner.setSpacing(8)
        layout.addWidget(card, 1)

        inner.addWidget(self._label(title_key, title_fallback, object_name="heading"))
        if sub_key:
            inner.addWidget(self._label(sub_key, sub_fallback))
        return page, inner

    # ── steps ────────────────────────────────────────────────────────────
    def _step_appearance(self) -> QWidget:
        page, inner = self._card(
            "wizard_ui_language_title",
            "App language",
            "wizard_ui_language_sub",
            "Language of the control panel (subtitles use their own setting).",
        )

        # GUI language first: changing it re-labels every later step.
        self.gui_lang_combo = Dropdown()
        for code, name in GUI_LANGUAGES:
            self.gui_lang_combo.addItem(name, code)
        idx = self.gui_lang_combo.findData(self._gui_language)
        if idx >= 0:
            self.gui_lang_combo.setCurrentIndex(idx)
        self.gui_lang_combo.currentIndexChanged.connect(self._on_gui_language)
        inner.addWidget(self.gui_lang_combo)

        # A segmented Dark|Light pair rather than a dropdown: it is a two-way
        # choice whose effect is visible the moment it is clicked.
        inner.addSpacing(6)
        inner.addWidget(self._label("wizard_theme_label", "Appearance"))
        self.theme_segment = SegmentedControl(
            self._theme_labels(),
            THEME_MODES.index(self._theme) if self._theme in THEME_MODES else 0,
        )
        self.theme_segment.changed.connect(self._on_theme)
        inner.addWidget(self.theme_segment)
        inner.addStretch(1)
        return page

    def _theme_labels(self) -> list[str]:
        return [self._t(f"theme_{mode}", mode.title()) for mode in THEME_MODES]

    def _step_languages(self) -> QWidget:
        page, inner = self._card(
            "wizard_languages_title", "Translation languages"
        )
        settings = load_settings()

        # Real-time — where onboarding always lands — cannot auto-detect the
        # source language, so "Automatic" (the entry with no language code) is
        # not offered here at all.
        self._source_names = [name for name, code in SOURCE_LANGUAGES if code is not None]
        self.source_combo = Dropdown()
        self.source_combo.addItems(
            [language_display_name(n) for n in self._source_names]
        )
        source = (
            settings.source_language
            if settings.source_language in self._source_names
            else self._source_names[0]
        )
        self.source_combo.setCurrentText(language_display_name(source))

        self.target_combo = Dropdown()
        self.target_combo.addItems(
            [language_display_name(n) for n in TARGET_LANGUAGE_NAMES]
        )
        self.target_combo.setCurrentText(
            language_display_name(settings.target_language)
        )

        inner.addSpacing(6)
        inner.addWidget(self._label("wizard_source_language", "Spoken language (source)"))
        inner.addWidget(self.source_combo)
        inner.addSpacing(6)
        inner.addWidget(
            self._label("wizard_target_language", "Subtitle language (target)")
        )
        inner.addWidget(self.target_combo)
        inner.addStretch(1)
        return page

    def _step_device(self) -> QWidget:
        page, inner = self._card(
            "wizard_audio_title",
            "Input device",
            "wizard_audio_sub",
            "Choose the audio input used for the live translation.",
        )
        (
            self.device_names,
            self.device_base_names,
            self.device_indices,
            _loopback,
        ) = get_input_devices()
        self.device_combo = Dropdown()
        self.device_combo.addItems(self.device_names or [""])
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        inner.addSpacing(6)
        inner.addWidget(self.device_combo)
        # Where the platform has no loopback capture (macOS) the list simply
        # has no system-audio entry; say why, as the Tk wizard does.
        self._loopback_hint = None
        if not loopback_supported():
            # Through _tr, so it follows a language switch on the first step
            # like every other label here.
            self._loopback_hint = self._tr(
                QPushButton(),
                "macos_loopback_hint",
                "macOS: system audio needs BlackHole ↗",
            )
            self._loopback_hint.setObjectName("link")
            self._loopback_hint.setFlat(True)
            self._loopback_hint.setCursor(Qt.PointingHandCursor)
            self._loopback_hint.clicked.connect(
                lambda: QDesktopServices.openUrl(QUrl(BLACKHOLE_URL))
            )
            inner.addWidget(self._loopback_hint)
        self._level_holder = self._level_row()
        inner.addWidget(self._level_holder)

        # Nothing to pick from: say so instead of showing a dead combo and a
        # meter that can never move (the panel can still be used later).
        self._no_devices_label = self._label(
            "wizard_no_devices",
            "No input devices found — you can choose one later in the control "
            "panel.",
        )
        inner.addWidget(self._no_devices_label)
        has_devices = bool(self.device_names)
        self.device_combo.setVisible(has_devices)
        self._level_holder.setVisible(has_devices)
        self._no_devices_label.setVisible(not has_devices)

        inner.addStretch(1)
        return page

    # ── input-level meter (device step only) ─────────────────────────────
    def _level_row(self) -> QWidget:
        """Live level for the selected input: dBFS · bar · Test button.

        The same meter as the control panel, and it belongs here for the same
        reason the Tk wizard has one: a dead or far too quiet microphone should
        be caught during setup, not during the first sermon.
        """
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 10, 0, 0)
        row.setSpacing(10)

        self.level_value = QLabel(self._t("input_level_no_signal", "No signal"))
        # See the control panel's meter: an id rule ("muted") in the app sheet
        # would outrank the per-reading colour set below.
        self.level_value.setStyleSheet(f"color: {current_colors()['muted']};")
        self.level_value.setMinimumWidth(84)
        self.level_bar = AudioLevelBar(height=14)
        self.level_btn = QPushButton(self._t("input_level_test", "Test mic"))
        self.level_btn.setMinimumHeight(34)
        self.level_btn.setMinimumWidth(110)
        self.level_btn.clicked.connect(self._toggle_level_test)

        row.addWidget(self.level_value)
        row.addWidget(self.level_bar, 1)
        row.addWidget(self.level_btn)

        self._level_timer = QTimer(self)
        self._level_timer.timeout.connect(self._poll_level)
        return holder

    def _selected_device_index(self) -> int | None:
        pos = self.device_combo.currentIndex()
        if not self.device_indices or not (0 <= pos < len(self.device_indices)):
            return None
        return self.device_indices[pos]

    def _on_device_changed(self, _index: int) -> None:
        """Follow the selection with the meter — but only while it is running.

        Switching device must not stop the preview (auditioning inputs is the
        whole point of this step), and must not silently re-open one the user
        deliberately stopped either.
        """
        if self._level_test_running():
            self._start_level_preview(auto=True)

    def _level_test_running(self) -> bool:
        checker = getattr(self._controller, "is_input_level_test_running", None)
        if checker is None:
            return False
        try:
            return bool(checker())
        except Exception:  # noqa: BLE001 - the meter is never worth a crash
            return False

    def _start_level_preview(self, *, auto: bool) -> None:
        if self._controller is None:
            return
        try:
            self._controller.start_input_level_test(self._selected_device_index())
        except Exception as exc:  # noqa: BLE001
            log(f"Input level preview failed: {exc}", level="WARNING")
            # A device that cannot be opened is worth a dialog only when the
            # user asked for the test — not while merely browsing the list.
            if not auto:
                show_message(
                    self, "MinbarLive", str(exc), translate=self._t
                )
            self._sync_level_button()
            return
        self._level_timer.start(_LEVEL_POLL_MS)
        self._sync_level_button()

    def _stop_level_capture(self) -> None:
        self._level_timer.stop()
        stopper = getattr(self._controller, "stop_input_level_test", None)
        if stopper is not None:
            try:
                stopper()
            except Exception:  # noqa: BLE001
                pass
        self._sync_level_button()

    def _toggle_level_test(self) -> None:
        if self._level_test_running():
            self._stop_level_capture()
        else:
            self._start_level_preview(auto=False)

    def _sync_level_button(self) -> None:
        self.level_btn.setText(
            self._t("input_level_stop_test", "Stop")
            if self._level_test_running()
            else self._t("input_level_test", "Test mic")
        )

    def _poll_level(self) -> None:
        # See the control panel's meter: the APPLIED theme, not self._theme.
        colors = current_colors()
        try:
            snapshot = self._controller.get_input_level()
        except Exception:  # noqa: BLE001
            snapshot = None
        if snapshot is not None:
            value = level_fill(snapshot.rms_dbfs)
            self.level_bar.set_value(value)
            if snapshot.clipping_ratio > 0.02:
                text, colour = (
                    self._t("input_level_clipping", "Clipping!"),
                    colors["danger"],
                )
            elif value <= 0.001:
                text, colour = (
                    self._t("input_level_no_signal", "No signal"),
                    colors["muted"],
                )
            else:
                text, colour = f"{snapshot.rms_dbfs:.0f} dBFS", colors["text"]
            self.level_value.setText(text)
            self.level_value.setStyleSheet(f"color: {colour};")
        self._sync_level_button()

    def _step_provider(self) -> QWidget:
        # The general note stays at the TOP of this card (the Tk wizard puts it
        # last): it answers "why is there only one key field?" before the field
        # is reached, and the per-provider caveats below are the exceptions.
        page, inner = self._card(
            "wizard_provider_title",
            "AI provider & API key",
            "wizard_keys_info",
            "Add keys for as many providers as you like by switching the list "
            "above. With OpenAI (the default), one key covers translation, "
            "real-time transcription and Quran verse detection.",
        )
        # Deepgram (streaming STT) is listed alongside the translation
        # providers so its key can be entered here too. It has no translation
        # capability, so selecting it only reveals its key field — the active
        # translation provider is still resolved from the keys entered.
        self.provider_combo = Dropdown()
        for _name, pid in self._provider_entries():
            self.provider_combo.addItem("", pid)
        self._refresh_provider_labels()
        self.provider_combo.currentIndexChanged.connect(self._on_provider)

        inner.addSpacing(6)
        # Row label, not the card heading — repeating it reads as a bug.
        inner.addWidget(self._label("wizard_provider", "Provider"))
        inner.addWidget(self.provider_combo)
        inner.addSpacing(6)
        inner.addWidget(self._label("wizard_api_key", "API key"))
        inner.addWidget(self._key_row())

        links = QHBoxLayout()
        links.setSpacing(8)
        self.key_help_btn = QPushButton("")
        self._tr(
            self.key_help_btn,
            "wizard_key_help",
            "Where do I get an API key?",
            prefix="🛈  ",
        )
        self.key_help_btn.clicked.connect(lambda: self._open_link(_KEY_HELP_LINKS))
        self.key_site_btn = QPushButton("")
        self._tr(
            self.key_site_btn,
            "wizard_key_site",
            "Open the API key page",
            prefix="🔑  ",
        )
        self.key_site_btn.clicked.connect(lambda: self._open_link(_KEY_SITE_LINKS))
        links.addWidget(self.key_help_btn)
        links.addWidget(self.key_site_btn)
        links.addStretch(1)
        inner.addSpacing(4)
        inner.addLayout(links)

        self.key_hint = QLabel("")
        self.key_hint.setObjectName("wizard_sub")
        self.key_hint.setWordWrap(True)
        inner.addWidget(self.key_hint)

        # Provider-specific caveats, as warning callouts rather than another
        # grey note: they cost the user money or accuracy if missed. Rebuilt
        # per selection so they always match the dropdown.
        self._notes_layout = QVBoxLayout()
        self._notes_layout.setSpacing(8)
        inner.addLayout(self._notes_layout)
        inner.addStretch(1)

        # Keys entered this session, per provider — browsing away and back must
        # not lose one, and finish resolves the provider from this map.
        self._provider_keys: dict[str, str] = {}
        self._on_provider(0)
        return page

    @staticmethod
    def _provider_entries() -> list[tuple[str, str]]:
        return [*PROVIDER_CHOICES, ("Deepgram", "deepgram")]

    def _refresh_provider_labels(self) -> None:
        """(Re-)label the provider entries, tagging the shipped default.

        The tag is translated, so this runs again after a GUI-language switch.
        """
        default_tag = self._t("provider_default_tag", "Default")
        for index, (name, pid) in enumerate(self._provider_entries()):
            label = f"{name} ({default_tag})" if pid == DEFAULT_AI_PROVIDER else name
            self.provider_combo.setItemText(index, label)

    def _key_row(self) -> QWidget:
        """The key field with a show/hide toggle beside it, as in Tk."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.show_key_check = QCheckBox("")
        self._tr(self.show_key_check, "wizard_show_key", "Show")
        self.show_key_check.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )
        row.addWidget(self.key_edit, 1)
        row.addWidget(self.show_key_check)
        return holder

    def _open_link(self, table: dict) -> None:
        provider = self._current_provider()
        target = table.get(provider)
        if isinstance(target, dict):
            # German links for the German GUI, English everywhere else.
            target = target.get(self._gui_language, target.get("en"))
        if target:
            QDesktopServices.openUrl(QUrl(target))

    def _refresh_provider_notes(self) -> None:
        while self._notes_layout.count():
            item = self._notes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        provider = self._current_provider()
        if provider == "gemini":
            # Both whitelisted Live models transcribe slower than people speak
            # (live-measured 0.75x), so subtitles drift further behind the
            # longer someone talks. Scoped to real-time, which is where
            # onboarding lands; the segmented path is comfortably inside budget.
            self._notes_layout.addWidget(
                warning_box(
                    self._t(
                        "wizard_gemini_latency_note",
                        "In real-time mode, Google's models transcribe slower "
                        "than people speak, so subtitles fall further behind "
                        "during long talks and only catch up when the speaker "
                        "pauses. OpenAI keeps up better here.",
                    )
                )
            )
        elif provider == "anthropic":
            # Gemini ships its own embedding space, so the RAG caveat applies
            # to Anthropic only.
            self._notes_layout.addWidget(
                warning_box(
                    self._t(
                        "wizard_gemini_rag_note",
                        "Quran verse detection uses OpenAI embeddings. Without "
                        "an OpenAI key, verse matching is disabled.",
                    )
                )
            )
            self._notes_layout.addWidget(
                warning_box(
                    self._t(
                        "wizard_anthropic_stt_note",
                        "Claude has no speech-to-text — transcription runs on "
                        "OpenAI, so an OpenAI key is also required.",
                    )
                )
            )
        elif provider == "deepgram":
            # Deepgram is transcription-only: no translation model and no
            # embedding model, so a key here can never be the whole setup.
            self._notes_layout.addWidget(
                warning_box(
                    self._t(
                        "wizard_deepgram_note",
                        "Deepgram only does real-time transcription — it has no "
                        "translation and no embeddings for Quran verse "
                        "detection. You also need a key for a translation "
                        "provider (OpenAI, Google Gemini or Anthropic).",
                    )
                )
            )
        self.key_help_btn.setEnabled(provider in _KEY_HELP_LINKS)
        self.key_site_btn.setEnabled(provider in _KEY_SITE_LINKS)

    def _step_disclaimer(self) -> QWidget:
        page, inner = self._card("wizard_disclaimer_title", "Please note")
        # A warning callout, not body text: this is the one thing on the whole
        # wizard the user must actually read.
        self._disclaimer_box = warning_box(
            self._t(
                "wizard_disclaimer_text",
                "MinbarLive uses artificial intelligence to transcribe and "
                "translate speech in real time. AI translations can be wrong, "
                "incomplete or inaccurate — especially for religious content. "
                "The audio is sent to the selected AI provider for processing. "
                "Do not rely on the subtitles as an authoritative religious "
                "source.",
            )
        )
        # warning_box already prefixes the sign; re-translation must keep it.
        self._i18n.append(
            (
                self._disclaimer_box.label,
                "wizard_disclaimer_text",
                self._disclaimer_box.label.text()[3:],
                "⚠  ",
            )
        )
        inner.addSpacing(6)
        inner.addWidget(self._disclaimer_box)

        self.disclaimer_check = QCheckBox("")
        self._tr(
            self.disclaimer_check,
            "wizard_disclaimer_accept",
            "I understand that AI translations can be inaccurate.",
        )
        self.disclaimer_check.toggled.connect(lambda _: self._sync_nav())
        inner.addSpacing(8)
        inner.addWidget(self.disclaimer_check)
        inner.addStretch(1)
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
        self.key_edit.setPlaceholderText(get_key_placeholder(provider))
        if get_stored_api_key(provider):
            self.key_hint.setText(
                self._t(
                    "wizard_key_saved_hint",
                    "A key is already saved — leave empty to keep it.",
                )
            )
        else:
            self.key_hint.setText("")
        self._refresh_provider_notes()

    def _capture_current_key(self) -> None:
        typed = self.key_edit.text().strip()
        if typed:
            self._provider_keys[self._current_provider()] = typed

    def _retranslate(self) -> None:
        """Re-label everything after a GUI-language switch.

        Rebuilding the pages instead would be simpler but would throw away the
        language, device and key choices already made — the language step is
        reachable via Back from any later step.
        """
        for widget, key, fallback, prefix in self._i18n:
            widget.setText(prefix + self._t(key, fallback))
        self.theme_segment.set_labels(self._theme_labels())
        self._refresh_provider_labels()
        self._on_provider(self.provider_combo.currentIndex())
        self._sync_level_button()
        self._sync_nav()

    # ── navigation ───────────────────────────────────────────────────────
    def _on_step_changed(self, index: int) -> None:
        if index == _DEVICE_STEP:
            self._start_level_preview(auto=True)
        else:
            self._stop_level_capture()

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

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Cancelling mid-setup must not leave the preview holding the mic.
        self._stop_level_capture()
        super().closeEvent(event)

    # ── finish ───────────────────────────────────────────────────────────
    def _finish(self) -> None:
        self._capture_current_key()
        self._stop_level_capture()
        settings = load_settings()
        settings.gui_language = self._gui_language
        # One appearance answer drives both windows.
        settings.theme_mode = self._theme
        settings.subtitle_theme_mode = self._theme
        settings.source_language = language_canonical_name(
            self.source_combo.currentText()
        )
        settings.target_language = language_canonical_name(
            self.target_combo.currentText()
        )
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

        self.completed = True
        self.accept()


def run_onboarding(app, controller=None) -> bool:
    """Run the wizard if first-run setup is outstanding.

    Returns False when the user cancelled, so the caller can exit without
    opening the control panel. Already-completed setups return True without
    showing anything. ``controller`` only feeds the input-level meter.
    """
    if load_settings().onboarding_completed:
        return True
    wizard = OnboardingWizard(app, controller)
    wizard.exec()
    return wizard.completed

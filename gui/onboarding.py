"""First-run setup wizard.

Shown once before the main control panel when ``onboarding_completed`` is
False. Runs as its own CTk root (same pattern as the already-running dialog
in main.py) so the main window is built afterwards with the chosen GUI
language and theme from the start.

Steps:
    1. App (GUI) language + appearance (light/dark, one answer for the
       control panel and the subtitle window)
    2. Spoken + subtitle language
    3. Microphone / audio input
    4. AI provider, API key (+ per-provider "where do I get a key" video)
    5. AI-translation disclaimer (checkbox required)
"""

from __future__ import annotations

import os
import tkinter as tk
import webbrowser
from tkinter import messagebox

import customtkinter as ctk

from config import ICON_PATH, ICON_PATH_PNG
from gui.device_list import get_input_devices
from gui.dropdown import CustomDropdown
from providers import (
    PROVIDER_CHOICES,
    get_default_model,
    get_stored_api_key,
    get_streaming_key_provider,
    resolve_provider_by_keys,
    save_api_key,
)
from utils.icons import ICO_SUPPORTED, scaled_icon_photo
from utils.logging import log
from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    GUI_LANGUAGES,
    PIPELINE_MODE_STREAMING,
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    TARGET_LANGUAGE_NAMES,
    load_settings,
    save_settings,
)

# Translation providers that have a real-time streaming engine of their own
# (same API key). Anthropic has none — see _finish for its fallback.
_REALTIME_ENGINE_FOR_PROVIDER = {
    "gemini": "gemini_realtime",
    "openai": "openai_realtime",
}

_DARK = {
    "app_bg": "#0b1020",
    "card": "#111827",
    "border": "#263449",
    "text": "#f8fafc",
    "muted": "#9ca3af",
    "accent": "#16a34a",
    "accent_hover": "#15803d",
    "button": "#1f2a44",
    "button_hover": "#263654",
    "entry": "#0f172a",
    "entry_border": "#334155",
    "warning": "#d97706",
    "danger": "#dc2626",
}
_LIGHT = {
    "app_bg": "#f8fafc",
    "card": "#ffffff",
    "border": "#cbd5e1",
    "text": "#111827",
    "muted": "#6b7280",
    "accent": "#16a34a",
    "accent_hover": "#15803d",
    "button": "#e2e8f0",
    "button_hover": "#cbd5e1",
    "entry": "#ffffff",
    "entry_border": "#94a3b8",
    "warning": "#b45309",
    "danger": "#dc2626",
}

_W, _H = 680, 640

# "Where do I get an API key?" video tutorials, per provider and GUI
# language. Languages without their own link fall back to English.
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
    "deepgram": {
        "en": "https://www.youtube.com/watch?v=FVJEkE69ei0",
    },
}

# Direct links to each provider's API-key console page (shown as a second
# button next to the video tutorial).
_KEY_SITE_LINKS: dict[str, str] = {
    "gemini": "https://aistudio.google.com/api-keys",
    "openai": "https://platform.openai.com/api-keys",
    "anthropic": "https://console.anthropic.com/settings/keys",
    "deepgram": "https://console.deepgram.com/",
}


class OnboardingWizard(ctk.CTk):
    """Five-step first-run configuration wizard."""

    TOTAL_STEPS = 5

    def __init__(self) -> None:
        super().__init__()

        settings = load_settings()
        self._state = {
            "gui_language": settings.gui_language,
            "theme_mode": settings.theme_mode,
            "source_language": settings.source_language,
            "target_language": settings.target_language,
            "device_name": settings.input_device_name,
            "selected_provider": settings.ai_provider,  # which key field is shown
            "provider_keys": {},  # provider_id -> key typed this session
        }
        self.completed = False
        self._step = 0
        self._texts = self._load_texts()
        self._c = _DARK if self._state["theme_mode"] != "light" else _LIGHT
        self._devices = get_input_devices()
        self._disclaimer_var = tk.BooleanVar(value=False)

        ctk.set_appearance_mode(self._state["theme_mode"])
        self.configure(fg_color=self._c["app_bg"])
        self.resizable(False, False)
        self.update_idletasks()
        x = (self.winfo_screenwidth() - _W) // 2
        y = (self.winfo_screenheight() - _H) // 2
        self.geometry(f"{_W}x{_H}+{x}+{y}")
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_cancel)

        # ── Persistent chrome: header, step container, nav bar ────────────
        self._header = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=self._c["text"],
        )
        self._header.pack(pady=(26, 2))

        self._step_label = ctk.CTkLabel(
            self,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._c["muted"],
        )
        self._step_label.pack(pady=(0, 10))

        self._container = ctk.CTkFrame(
            self,
            fg_color=self._c["card"],
            border_color=self._c["border"],
            border_width=1,
            corner_radius=20,
        )
        self._container.pack(fill="both", expand=True, padx=28, pady=(0, 14))

        nav = ctk.CTkFrame(self, fg_color="transparent")
        nav.pack(fill="x", padx=28, pady=(0, 22))
        nav.grid_columnconfigure(0, weight=1)
        nav.grid_columnconfigure(1, weight=1)

        self._back_btn = ctk.CTkButton(
            nav,
            text="",
            command=self._on_back,
            height=46,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=self._c["button"],
            hover_color=self._c["button_hover"],
            text_color=self._c["text"],
        )
        self._back_btn.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self._next_btn = ctk.CTkButton(
            nav,
            text="",
            command=self._on_next,
            height=46,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=self._c["accent"],
            hover_color=self._c["accent_hover"],
            text_color="#ffffff",
        )
        self._next_btn.grid(row=0, column=1, sticky="ew", padx=(8, 0))

        self._render()

    # ── Helpers ────────────────────────────────────────────────────────────

    def _load_texts(self) -> dict:
        # Imported lazily to avoid a circular import at module load
        from gui.app_gui import load_gui_translations

        return load_gui_translations(self._state["gui_language"])

    def _t(self, key: str, default: str) -> str:
        return self._texts.get(key, default)

    def _set_icon(self) -> None:
        self.title("MinbarLive")
        if ICO_SUPPORTED and os.path.exists(ICON_PATH):
            def _set_win_icon() -> None:
                # Guard inside the callback: the outer try can't catch an
                # exception raised 200 ms later inside Tk's event loop.
                try:
                    self.iconbitmap(ICON_PATH)
                except Exception:
                    pass

            self.after(200, _set_win_icon)
        elif os.path.exists(ICON_PATH_PNG):
            try:
                self.iconphoto(True, scaled_icon_photo(ICON_PATH_PNG))
            except Exception:
                pass

    def _section_label(
        self, parent, text: str, muted: bool = False, pady=(14, 2)
    ) -> ctk.CTkLabel:
        label = ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(family="Segoe UI", size=14 if muted else 15,
                             weight="normal" if muted else "bold"),
            text_color=self._c["muted"] if muted else self._c["text"],
            wraplength=_W - 130,
            justify="left",
            anchor="w",
        )
        label.pack(anchor="w", padx=26, pady=pady)
        return label

    def _warning_box(self, parent, text: str, pady=(14, 4)) -> ctk.CTkFrame:
        """A bordered, warning-colored callout. Used for the provider caveats
        and the AI-accuracy disclaimer so they stand out from the muted info
        text instead of blending in as grey notes."""
        box = ctk.CTkFrame(
            parent,
            fg_color="transparent",
            border_color=self._c["warning"],
            border_width=2,
            corner_radius=12,
        )
        box.pack(fill="x", padx=26, pady=pady)
        ctk.CTkLabel(
            box,
            text="⚠  " + text,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=self._c["warning"],
            wraplength=_W - 170,
            justify="left",
            anchor="w",
        ).pack(anchor="w", padx=14, pady=10)
        return box

    def _combo(self, parent, values: list[str]) -> CustomDropdown:
        combo = CustomDropdown(
            parent,
            values=values,
            height=44,
            corner_radius=12,
            border_width=1,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            dropdown_font=ctk.CTkFont(family="Segoe UI", size=14),
            fg_color=self._c["entry"],
            border_color=self._c["entry_border"],
            button_color=self._c["button"],
            button_hover_color=self._c["button_hover"],
            text_color=self._c["text"],
            dropdown_fg_color=self._c["card"],
            dropdown_hover_color=self._c["button_hover"],
            dropdown_text_color=self._c["text"],
        )
        combo.pack(fill="x", padx=26, pady=(2, 4))
        return combo

    def _key_entry_row(self, prefill: str) -> ctk.CTkEntry:
        """One API-key entry with a show/hide toggle."""
        row = ctk.CTkFrame(self._container, fg_color="transparent")
        row.pack(fill="x", padx=26, pady=(2, 0))
        row.grid_columnconfigure(0, weight=1)

        entry = ctk.CTkEntry(
            row,
            show="●",
            height=44,
            corner_radius=12,
            border_width=1,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            fg_color=self._c["entry"],
            border_color=self._c["entry_border"],
            text_color=self._c["text"],
        )
        entry.grid(row=0, column=0, sticky="ew")
        if prefill:
            entry.insert(0, prefill)

        show_var = tk.BooleanVar(value=False)
        show_cb = ctk.CTkCheckBox(
            row,
            text=self._t("wizard_show_key", "Show"),
            variable=show_var,
            command=lambda e=entry, v=show_var: e.configure(
                show="" if v.get() else "●"
            ),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._c["muted"],
        )
        show_cb.grid(row=0, column=1, padx=(10, 0))
        return entry

    def _apply_theme(self, theme_mode: str) -> None:
        """Switch the wizard's palette live (light/dark) and re-render the
        current step; the persistent chrome is reconfigured in place."""
        self._state["theme_mode"] = theme_mode
        self._c = _LIGHT if theme_mode == "light" else _DARK
        ctk.set_appearance_mode(theme_mode)
        self.configure(fg_color=self._c["app_bg"])
        self._header.configure(text_color=self._c["text"])
        self._step_label.configure(text_color=self._c["muted"])
        self._container.configure(
            fg_color=self._c["card"], border_color=self._c["border"]
        )
        self._back_btn.configure(
            fg_color=self._c["button"],
            hover_color=self._c["button_hover"],
            text_color=self._c["text"],
        )
        self._next_btn.configure(
            fg_color=self._c["accent"], hover_color=self._c["accent_hover"]
        )
        self._render()

    def _capture_current_key(self) -> None:
        """Remember the key typed for the currently-selected provider so it
        survives switching the provider dropdown and finishing."""
        entry = getattr(self, "_key_entry", None)
        if entry is not None:
            try:
                self._state["provider_keys"][
                    self._state["selected_provider"]
                ] = entry.get().strip()
            except Exception:
                pass

    # ── Rendering ──────────────────────────────────────────────────────────

    def _render(self) -> None:
        self._header.configure(text=self._t("wizard_title", "Welcome to MinbarLive"))
        step_fmt = self._t("wizard_step_of", "Step {current} of {total}")
        self._step_label.configure(
            text=step_fmt.format(current=self._step + 1, total=self.TOTAL_STEPS)
        )
        self._back_btn.configure(
            text=self._t("wizard_back", "Back"),
            state="normal" if self._step > 0 else "disabled",
        )
        if self._step == self.TOTAL_STEPS - 1:
            self._next_btn.configure(text=self._t("wizard_finish", "Finish"))
            self._update_finish_state()
        else:
            self._next_btn.configure(
                text=self._t("wizard_next", "Next"), state="normal"
            )

        for child in self._container.winfo_children():
            child.destroy()

        builder = [
            self._build_step_ui_language,
            self._build_step_languages,
            self._build_step_audio,
            self._build_step_provider,
            self._build_step_disclaimer,
        ][self._step]
        builder()
        self._fit_height()

    def _fit_height(self) -> None:
        """Grow the window when a step needs more room than the base height
        (the stacked provider notes on the key step overflow it under
        Anthropic in the more verbose GUI languages) and shrink back when
        the content fits again.

        winfo_reqheight() is physical px but CTk.geometry() scales WxH by
        the DPI factor, so convert to the logical units it expects (same
        pattern as BatchViewMixin._resize_batch_window); +X+Y pass through
        unscaled.
        """
        self.update_idletasks()
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        h = max(_H, int(self.winfo_reqheight() / scaling) + 1)
        self.geometry(f"{_W}x{h}+{self.winfo_x()}+{self.winfo_y()}")

    # ── Step 1: GUI language ───────────────────────────────────────────────

    def _build_step_ui_language(self) -> None:
        self._section_label(
            self._container, self._t("wizard_ui_language_title", "App language")
        )
        self._section_label(
            self._container,
            self._t(
                "wizard_ui_language_sub",
                "Language of the control panel (subtitles use their own setting).",
            ),
            muted=True,
        )
        names = [name for _code, name in GUI_LANGUAGES]
        codes = [code for code, _name in GUI_LANGUAGES]
        combo = self._combo(self._container, names)
        current = self._state["gui_language"]
        combo.set(names[codes.index(current)] if current in codes else names[0])

        def _on_change(value: str) -> None:
            self._state["gui_language"] = codes[names.index(value)]
            self._texts = self._load_texts()
            self._render()  # re-render chrome + step in the new language

        combo.configure(command=_on_change)

        # One appearance question for the whole app (control panel AND
        # subtitle window) — applies to the wizard immediately.
        self._section_label(
            self._container,
            self._t("wizard_theme_label", "Appearance"),
            muted=True,
        )
        theme_names = [
            self._t("theme_light", "Light"),
            self._t("theme_dark", "Dark"),
        ]
        theme_ids = ["light", "dark"]
        theme_combo = self._combo(self._container, theme_names)
        theme_combo.set(theme_names[theme_ids.index(self._state["theme_mode"])])
        theme_combo.configure(
            command=lambda v: self._apply_theme(theme_ids[theme_names.index(v)])
        )

    # ── Step 2: source/target languages ────────────────────────────────────

    def _build_step_languages(self) -> None:
        self._section_label(
            self._container,
            self._t("wizard_languages_title", "Translation languages"),
        )

        self._section_label(
            self._container,
            self._t("wizard_source_language", "Spoken language (source)"),
            muted=True,
        )
        # Real-time (the default transcription mode) can't auto-detect the
        # source language, so "Automatic" is not offered here. Sync the state
        # to a concrete language up front — .set() does not fire the command.
        source_names = [name for name, code in SOURCE_LANGUAGES if code is not None]
        if self._state["source_language"] not in source_names:
            self._state["source_language"] = source_names[0]
        source_combo = self._combo(self._container, source_names)
        source_combo.set(self._state["source_language"])
        source_combo.configure(
            command=lambda v: self._state.__setitem__("source_language", v)
        )

        self._section_label(
            self._container,
            self._t("wizard_target_language", "Subtitle language (target)"),
            muted=True,
        )
        target_combo = self._combo(self._container, TARGET_LANGUAGE_NAMES)
        target_combo.set(
            self._state["target_language"]
            if self._state["target_language"] in TARGET_LANGUAGE_NAMES
            else TARGET_LANGUAGE_NAMES[0]
        )
        target_combo.configure(
            command=lambda v: self._state.__setitem__("target_language", v)
        )

    # ── Step 3: audio input ────────────────────────────────────────────────

    def _build_step_audio(self) -> None:
        self._section_label(
            self._container, self._t("wizard_audio_title", "Input Device")
        )
        self._section_label(
            self._container,
            self._t(
                "wizard_audio_sub",
                "Choose the audio input used for the live translation.",
            ),
            muted=True,
        )

        display_names, base_names, _indices, _loopback = self._devices
        if not display_names:
            self._section_label(
                self._container,
                self._t(
                    "wizard_no_devices",
                    "No input devices found — you can choose one later in the "
                    "control panel.",
                ),
                muted=True,
            )
            return

        combo = self._combo(self._container, display_names)
        saved = self._state["device_name"]
        if saved in base_names:
            combo.set(display_names[base_names.index(saved)])
        else:
            combo.set(display_names[0])
            self._state["device_name"] = base_names[0]

        combo.configure(
            command=lambda v: self._state.__setitem__(
                "device_name", base_names[display_names.index(v)]
            )
        )

    # ── Step 4: provider + model + API key ─────────────────────────────────

    def _build_step_provider(self) -> None:
        self._section_label(
            self._container,
            self._t("wizard_provider_title", "AI provider & API key"),
        )

        # Deepgram (real-time streaming STT) is listed alongside the translation
        # providers so its key can be entered here too. Selecting a translation
        # provider also makes it the active one; selecting Deepgram only reveals
        # its key field (it has no translation capability).
        choices = list(PROVIDER_CHOICES) + [("Deepgram (real-time)", "deepgram")]
        provider_names = [name for name, _pid in choices]
        provider_ids = [pid for _name, pid in choices]

        self._section_label(
            self._container, self._t("wizard_provider", "Provider"), muted=True
        )
        provider_combo = self._combo(self._container, provider_names)
        selected = self._state["selected_provider"]
        provider_combo.set(
            provider_names[provider_ids.index(selected)]
            if selected in provider_ids
            else provider_names[0]
        )

        def _on_provider_change(value: str) -> None:
            self._capture_current_key()  # remember the key for the old provider
            new = provider_ids[provider_names.index(value)]
            self._state["selected_provider"] = new
            self._render()

        provider_combo.configure(command=_on_provider_change)

        # No model picker — _finish uses the resolved provider's default model.

        # One key field, for whichever provider is selected. Keys typed for other
        # providers are remembered and all saved on finish, so several provider
        # keys can be added just by switching the dropdown — no stacked fields.
        self._section_label(
            self._container, self._t("wizard_api_key", "API key"), muted=True
        )
        self._key_entry = self._key_entry_row(
            self._state["provider_keys"].get(selected, "")
        )

        # Video tutorial + direct key-console link for the selected provider.
        # Tutorial in the wizard's GUI language when available (falls back to
        # English); the console link is per provider.
        help_links = _KEY_HELP_LINKS.get(selected, {})
        help_url = help_links.get(self._state["gui_language"]) or help_links.get("en")
        site_url = _KEY_SITE_LINKS.get(selected)
        if help_url or site_url:
            btn_row = ctk.CTkFrame(self._container, fg_color="transparent")
            btn_row.pack(anchor="w", padx=26, pady=(10, 0))
            for text, url in (
                ("🛈  " + self._t("wizard_key_help", "Where do I get an API key?"),
                 help_url),
                ("🔑  " + self._t("wizard_key_site", "Open the API key page"),
                 site_url),
            ):
                if not url:
                    continue
                ctk.CTkButton(
                    btn_row,
                    text=text,
                    command=lambda u=url: webbrowser.open(u),
                    height=34,
                    corner_radius=10,
                    font=ctk.CTkFont(family="Segoe UI", size=13),
                    fg_color=self._c["button"],
                    hover_color=self._c["button_hover"],
                    text_color=self._c["text"],
                ).pack(side="left", padx=(0, 8))

        if get_stored_api_key(selected):
            self._section_label(
                self._container,
                self._t(
                    "wizard_key_saved_hint",
                    "A key is already saved — leave empty to keep it.",
                ),
                muted=True,
                pady=(14, 10),
            )

        self._section_label(
            self._container,
            self._t(
                "wizard_keys_info",
                "Add keys for as many providers as you like by switching the "
                "list above. With Google Gemini (the default), one key covers "
                "both translation and real-time transcription.",
            ),
            muted=True,
            pady=(14, 10),
        )

        # Provider-specific notes — keyed to the SELECTED provider so they
        # match the dropdown. Gemini has its own bundled embedding space, so
        # the RAG note only applies to Anthropic (embeddings stay OpenAI).
        # Shown as warning callouts so users don't miss the extra-key caveat.
        if selected == "anthropic":
            self._warning_box(
                self._container,
                self._t(
                    "wizard_gemini_rag_note",
                    "Quran verse detection uses OpenAI embeddings. Without an "
                    "OpenAI key, verse matching is disabled.",
                ),
                pady=(14, 4),
            )
            self._warning_box(
                self._container,
                self._t(
                    "wizard_anthropic_stt_note",
                    "Claude has no speech-to-text — transcription runs on "
                    "OpenAI, so an OpenAI key is also required.",
                ),
                pady=(8, 10),
            )

    # ── Step 5: disclaimer ─────────────────────────────────────────────────

    def _build_step_disclaimer(self) -> None:
        self._section_label(
            self._container, self._t("wizard_disclaimer_title", "Please note")
        )
        self._warning_box(
            self._container,
            self._t(
                "wizard_disclaimer_text",
                "MinbarLive uses artificial intelligence to transcribe and "
                "translate speech in real time. AI translations can be wrong, "
                "incomplete or inaccurate — especially for religious content. "
                "The audio is sent to the selected AI provider for processing. "
                "Do not rely on the subtitles as an authoritative religious "
                "source.",
            ),
        )

        cb = ctk.CTkCheckBox(
            self._container,
            text=self._t(
                "wizard_disclaimer_accept",
                "I understand that AI translations can be inaccurate.",
            ),
            variable=self._disclaimer_var,
            command=self._update_finish_state,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            text_color=self._c["text"],
        )
        cb.pack(anchor="w", padx=26, pady=(18, 10))

    def _update_finish_state(self) -> None:
        if self._step == self.TOTAL_STEPS - 1:
            self._next_btn.configure(
                state="normal" if self._disclaimer_var.get() else "disabled"
            )

    # ── Navigation ─────────────────────────────────────────────────────────

    def _validate_step(self) -> bool:
        if self._step == 3:  # provider + keys
            # Keys are optional here — capture whatever was typed, never block.
            # Missing keys are prompted when the user presses Start.
            self._capture_current_key()
        return True

    def _on_back(self) -> None:
        if self._step > 0:
            if self._step == 3:  # keep typed keys when navigating away
                self._capture_current_key()
            self._step -= 1
            self._render()

    def _on_next(self) -> None:
        if not self._validate_step():
            return
        if self._step < self.TOTAL_STEPS - 1:
            self._step += 1
            self._render()
        else:
            self._finish()

    def _on_cancel(self) -> None:
        self.quit()

    def _finish(self) -> None:
        self._capture_current_key()
        settings = load_settings()
        settings.gui_language = self._state["gui_language"]
        # One appearance answer drives both windows
        settings.theme_mode = self._state["theme_mode"]
        settings.subtitle_theme_mode = self._state["theme_mode"]
        settings.source_language = self._state["source_language"]
        settings.target_language = self._state["target_language"]
        if self._state["device_name"]:
            settings.input_device_name = self._state["device_name"]
        # Keys decide the provider, not the dropdown's last position (browsing
        # to a provider without entering its key must not select it): the
        # default (Gemini) wins whenever its key exists or no key was given at
        # all; otherwise the highest-ranked provider with a key is used and
        # "Use default" is unchecked so the control panel shows the real one.
        provider = resolve_provider_by_keys(self._state["provider_keys"])
        settings.ai_provider = provider
        settings.translation_model = get_default_model(provider, "translation")
        settings.use_default_translation_model = provider == DEFAULT_AI_PROVIDER
        # Onboarding lands on real-time streaming — on the engine that
        # belongs to the CHOSEN provider, so the key the user just entered is
        # the one the pipeline authenticates with. (A pinned Gemini engine
        # used to prompt OpenAI-only users for a Gemini key on first Start.)
        # Anthropic has no realtime engine of its own: use the first engine
        # whose key exists (entered this session or already stored), falling
        # back to the app default.
        engine = _REALTIME_ENGINE_FOR_PROVIDER.get(provider)
        if engine is None:
            engine = DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
            for candidate in STREAMING_TRANSCRIPTION_PROVIDERS:
                key_provider = get_streaming_key_provider(candidate)
                if self._state["provider_keys"].get(key_provider) or get_stored_api_key(
                    key_provider
                ):
                    engine = candidate
                    break
        settings.transcription_provider = engine
        settings.pipeline_mode = PIPELINE_MODE_STREAMING
        settings.transcription_model = get_default_model(engine, "transcription")
        # "Use default" only when the engine IS the default one — a greyed
        # non-default engine next to a ticked "Standard" reads as broken.
        settings.use_default_transcription_model = (
            engine == DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
        )
        settings.disclaimer_accepted = True
        settings.onboarding_completed = True
        save_settings(settings)

        # Persist every provider key entered this session. Only the translation
        # provider's key surfaces the insecure-storage warning (once) so several
        # keys don't stack dialogs.
        insecure = False
        for pid, key in self._state.get("provider_keys", {}).items():
            if not key:
                continue
            stored = save_api_key(pid, key)
            if pid == provider and not stored:
                insecure = True
        if insecure:
            messagebox.showwarning(
                "MinbarLive",
                self._t(
                    "dlg_key_insecure_warning",
                    "Keyring unavailable — key will be stored unencrypted.",
                ),
                parent=self,
            )

        log("Onboarding completed.", level="INFO")
        self.completed = True
        self.quit()


def run_onboarding() -> bool:
    """Show the first-run wizard when it has not been completed yet.

    Returns:
        True when the app should continue starting (wizard finished, or already
        completed on an earlier run). False when the wizard was closed without
        finishing — the app should exit, even if a key from a previous run
        happens to be stored (closing the wizard means setup wasn't confirmed).
    """
    settings = load_settings()
    if settings.onboarding_completed:
        return True

    wizard = OnboardingWizard()
    wizard.mainloop()
    completed = wizard.completed

    # Hide the window the instant its loop ends. On some setups the CTk root's
    # destroy() below does not fully tear the window down — it can be left as a
    # blank white shell next to the control panel. Withdrawing first guarantees
    # the user never sees a stray onboarding window, and can never close it:
    # as the process's first Tk root, closing it would take the whole app down.
    try:
        wizard.withdraw()
    except Exception:
        pass

    # Cancel any still-pending after() callbacks (CustomTkinter's DPI-scaling
    # tracker, a button's click animation, scheduled updates) before tearing
    # the root down — otherwise they fire on destroyed widgets and Tcl prints
    # "invalid command name ...check_dpi_scaling/_click_animation" noise.
    try:
        for after_id in wizard.tk.call("after", "info"):
            try:
                wizard.after_cancel(after_id)
            except Exception:
                pass
    except Exception:
        pass
    try:
        wizard.destroy()
    except Exception as exc:
        log(f"Onboarding wizard teardown error: {exc}", level="WARNING")

    _reset_wizard_root_state(wizard)
    return completed


def _reset_wizard_root_state(wizard: OnboardingWizard) -> None:
    """Clear process-global state still pointing at the destroyed wizard root.

    The wizard was the first Tk root (Tk's default root) and ``CustomDropdown``
    caches, at class level, the root its global handlers are bound to plus the
    last-open dropdown. Left untouched, both reference the dead wizard, so the
    main window's root — created next — inherits stale references. Clearing them
    (and dropping the default-root pointer if destroy left it dangling) lets the
    control panel's root start from a clean slate.
    """
    try:
        CustomDropdown._active = None
        if CustomDropdown._bound_root is wizard:
            CustomDropdown._bound_root = None
    except Exception:
        pass
    try:
        if tk._default_root is wizard:
            tk._default_root = None
    except Exception:
        pass

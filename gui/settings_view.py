"""Settings window (mixin for AppGUI).

Moved verbatim out of gui/app_gui.py: the ⚙ Settings Toplevel (GUI
language, control-panel + subtitle theme, per-provider API-key management)
and the API-key status/change/remove handlers it wires up. Uses AppGUI
services: ``_colors``, ``gui_texts``, ``_label`` / ``_combo`` / ``_button``
/ ``_set_toplevel_icon`` / ``_setup_autohide_scrollbar`` (from
WidgetFactoryMixin), ``on_change_key`` and ``_saved_settings``. Unlike the
history/batch windows this one SURVIVES theme + language switches (it hosts
those toggles) — AppGUI._apply_theme / _update_all_ui_texts re-style its
widgets in place via the ``_settings_*`` registries populated here.
"""

import tkinter as tk

import customtkinter as ctk

from gui.control_dashboard import ICON_FONT, ICONS, provider_display_name
from gui.scaling import centered_position
from providers import PROVIDER_CHOICES, has_usable_key
from utils.api_key_manager import remove_api_key
from utils.logging import log
from utils.settings import GUI_LANGUAGE_CODES, GUI_LANGUAGES
from version import __version__


class SettingsViewMixin:
    """Settings window + API-key management UI, hosted by AppGUI."""

    def _settings_win_exists(self) -> bool:
        return (
            hasattr(self, "_settings_win")
            and self._settings_win is not None
            and self._settings_win.winfo_exists()
        )

    def _open_settings_window(self) -> None:
        if self._settings_win_exists():
            self._settings_win.lift()
            self._settings_win.focus()
            return

        win = ctk.CTkToplevel(self)
        win.title(self.gui_texts.get("settings_title", "Settings"))
        win.resizable(False, False)
        win.configure(fg_color=self._colors["app_bg"])
        # Build fully transparent to hide the white first-paint flash, then fade
        # to opaque once themed. Alpha (not withdraw()) so the window is never
        # unmapped — withdraw + this window's transient/topmost surface-restore
        # can leave it hidden for good (see the titlebar note in history).
        try:
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            pass

        win.after(200, lambda: self._set_toplevel_icon(win))
        win.transient(self)
        self.update_idletasks()
        x, y = centered_position(self, 500, 620)
        win.geometry(f"500x620+{x}+{y}")
        self._settings_win = win
        self._settings_labels = []
        self._settings_muted_labels = []
        self._settings_buttons = []
        self._settings_combos = []
        self._settings_cards: list[ctk.CTkFrame] = []
        self._settings_symbol_labels: list[ctk.CTkLabel] = []
        self._settings_checkboxes: list[ctk.CTkCheckBox] = []
        self._settings_switches: list[ctk.CTkSwitch] = []

        # Track list lengths before building settings widgets so we can move
        # any entries the helpers add back into settings-specific lists.
        _lbl_start = len(self._labels)
        _sec_start = len(self._section_titles)
        _btn_start = len(self._buttons)
        _cmb_start = len(self._combos)
        _chk_start = len(self._checkboxes)

        # Bottom bar (packed before scroll so it anchors to the bottom)
        self._settings_bottom_bar = ctk.CTkFrame(
            win,
            fg_color=self._colors["sidebar"],
            height=54,
            corner_radius=0,
        )
        self._settings_bottom_bar.pack(fill="x", side="bottom")

        self._settings_scroll = ctk.CTkScrollableFrame(
            win, fg_color=self._colors["sidebar"], corner_radius=0
        )
        self._settings_scroll.pack(fill="both", expand=True)
        self._settings_scroll.grid_columnconfigure(0, weight=1)
        win.after(300, lambda: self._setup_autohide_scrollbar(self._settings_scroll))
        scroll = self._settings_scroll

        # App info header
        info_label = ctk.CTkLabel(
            scroll,
            text=f"MinbarLive  —  v{__version__}",
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            text_color=self._colors["text"],
        )
        info_label.grid(row=0, column=0, sticky="w", padx=22, pady=(20, 6))
        self._settings_labels = getattr(self, "_settings_labels", [])
        self._settings_labels.append(info_label)

        subtitle_label = ctk.CTkLabel(
            scroll,
            text=self.gui_texts.get("hero_subtitle", "Live translation control center"),
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._colors["muted"],
        )
        subtitle_label.grid(row=1, column=0, sticky="w", padx=22, pady=(0, 18))
        subtitle_label._text_key = "hero_subtitle"  # type: ignore[attr-defined]
        self._settings_muted_labels.append(subtitle_label)

        # Language
        lang_label = self._label(scroll, "language", symbol="◎", size=14, weight="bold")
        lang_label.grid(row=2, column=0, sticky="w", padx=22, pady=(0, 4))
        self._gui_lang_names = [name for _, name in GUI_LANGUAGES]
        self.gui_lang_combo = self._combo(
            scroll,
            values=self._gui_lang_names,
            command=lambda _value: self._on_gui_language_change(),
        )
        if self.gui_lang_code in GUI_LANGUAGE_CODES:
            gui_lang_name = self._gui_lang_names[
                GUI_LANGUAGE_CODES.index(self.gui_lang_code)
            ]
        else:
            gui_lang_name = self._gui_lang_names[0]
        self.gui_lang_combo.set(gui_lang_name)
        self.gui_lang_combo.grid(row=3, column=0, sticky="ew", padx=22, pady=(0, 18))

        # Theme
        theme_label = self._label(
            scroll, "theme_mode", symbol="☾", size=14, weight="bold"
        )
        theme_label.grid(row=4, column=0, sticky="w", padx=22, pady=(0, 4))
        self.theme_segment = ctk.CTkSegmentedButton(
            scroll,
            values=[
                self.gui_texts.get("theme_dark", "Dark"),
                self.gui_texts.get("theme_light", "Light"),
            ],
            command=self._on_theme_change,
            height=44,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=self._colors["button"],
            selected_color=self._colors["accent"],
            selected_hover_color=self._colors["accent_hover"],
            unselected_color=self._colors["button"],
            unselected_hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self.theme_segment.set(
            self.gui_texts.get(
                "theme_light" if self._theme_mode == "light" else "theme_dark", "Dark"
            )
        )
        self.theme_segment.grid(row=5, column=0, sticky="ew", padx=22, pady=(0, 12))

        # Subtitle Theme
        subtitle_theme_label = self._label(
            scroll, "subtitle_theme_mode", symbol="▣", size=14, weight="bold"
        )
        subtitle_theme_label.grid(row=6, column=0, sticky="w", padx=22, pady=(0, 4))
        _sub_mode = getattr(
            self._saved_settings, "subtitle_theme_mode", self._theme_mode
        )
        self.subtitle_theme_segment = ctk.CTkSegmentedButton(
            scroll,
            values=[
                self.gui_texts.get("theme_dark", "Dark"),
                self.gui_texts.get("theme_light", "Light"),
            ],
            command=self._on_subtitle_theme_change,
            height=44,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=self._colors["button"],
            selected_color=self._colors["accent"],
            selected_hover_color=self._colors["accent_hover"],
            unselected_color=self._colors["button"],
            unselected_hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self.subtitle_theme_segment.set(
            self.gui_texts.get(
                "theme_light" if _sub_mode == "light" else "theme_dark", "Dark"
            )
        )
        self.subtitle_theme_segment.grid(
            row=7, column=0, sticky="ew", padx=22, pady=(0, 24)
        )

        # Islamic mode — one switch. Turning it OFF asks for confirmation
        # (so it can't be disabled by accident); turning it ON is instant.
        islamic_label = self._label(
            scroll, "islamic_mode", symbol="☪", size=14, weight="bold"
        )
        islamic_label.grid(row=8, column=0, sticky="w", padx=22, pady=(0, 4))

        self._islamic_mode_var = tk.BooleanVar(
            value=self._saved_settings.islamic_mode
        )
        self.islamic_mode_switch = ctk.CTkSwitch(
            scroll,
            text=self.gui_texts.get("islamic_mode_enabled", "Enabled"),
            variable=self._islamic_mode_var,
            command=self._on_islamic_mode_change,
            height=34,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            progress_color=self._colors["accent"],
            fg_color=self._colors["entry_border"],
            button_color="#ffffff",
            button_hover_color="#e5e7eb",
            text_color=self._colors["text"],
        )
        self.islamic_mode_switch._text_key = "islamic_mode_enabled"  # type: ignore[attr-defined]
        self._settings_switches.append(self.islamic_mode_switch)
        self.islamic_mode_switch.grid(
            row=9, column=0, sticky="w", padx=22, pady=(0, 4)
        )

        islamic_hint = ctk.CTkLabel(
            scroll,
            text=self.gui_texts.get(
                "islamic_mode_hint",
                "Quran verse & Athan recognition and Islamic translation "
                "style. Off = general translator for any content.",
            ),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self._colors["muted"],
            anchor="w",
            justify="left",
            wraplength=440,
        )
        islamic_hint.grid(row=10, column=0, sticky="w", padx=22, pady=(0, 18))
        islamic_hint._text_key = "islamic_mode_hint"  # type: ignore[attr-defined]
        self._settings_muted_labels.append(islamic_hint)

        # Update check (one anonymous GitHub API request at startup, opt-out)
        self._check_updates_var = tk.BooleanVar(
            value=self._saved_settings.check_for_updates
        )
        update_cb = self._checkbox(
            scroll,
            "check_updates_on_launch",
            self._check_updates_var,
            self._on_check_updates_change,
        )
        update_cb.grid(row=11, column=0, sticky="w", padx=22, pady=(0, 12))

        # API Key section — styled card
        api_card = ctk.CTkFrame(
            scroll,
            fg_color=self._colors["card"],
            border_color=self._colors["border"],
            border_width=2,
            corner_radius=24,
        )
        api_card.grid(sticky="ew", padx=18, pady=(18, 16))
        api_card.grid_columnconfigure(0, weight=1, uniform="api_btns")
        api_card.grid_columnconfigure(1, weight=1, uniform="api_btns")
        self._settings_cards.append(api_card)

        api_header = ctk.CTkFrame(api_card, fg_color="transparent")
        api_header.grid(
            row=0, column=0, columnspan=2, sticky="ew", padx=20, pady=(18, 10)
        )
        api_header.grid_columnconfigure(1, weight=1)

        api_symbol = ctk.CTkLabel(
            api_header,
            text=ICONS["key"],
            font=ctk.CTkFont(family=ICON_FONT, size=20),
            text_color=self._colors["accent"],
            width=44,
            height=44,
            fg_color=self._colors["panel_soft"],
            corner_radius=16,
        )
        api_symbol.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self._settings_symbol_labels.append(api_symbol)

        api_title = ctk.CTkLabel(
            api_header,
            text=self.gui_texts.get("api_key_section", "API Key"),
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=self._colors["text"],
            anchor="w",
        )
        api_title.grid(row=0, column=1, sticky="ew")
        api_title._text_key = "api_key_section"  # type: ignore[attr-defined]
        self._settings_labels.append(api_title)

        # Provider selector — Change/Remove act on the chosen provider, so keys
        # for every provider (OpenAI, Gemini, Anthropic, Deepgram) can be
        # managed here, not just the active translation provider.
        self._api_key_provider_choices = [*PROVIDER_CHOICES, ("Deepgram", "deepgram")]
        self._api_key_provider_ids = [
            pid for _name, pid in self._api_key_provider_choices
        ]
        self.api_key_provider_combo = self._combo(
            api_card,
            [name for name, _pid in self._api_key_provider_choices],
            command=lambda _value: self._refresh_api_key_status(),
        )
        # Start with no provider chosen — buttons stay disabled and the status
        # shows "—" until the user picks one.
        self.api_key_provider_combo.set(
            self.gui_texts.get("api_key_select_provider", "Please Select")
        )
        self.api_key_provider_combo.grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 10)
        )

        self.api_key_status_label = ctk.CTkLabel(
            api_card,
            text="",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=self._colors["muted"],
            anchor="w",
        )
        self.api_key_status_label.grid(
            row=2, column=0, columnspan=2, sticky="w", padx=18, pady=(0, 12)
        )
        self._settings_muted_labels.append(self.api_key_status_label)

        api_vault_note = ctk.CTkLabel(
            api_card,
            text=self.gui_texts.get(
                "api_key_vault_note",
                "Saving a key here only stores it for use by services; it does not "
                "select or activate a provider. Choose the active service profile "
                "in the control dashboard.",
            ),
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=self._colors["muted"],
            anchor="w",
            justify="left",
            wraplength=420,
        )
        api_vault_note.grid(
            row=3, column=0, columnspan=2, sticky="ew", padx=18, pady=(0, 14)
        )
        api_vault_note._text_key = "api_key_vault_note"  # type: ignore[attr-defined]
        self._settings_muted_labels.append(api_vault_note)

        self.change_key_btn = self._button(
            api_card, "change_key", self._on_settings_change_key, height=46
        )
        self.change_key_btn.configure(
            image=self._dashboard.icon_image("key", 16), compound="left"
        )
        self.change_key_btn.grid(
            row=4, column=0, sticky="ew", padx=(16, 6), pady=(0, 16)
        )
        self.remove_key_btn = self._button(
            api_card, "remove_key", self._on_settings_remove_key, height=46
        )
        self.remove_key_btn.configure(
            image=self._dashboard.icon_image("close", 16), compound="left"
        )
        self.remove_key_btn.grid(
            row=4, column=1, sticky="ew", padx=(6, 16), pady=(0, 16)
        )
        self._refresh_api_key_status()

        # Move any widgets the helpers added to the main lists into settings-specific
        # lists, so they don't get touched (and crash) after the window is closed.
        self._settings_labels += (
            self._labels[_lbl_start:] + self._section_titles[_sec_start:]
        )
        del self._labels[_lbl_start:]
        del self._section_titles[_sec_start:]
        self._settings_buttons += self._buttons[_btn_start:]
        del self._buttons[_btn_start:]
        self._settings_combos += self._combos[_cmb_start:]
        del self._combos[_cmb_start:]
        self._settings_checkboxes += self._checkboxes[_chk_start:]
        del self._checkboxes[_chk_start:]

        cancel_btn = ctk.CTkButton(
            self._settings_bottom_bar,
            text=self.gui_texts.get("dlg_cancel", "Cancel"),
            command=win.destroy,
            width=110,
            height=36,
            corner_radius=14,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        cancel_btn._text_key = "dlg_cancel"  # type: ignore[attr-defined]
        cancel_btn.pack(side="right", padx=18, pady=9)
        self._settings_buttons.append(cancel_btn)

        # Content is fully built and themed now — fade in without the flash.
        win.update_idletasks()
        try:
            win.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

    def _selected_api_key_provider(self) -> str | None:
        """Provider chosen in the settings API-key dropdown, or None while the
        "Please Select" placeholder is still showing."""
        combo = getattr(self, "api_key_provider_combo", None)
        if combo is None or not combo.winfo_exists():
            return None
        idx = combo.current()
        if idx is None or not (0 <= idx < len(self._api_key_provider_ids)):
            return None
        return self._api_key_provider_ids[idx]

    def _set_api_key_buttons_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for btn in (
            getattr(self, "change_key_btn", None),
            getattr(self, "remove_key_btn", None),
        ):
            if btn is not None and btn.winfo_exists():
                btn.configure(state=state)

    def _refresh_api_key_status(self) -> None:
        """Update the 'key saved / not set' hint and enable/disable the
        Change/Remove buttons for the selected provider."""
        label = getattr(self, "api_key_status_label", None)
        if label is None or not label.winfo_exists():
            return
        provider = self._selected_api_key_provider()
        if provider is None:
            label.configure(text="—")
            label._text_key = None  # type: ignore[attr-defined]
            self.change_key_btn.configure(
                text=self.gui_texts.get("change_key", "Change Key")
            )
            self.remove_key_btn.configure(
                text=self.gui_texts.get("remove_key", "Remove Key")
            )
            self._set_api_key_buttons_enabled(False)
            return
        saved = has_usable_key(provider)
        key = "api_key_status_saved" if saved else "api_key_status_none"
        label.configure(
            text=self.gui_texts.get(
                key, "Key saved" if saved else "Key missing"
            )
        )
        label._text_key = key  # type: ignore[attr-defined]
        service = provider_display_name(provider)
        self.change_key_btn.configure(
            text=f"{service} · {self.gui_texts.get('change_key', 'Change Key')}"
        )
        self.remove_key_btn.configure(
            text=f"{service} · {self.gui_texts.get('remove_key', 'Remove Key')}"
        )
        self._set_api_key_buttons_enabled(True)

    def _on_islamic_mode_change(self) -> None:
        enabled = self._islamic_mode_var.get()
        if not enabled:
            confirmed = self._confirm(
                self.gui_texts.get("islamic_mode", "Islamic mode"),
                self.gui_texts.get(
                    "islamic_mode_off_confirm",
                    "Turn off Islamic mode? Quran verse recognition, Athan "
                    "detection and the Islamic translation style will be "
                    "disabled — the app becomes a general translator.",
                ),
                parent=self._settings_win if self._settings_win_exists() else self,
            )
            if not confirmed:
                self._islamic_mode_var.set(True)
                return
        self._apply_islamic_mode(enabled)

    def _apply_islamic_mode(self, enabled: bool) -> None:
        self._saved_settings.islamic_mode = enabled
        log(f"Islamic mode: {'on' if enabled else 'off'}", level="INFO")
        self._save_current_settings()

    def _on_check_updates_change(self) -> None:
        enabled = self._check_updates_var.get()
        self._saved_settings.check_for_updates = enabled
        log(f"Update check on startup: {'on' if enabled else 'off'}", level="INFO")
        self._save_current_settings()

    def _on_settings_change_key(self) -> None:
        provider = self._selected_api_key_provider()
        if provider is None:
            return
        self.on_change_key(provider=provider)
        self._refresh_api_key_status()

    def _on_settings_remove_key(self) -> None:
        provider = self._selected_api_key_provider()
        if provider is None:
            return
        remove_api_key(
            is_running=self._running,
            root=self,
            colors=self._colors,
            texts=self.gui_texts,
            provider=provider,
        )
        self._refresh_api_key_status()


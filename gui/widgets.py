"""Shared UI building blocks for the control panel (mixin for AppGUI).

Moved verbatim out of gui/app_gui.py: the theme palette, the themed
message-box wrappers, the toplevel icon/titlebar helpers and the widget
factory (_section_card / _field / _label / _combo / ...). The methods run
on the AppGUI instance (``self``) and rely on attributes created in
``AppGUI.__init__``: ``_colors``, ``gui_texts``, ``_theme_mode`` and the
themed-widget registries (``_cards``, ``_labels``, ``_buttons``,
``_combos``, ...).
"""

import os
import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from config import ICON_PATH, ICON_PATH_PNG
from gui.control_dashboard import ICON_FONT, ICONS
from gui.dropdown import CustomDropdown
from utils.api_key_manager import apply_dark_titlebar, show_message
from utils.icons import ICO_SUPPORTED, scaled_icon_photo


class WidgetFactoryMixin:
    """UI-kit methods shared by the control panel and its child windows."""

    def _palette(self, theme_mode: str) -> dict[str, str]:
        if theme_mode == "light":
            return {
                "app_bg": "#e9eef1",
                "sidebar": "#f5f2ea",
                "card": "#ffffff",
                "card_border": "#b8c6c4",
                "card_highlight": "#ffffff",
                "card_lowlight": "#aab7b5",
                "panel": "#ffffff",
                "panel_soft": "#eef3f1",
                "recessed": "#e7eeec",
                "recessed_border": "#b7c5c2",
                "recessed_shadow": "#a9b7b4",
                "recessed_highlight": "#ffffff",
                "border": "#c9d4d2",
                "shadow": "#c5cfce",
                "text": "#10212b",
                "muted": "#607079",
                "log_bg": "#fbfdff",
                "log_text": "#162934",
                "accent": "#15945b",
                "accent_hover": "#107648",
                "accent_soft": "#dff3e8",
                "accent_glow": "#3bc984",
                "on_accent": "#ffffff",
                "danger": "#c74848",
                "danger_hover": "#a83a3a",
                "danger_soft": "#f7e5e2",
                "warning": "#b9792d",
                "warning_soft": "#f7eddd",
                "brass": "#9b6b32",
                "brass_soft": "#c9a978",
                "hero": "#e8eeeb",
                "hero_overlay": "#f8faf7",
                "surface_highlight": "#ffffff",
                "button": "#e1e8e6",
                "button_hover": "#d3dedb",
                "button_border": "#b9c8c5",
                "entry": "#f8faf8",
                "entry_border": "#bdcbc7",
                "section_session_surface": "#fbfdff",
                "section_session_border": "#b4cbd6",
                "section_session_accent": "#4d7588",
                "section_session_soft": "#e8f1f5",
                "section_output_surface": "#fafffd",
                "section_output_border": "#b2cec7",
                "section_output_accent": "#3d7772",
                "section_output_soft": "#e5f1ee",
                "section_services_surface": "#fffdf8",
                "section_services_border": "#d8c19e",
                "section_services_accent": "#9b6b32",
                "section_services_soft": "#f5ead9",
            }
        return {
            "app_bg": "#040910",
            "sidebar": "#06101a",
            "card": "#0a1622",
            "card_border": "#304756",
            "card_highlight": "#3b5260",
            "card_lowlight": "#02070d",
            "panel": "#091521",
            "panel_soft": "#102131",
            "recessed": "#07121c",
            "recessed_border": "#203745",
            "recessed_shadow": "#02070c",
            "recessed_highlight": "#263e4c",
            "border": "#263b48",
            "shadow": "#02060b",
            "text": "#f5f1e8",
            "muted": "#93a5af",
            "log_bg": "#050c14",
            "log_text": "#dce7e5",
            "accent": "#1fc878",
            "accent_hover": "#18aa66",
            "accent_soft": "#0d3025",
            "accent_glow": "#69edaf",
            "on_accent": "#032016",
            "danger": "#df5d5d",
            "danger_hover": "#c34949",
            "danger_soft": "#35191d",
            "warning": "#dfa75c",
            "warning_soft": "#352817",
            "brass": "#d6a45f",
            "brass_soft": "#705638",
            "hero": "#07131d",
            "hero_overlay": "#081621",
            "surface_highlight": "#314756",
            "button": "#132638",
            "button_hover": "#1a344a",
            "button_border": "#2d4758",
            "entry": "#07141f",
            "entry_border": "#304958",
            "section_session_surface": "#0a1826",
            "section_session_border": "#315064",
            "section_session_accent": "#76a9c0",
            "section_session_soft": "#122737",
            "section_output_surface": "#091b1d",
            "section_output_border": "#2c5a55",
            "section_output_accent": "#69aaa3",
            "section_output_soft": "#112b2c",
            "section_services_surface": "#141922",
            "section_services_border": "#604b33",
            "section_services_accent": "#d6a45f",
            "section_services_soft": "#282119",
        }

    def _section_role_colors(self, role: str) -> dict[str, str]:
        """Return the non-status identity colours for one dashboard card."""

        prefix = f"section_{role}_"
        return {
            "surface": self._colors.get(f"{prefix}surface", self._colors["card"]),
            "border": self._colors.get(
                f"{prefix}border", self._colors["card_border"]
            ),
            "accent": self._colors.get(f"{prefix}accent", self._colors["brass"]),
            "soft": self._colors.get(f"{prefix}soft", self._colors["panel_soft"]),
        }

    def _refresh_main_card_chrome(self) -> None:
        """Repaint persistent section depth after a runtime theme switch."""

        for style in getattr(self, "_section_card_styles", []):
            colors = self._section_role_colors(style["role"])
            style["card"].configure(
                fg_color=colors["surface"], border_color=colors["border"]
            )
            style["header"].configure(
                fg_color=colors["soft"], border_color=colors["border"]
            )
            style["symbol_shell"].configure(
                fg_color=colors["surface"], border_color=colors["border"]
            )
            style["symbol"].configure(
                text_color=colors["accent"],
                fg_color="transparent",
            )
            style["accent"].configure(fg_color=colors["accent"])
            style["highlight"].configure(fg_color=self._colors["card_highlight"])
            style["lowlight"].configure(fg_color=self._colors["card_lowlight"])

    def _refresh_recessed_panel_chrome(self) -> None:
        """Repaint the inset wells without touching the diagnostic drawer."""

        for style in getattr(self, "_recessed_panel_styles", []):
            style["panel"].configure(
                fg_color=self._colors["recessed"],
                border_color=self._colors["recessed_border"],
            )
            style["shadow"].configure(fg_color=self._colors["recessed_shadow"])
            style["highlight"].configure(
                fg_color=self._colors["recessed_highlight"]
            )

    def _set_toplevel_icon(self, win: ctk.CTkToplevel) -> None:
        """Set the window icon on a CTkToplevel, then re-assert the themed
        titlebar. On Windows ``iconbitmap()`` resets the DWM titlebar to the
        light default, which left a white title bar above our dark windows."""
        loaded = False
        if ICO_SUPPORTED and os.path.exists(ICON_PATH):
            try:
                win.iconbitmap(ICON_PATH)
                loaded = True
            except Exception:
                pass
        if not loaded and os.path.exists(ICON_PATH_PNG):
            try:
                win.iconphoto(False, scaled_icon_photo(ICON_PATH_PNG))
            except Exception:
                pass
        self._reassert_dark_titlebar(win)

    def _reassert_dark_titlebar(self, win: ctk.CTkToplevel) -> None:
        """Re-apply the themed DWM titlebar after ``iconbitmap`` reset it.

        Delegates to the shared ``apply_dark_titlebar`` (also used by the
        themed message dialogs) so the DWM/ctypes logic lives in one place.
        Deliberately NOT CTk's ``_windows_set_titlebar_color`` — that
        withdraws/deiconifies and in our ``after()``-icon + transient flow the
        async re-show never happened, leaving the window hidden.

        Passes the app's current theme explicitly — CTk's global appearance
        mode is only set at startup, so it is stale after a runtime switch."""
        apply_dark_titlebar(win, dark=self._theme_mode == "dark")

    def _alert(
        self,
        title: str,
        message: str,
        *,
        parent: tk.Misc | None = None,
        danger: bool = False,
    ) -> None:
        """Themed replacement for messagebox.showerror/showwarning (OK-only)."""
        show_message(
            parent or self,
            title,
            message,
            self._colors,
            icon="✕" if danger else "⚠",
            icon_color=self._colors["danger"] if danger else self._colors["warning"],
            ok_label=self.gui_texts.get("dlg_ok", "OK"),
        )

    def _confirm(
        self, title: str, message: str, *, parent: tk.Misc | None = None
    ) -> bool:
        """Themed replacement for messagebox.askyesno. Returns True on Yes."""
        return show_message(
            parent or self,
            title,
            message,
            self._colors,
            confirm=True,
            icon="⚠",
            icon_color=self._colors["danger"],
            yes_label=self.gui_texts.get("dlg_yes", "Yes"),
            no_label=self.gui_texts.get("dlg_no", "No"),
        )

    def _section_card(
        self,
        parent: ctk.CTkBaseClass,
        symbol: str,
        title_key: str,
        subtitle_key: str | None = None,
        toggle_command: Callable[[], Any] | None = None,
        role: str = "neutral",
    ) -> ctk.CTkFrame:
        role_colors = self._section_role_colors(role)
        card = ctk.CTkFrame(
            parent,
            fg_color=role_colors["surface"],
            border_color=role_colors["border"],
            border_width=2,
            corner_radius=22,
        )
        card._theme_role = role  # type: ignore[attr-defined]
        # Placement is handled by _layout_sidebar_cards() (single column when
        # the log panel is open, 2-column grid when it is collapsed).
        card.grid_columnconfigure(0, weight=1)
        self._cards.append(card)

        # A short role edge identifies the topic without borrowing green/red,
        # which remain reserved for factual ready/live/error states.  The
        # highlight and lowlight are static: they add depth without introducing
        # movement while the native window is dragged.
        accent_edge = ctk.CTkFrame(
            card,
            height=3,
            corner_radius=2,
            fg_color=role_colors["accent"],
        )
        accent_edge.place(relx=0.08, y=7, relwidth=0.18, anchor="nw")
        top_highlight = ctk.CTkFrame(
            card,
            height=1,
            corner_radius=1,
            fg_color=self._colors["card_highlight"],
        )
        top_highlight.place(relx=0.64, y=8, relwidth=0.58, anchor="n")
        bottom_lowlight = ctk.CTkFrame(
            card,
            height=2,
            corner_radius=1,
            fg_color=self._colors["card_lowlight"],
        )
        bottom_lowlight.place(
            relx=0.5, rely=1.0, y=-4, relwidth=0.84, anchor="s"
        )

        header = ctk.CTkFrame(
            card,
            fg_color=role_colors["soft"],
            border_color=role_colors["border"],
            border_width=1,
            corner_radius=16,
        )
        header.grid(row=0, column=0, columnspan=99, sticky="ew", padx=16, pady=(16, 12))
        header.grid_columnconfigure(1, weight=1)

        symbol_shell = ctk.CTkFrame(
            header,
            width=44,
            height=44,
            fg_color=role_colors["surface"],
            border_color=role_colors["border"],
            border_width=1,
            corner_radius=14,
        )
        symbol_shell.grid_propagate(False)
        symbol_label = ctk.CTkLabel(
            symbol_shell,
            text=symbol,
            font=ctk.CTkFont(family=ICON_FONT, size=20),
            text_color=role_colors["accent"],
            fg_color="transparent",
        )
        symbol_label.place(relx=0.5, rely=0.5, anchor="center")
        if subtitle_key:
            symbol_shell.grid(
                row=0,
                column=0,
                rowspan=2,
                sticky="nw",
                padx=(8, 12),
                pady=(5, 5),
            )
        else:
            symbol_shell.grid(
                row=0, column=0, sticky="w", padx=(8, 12), pady=(5, 5)
            )
        self._symbol_labels.append(symbol_label)

        title = ctk.CTkLabel(
            header,
            text=self.gui_texts.get(title_key, title_key),
            font=ctk.CTkFont(
                family="Segoe UI Variable Display", size=20, weight="bold"
            ),
            text_color=self._colors["text"],
            width=0,
            height=44,
            wraplength=340,
            justify="left",
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        title._text_key = title_key  # type: ignore[attr-defined]
        self._section_titles.append(title)

        if toggle_command is not None:
            header.grid_columnconfigure(2, weight=0)
            toggle_arrow = ctk.CTkLabel(
                header,
                text=ICONS["chevron_down"],
                font=ctk.CTkFont(family=ICON_FONT, size=18),
                text_color=self._colors["muted"],
                width=36,
                height=44,
                fg_color="transparent",
                cursor="hand2",
            )
            toggle_arrow.grid(row=0, column=2, sticky="e", padx=(4, 0))
            self._labels.append(toggle_arrow)
            self._advanced_toggle_arrow = toggle_arrow
            for w in (header, symbol_shell, symbol_label, title, toggle_arrow):
                w.bind("<Button-1>", lambda _e: toggle_command(), add="+")
                try:
                    w.configure(cursor="hand2")
                except Exception:
                    pass

        if subtitle_key:
            subtitle_text = self.gui_texts.get(subtitle_key, "")
            subtitle = ctk.CTkLabel(
                header,
                text=subtitle_text,
                font=ctk.CTkFont(family="Segoe UI", size=14),
                text_color=self._colors["muted"],
                width=0,
                height=24,
                wraplength=340,
                justify="left",
                anchor="w",
            )
            subtitle.grid(row=1, column=1, sticky="ew", pady=(2, 0))
            subtitle._text_key = subtitle_key  # type: ignore[attr-defined]
            self._muted_labels.append(subtitle)

        self._section_card_styles.append(
            {
                "role": role,
                "card": card,
                "header": header,
                "symbol_shell": symbol_shell,
                "symbol": symbol_label,
                "accent": accent_edge,
                "highlight": top_highlight,
                "lowlight": bottom_lowlight,
            }
        )

        return card

    def _field(
        self,
        parent: ctk.CTkBaseClass,
        label_key: str,
        symbol: str,
        row: int,
        column: int = 0,
        columnspan: int = 1,
        padx: int | tuple[int, int] = 18,
    ) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(
            row=row,
            column=column,
            columnspan=columnspan,
            sticky="ew",
            padx=padx,
            pady=(0, 18),
        )
        frame.grid_columnconfigure(0, weight=1)
        label = self._label(frame, label_key, symbol=symbol, size=14, weight="bold")
        label.pack(anchor="w")
        return frame

    def _mini_panel(
        self, parent: ctk.CTkBaseClass, *, corner_radius: int = 18
    ) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(
            parent,
            fg_color=self._colors["recessed"],
            border_color=self._colors["recessed_border"],
            border_width=2,
            corner_radius=corner_radius,
        )
        inner_shadow = ctk.CTkFrame(
            frame,
            height=2,
            corner_radius=1,
            fg_color=self._colors["recessed_shadow"],
        )
        inner_shadow.place(relx=0.5, y=5, relwidth=0.84, anchor="n")
        lower_highlight = ctk.CTkFrame(
            frame,
            height=1,
            corner_radius=1,
            fg_color=self._colors["recessed_highlight"],
        )
        lower_highlight.place(
            relx=0.5, rely=1.0, y=-4, relwidth=0.82, anchor="s"
        )
        self._main_panels.append(frame)
        self._recessed_panel_styles.append(
            {
                "panel": frame,
                "shadow": inner_shadow,
                "highlight": lower_highlight,
            }
        )
        return frame

    def _label(
        self,
        parent: ctk.CTkBaseClass,
        text_key: str,
        symbol: str | None = None,
        size: int = 14,
        weight: str = "normal",
        register: bool = True,
    ) -> ctk.CTkLabel:
        text = self.gui_texts.get(text_key, text_key)
        icon_name = next(
            (name for name, glyph in ICONS.items() if glyph == symbol), None
        )
        icon_image = (
            self._dashboard.icon_image(icon_name, max(14, size)) if icon_name else None
        )
        if symbol and icon_image is None:
            text = f"{symbol}  {text}"
        # The prefix symbols (▣ ◉ ⌁ ≋ ⇶ …) live in "Segoe UI Symbol", not the
        # plain "Segoe UI" text font — the latter renders them as ".notdef"
        # tofu boxes. Use the symbol font whenever a symbol is present (the same
        # family the header/card icons already use); plain labels stay "Segoe UI".
        family = "Segoe UI Variable Text"
        label = ctk.CTkLabel(
            parent,
            text=text,
            image=icon_image,
            compound="left",
            font=ctk.CTkFont(family=family, size=size, weight=weight),
            text_color=self._colors["text"],
            height=max(32, size + 16),
        )
        label._text_key = text_key  # type: ignore[attr-defined]
        label._symbol = symbol if icon_image is None else None  # type: ignore[attr-defined]
        # Short-lived windows (Batch) pass register=False so their labels aren't
        # held in the app-wide themed list — a destroyed window would otherwise
        # leave dead widgets there and crash the next theme/language re-apply.
        if register:
            self._labels.append(label)
        return label

    def _combo(
        self,
        parent: ctk.CTkBaseClass,
        values: list[str],
        command: Callable[[str], Any] | None = None,
        register: bool = True,
    ) -> CustomDropdown:
        combo = CustomDropdown(
            parent,
            values=values,
            command=command,
            height=46,
            corner_radius=16,
            border_width=2,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            dropdown_font=ctk.CTkFont(family="Segoe UI", size=14),
            fg_color=self._colors["entry"],
            border_color=self._colors["entry_border"],
            button_color=self._colors["entry"],
            button_hover_color=self._colors["panel_soft"],
            text_color=self._colors["text"],
            dropdown_fg_color=self._colors["panel"],
            dropdown_hover_color=self._colors["button_hover"],
            dropdown_text_color=self._colors["text"],
        )
        # Short-lived dialogs pass register=False so their combos aren't held in
        # the app-wide themed list after the dialog is destroyed.
        if register:
            self._combos.append(combo)
        return combo

    def _plain_button(
        self,
        parent: ctk.CTkBaseClass,
        text: str,
        command: Callable[[], Any],
        height: int = 48,
        width: int | None = None,
    ) -> ctk.CTkButton:
        icon_name = next((name for name, glyph in ICONS.items() if glyph == text), None)
        icon_image = self._dashboard.icon_image(icon_name, 18) if icon_name else None
        button = ctk.CTkButton(
            parent,
            text="" if icon_image is not None else text,
            image=icon_image,
            command=command,
            height=height,
            width=width or 0,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=18, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
            border_width=1,
            border_color=self._colors["button_border"],
        )
        button._uses_depth_border = True  # type: ignore[attr-defined]
        self._buttons.append(button)
        return button

    def _button(
        self,
        parent: ctk.CTkBaseClass,
        text_key: str,
        command: Callable[[], Any],
        symbol: str | None = None,
        height: int = 50,
    ) -> ctk.CTkButton:
        text = self.gui_texts.get(text_key, text_key)
        if symbol:
            text = f"{symbol}  {text}"
        button = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            height=height,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
            border_width=1,
            border_color=self._colors["button_border"],
        )
        button._uses_depth_border = True  # type: ignore[attr-defined]
        button._text_key = text_key  # type: ignore[attr-defined]
        button._symbol = symbol  # type: ignore[attr-defined]
        self._buttons.append(button)
        return button

    def _checkbox(
        self,
        parent: ctk.CTkBaseClass,
        text_key: str,
        variable: tk.BooleanVar,
        command: Callable[[], Any],
    ) -> ctk.CTkCheckBox:
        cb = ctk.CTkCheckBox(
            parent,
            text=self.gui_texts.get(text_key, text_key),
            variable=variable,
            command=command,
            height=34,
            font=ctk.CTkFont(family="Segoe UI", size=14),
            corner_radius=8,
            checkbox_width=24,
            checkbox_height=24,
            border_width=2,
            fg_color=self._colors["accent"],
            hover_color=self._colors["accent_hover"],
            border_color=self._colors["entry_border"],
            text_color=self._colors["text"],
        )
        cb._text_key = text_key  # type: ignore[attr-defined]
        self._checkboxes.append(cb)
        return cb

    def _setup_autohide_scrollbar(self, sf: ctk.CTkScrollableFrame) -> None:
        """Hide the scrollbar of a CTkScrollableFrame when all content fits."""

        def _check(*_: object) -> None:
            try:
                if sf.winfo_reqheight() <= sf._parent_canvas.winfo_height():
                    sf._scrollbar.grid_remove()
                else:
                    sf._scrollbar.grid()
            except Exception:
                pass

        try:
            sf.bind("<Configure>", _check, add="+")
            sf._parent_canvas.bind("<Configure>", _check, add="+")
            sf.after_idle(_check)
        except Exception:
            pass

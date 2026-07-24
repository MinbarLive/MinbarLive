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
from gui.dropdown import CustomDropdown
from utils.api_key_manager import apply_dark_titlebar, show_message
from utils.icons import ICO_SUPPORTED, scaled_icon_photo


class WidgetFactoryMixin:
    """UI-kit methods shared by the control panel and its child windows."""

    # Card header padding: above the header, and below it as the gap to the
    # card's body. A collapsible card that is closed has no body, so the
    # smaller bottom value would read as a lopsided card — see
    # AppGUI._set_advanced_visible.
    _CARD_HEADER_PADY = (18, 10)

    def _palette(self, theme_mode: str) -> dict[str, str]:
        if theme_mode == "light":
            return {
                "app_bg": "#edf2f7",
                "sidebar": "#f8fafc",
                "card": "#ffffff",
                "panel": "#ffffff",
                "panel_soft": "#f1f5f9",
                "border": "#d7dee8",
                "shadow": "#cbd5e1",
                "text": "#111827",
                "muted": "#64748b",
                "log_bg": "#fbfdff",
                "log_text": "#172033",
                "accent": "#15803d",
                "accent_hover": "#166534",
                "accent_soft": "#dcfce7",
                "danger": "#dc2626",
                "danger_hover": "#b91c1c",
                "danger_soft": "#fee2e2",
                "warning": "#d97706",
                "button": "#e2e8f0",
                "button_hover": "#cbd5e1",
                "entry": "#f8fafc",
                "entry_border": "#cbd5e1",
            }
        return {
            "app_bg": "#0b1020",
            "sidebar": "#0f172a",
            "card": "#111827",
            "panel": "#111827",
            "panel_soft": "#182235",
            "border": "#263449",
            "shadow": "#050817",
            "text": "#f8fafc",
            "muted": "#9ca3af",
            "log_bg": "#0a0f1d",
            "log_text": "#d8e3f0",
            "accent": "#16a34a",
            "accent_hover": "#15803d",
            "accent_soft": "#163821",
            "danger": "#dc2626",
            "danger_hover": "#b91c1c",
            "danger_soft": "#421719",
            "warning": "#f59e0b",
            "button": "#1f2a44",
            "button_hover": "#263654",
            "entry": "#0f172a",
            "entry_border": "#334155",
        }

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
        icon: str | None = None,
        icon_color: str | None = None,
        sections: list[tuple[str, str]] | None = None,
    ) -> None:
        """Themed replacement for messagebox.showerror/showwarning (OK-only).

        ``icon``/``icon_color`` default to a warning glyph; pass e.g. ``"🛈"``
        with the accent colour for a neutral info dialog (dropdown help).
        ``sections`` renders ``(heading, body)`` pairs with typographic
        hierarchy instead of one flat ``message``."""
        show_message(
            parent or self,
            title,
            message,
            self._colors,
            icon=icon or ("✕" if danger else "⚠"),
            icon_color=icon_color
            or (self._colors["danger"] if danger else self._colors["warning"]),
            ok_label=self.gui_texts.get("dlg_ok", "OK"),
            sections=sections,
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
    ) -> ctk.CTkFrame:
        card = ctk.CTkFrame(
            parent,
            fg_color=self._colors["card"],
            border_color=self._colors["border"],
            border_width=2,
            corner_radius=24,
        )
        # Placement is handled by _layout_sidebar_cards() (the card grid reflows
        # into 1, 2 or 3 columns; single column while the log panel is open).
        card.grid_columnconfigure(0, weight=1)
        self._cards.append(card)

        header = ctk.CTkFrame(card, fg_color="transparent")
        # A collapsible card is built closed, and closed it has no body for the
        # header's smaller bottom pad to sit against — that reads as a lopsided
        # card, so it gets its top pad on both sides. AppGUI's
        # _set_advanced_visible flips it back when the body opens.
        top_pad, body_gap = self._CARD_HEADER_PADY
        header.grid(
            row=0,
            column=0,
            columnspan=99,
            sticky="ew",
            padx=20,
            pady=(top_pad, top_pad if toggle_command is not None else body_gap),
        )
        header.grid_columnconfigure(1, weight=1)

        symbol_label = ctk.CTkLabel(
            header,
            text=symbol,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=20, weight="bold"),
            text_color=self._colors["accent"],
            width=44,
            height=44,
            fg_color=self._colors["panel_soft"],
            corner_radius=16,
        )
        if subtitle_key:
            symbol_label.grid(
                row=0, column=0, rowspan=2, sticky="nw", padx=(0, 12), pady=(1, 0)
            )
        else:
            symbol_label.grid(row=0, column=0, sticky="w", padx=(0, 12))
        self._symbol_labels.append(symbol_label)

        title = ctk.CTkLabel(
            header,
            text=self.gui_texts.get(title_key, title_key),
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=self._colors["text"],
            width=0,
            height=44,
            wraplength=340,
            justify="left",
            anchor="w",
        )
        title.grid(row=0, column=1, sticky="ew")
        title._text_key = title_key  # type: ignore[attr-defined]
        self._section_titles.append(title)

        if toggle_command is not None:
            header.grid_columnconfigure(2, weight=0)
            toggle_arrow = ctk.CTkLabel(
                header,
                text="▾",
                font=ctk.CTkFont(family="Segoe UI Symbol", size=20),
                text_color=self._colors["muted"],
                width=36,
                height=44,
                fg_color="transparent",
                cursor="hand2",
            )
            toggle_arrow.grid(row=0, column=2, sticky="e", padx=(4, 0))
            self._labels.append(toggle_arrow)
            self._advanced_toggle_arrow = toggle_arrow
            self._advanced_header = header
            for w in (header, symbol_label, title, toggle_arrow):
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

    def _mini_panel(self, parent: ctk.CTkBaseClass) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(
            parent,
            fg_color=self._colors["panel_soft"],
            border_color=self._colors["border"],
            border_width=1,
            corner_radius=18,
        )
        self._main_panels.append(frame)
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
        if symbol:
            text = f"{symbol}  {text}"
        # The prefix symbols (▣ ◉ ⌁ ≋ ⇶ …) live in "Segoe UI Symbol", not the
        # plain "Segoe UI" text font — the latter renders them as ".notdef"
        # tofu boxes. Use the symbol font whenever a symbol is present (the same
        # family the header/card icons already use); plain labels stay "Segoe UI".
        family = "Segoe UI Symbol" if symbol else "Segoe UI"
        label = ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(family=family, size=size, weight=weight),
            text_color=self._colors["text"],
            height=max(32, size + 16),
        )
        label._text_key = text_key  # type: ignore[attr-defined]
        label._symbol = symbol  # type: ignore[attr-defined]
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
            border_width=1,
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
        button = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            height=height,
            width=width or 0,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI Symbol", size=18, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
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
        )
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


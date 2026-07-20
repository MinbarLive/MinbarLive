"""Subtitle typography controls (mixin for AppGUI).

A collapsible "subtitle appearance" section in the control panel's Display &
Audio card, directly under the font-size and height controls it belongs with:
an independent size for the original/live text plus optional colour overrides
for the translation and original lines. Collapsed by default — these are
set-once values, not something an operator touches mid-session.

Uses AppGUI services: ``_colors``, ``gui_texts``, ``_saved_settings``,
``_label`` / ``_plain_button`` / ``_mini_panel`` (WidgetFactoryMixin),
``_save_current_settings`` and ``subtitle_window``.
"""

import tkinter as tk
from tkinter import colorchooser

import customtkinter as ctk

from utils.logging import log
from utils.settings import (
    DEFAULT_SOURCE_FONT_SIZE_BASE,
    SOURCE_FONT_SIZE_BASE_MAX,
    SOURCE_FONT_SIZE_BASE_MIN,
)

# Step applied to the original-text divisor per −/+ click, matching the
# subtitle window's own font steps.
_SOURCE_FONT_STEP = 5.0

# (settings field, label text key) for the two colour rows.
_COLOR_ROWS = (
    ("translation_text_color", "translation_text_color"),
    ("source_text_color", "source_text_color"),
)


def _readable_on(hex_color: str) -> str:
    """Black or white label text, whichever stays legible on ``hex_color``."""
    try:
        r = int(hex_color[1:3], 16)
        g = int(hex_color[3:5], 16)
        b = int(hex_color[5:7], 16)
    except (IndexError, ValueError):
        return "#000000"
    # Rec. 601 luma — good enough to pick a contrasting label colour.
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


class SubtitleTypographyMixin:
    """Collapsible subtitle-appearance controls, hosted by AppGUI."""

    def _build_typography_section(self, card: ctk.CTkBaseClass, row: int) -> None:
        """Build the collapsed expander into ``card`` at grid ``row``/``row+1``.

        Both widgets get explicit rows: the frame starts collapsed via
        ``grid_remove()``, and any later sibling gridded without an explicit
        ``row=`` would be handed that freed row and then overlap the expander
        once it is opened.
        """
        self.typography_toggle_btn = ctk.CTkButton(
            card,
            text=self._typography_toggle_text(),
            command=self._on_toggle_typography,
            height=36,
            corner_radius=14,
            anchor="w",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        self.typography_toggle_btn.grid(
            row=row, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 8)
        )

        panel = self._mini_panel(card)
        panel.grid(
            row=row + 1, column=0, columnspan=2, sticky="ew", padx=16, pady=(0, 12)
        )
        panel.grid_columnconfigure(1, weight=1)
        self._typography_frame = panel
        self._build_typography_controls(panel)
        if not getattr(self, "_typography_open", False):
            panel.grid_remove()

    def _typography_toggle_text(self) -> str:
        arrow = "▾" if getattr(self, "_typography_open", False) else "▸"
        label = self.gui_texts.get("subtitle_appearance", "Subtitle appearance")
        return f"{arrow}  {label}"

    def _on_toggle_typography(self) -> None:
        self._typography_open = not getattr(self, "_typography_open", False)
        if self._typography_open:
            self._typography_frame.grid()
        else:
            self._typography_frame.grid_remove()
        self.typography_toggle_btn.configure(text=self._typography_toggle_text())

    def _build_typography_controls(self, parent: ctk.CTkFrame) -> None:
        """Size stepper plus a pick/reset button pair per colour."""
        size_label = self._label(parent, "source_text_size", size=13)
        size_label.grid(row=0, column=0, sticky="w", padx=(12, 8), pady=(12, 6))

        size_box = ctk.CTkFrame(parent, fg_color="transparent")
        size_box.grid(row=0, column=1, sticky="e", padx=(0, 12), pady=(12, 6))

        self.source_font_minus_btn = self._plain_button(
            size_box, "−", lambda: self._on_source_font_step(+_SOURCE_FONT_STEP), height=32
        )
        self.source_font_minus_btn.configure(width=36)
        self.source_font_minus_btn.grid(row=0, column=0, padx=(0, 6))

        self.source_font_value_label = ctk.CTkLabel(
            size_box,
            text=self._source_font_percent_text(),
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=self._colors["text"],
            width=52,
        )
        self.source_font_value_label.grid(row=0, column=1)

        self.source_font_plus_btn = self._plain_button(
            size_box, "+", lambda: self._on_source_font_step(-_SOURCE_FONT_STEP), height=32
        )
        self.source_font_plus_btn.configure(width=36)
        self.source_font_plus_btn.grid(row=0, column=2, padx=(6, 0))

        self._color_pick_btns: dict[str, ctk.CTkButton] = {}
        self._color_reset_btns: dict[str, ctk.CTkButton] = {}
        for idx, (field, text_key) in enumerate(_COLOR_ROWS, start=1):
            self._build_color_row(parent, idx, field, text_key)

        # The pick buttons are created without colours (they carry the operator's
        # own fill, so they stay out of the themed-button registry) — this is
        # what paints their initial state.
        self._refresh_typography_controls()

    def _build_color_row(
        self, parent: ctk.CTkFrame, row: int, field: str, text_key: str
    ) -> None:
        label = self._label(parent, text_key, size=13)
        label.grid(row=row, column=0, sticky="w", padx=(12, 8), pady=(0, 10))

        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=row, column=1, sticky="e", padx=(0, 12), pady=(0, 10))

        # Deliberately NOT registered in self._buttons: the pick button's fill is
        # the operator's chosen colour, which the theme loop would overwrite.
        # _refresh_typography_controls owns its styling in both states.
        pick = ctk.CTkButton(
            box,
            text="",
            command=lambda f=field: self._on_pick_subtitle_color(f),
            width=104,
            height=32,
            corner_radius=10,
            border_width=2,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
        )
        pick.grid(row=0, column=0, padx=(0, 6))
        self._color_pick_btns[field] = pick

        reset = ctk.CTkButton(
            box,
            text=self.gui_texts.get("color_default", "Default"),
            command=lambda f=field: self._on_reset_subtitle_color(f),
            width=88,
            height=32,
            corner_radius=10,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        reset._text_key = "color_default"  # type: ignore[attr-defined]
        reset.grid(row=0, column=1)
        self._buttons.append(reset)
        self._color_reset_btns[field] = reset

    def _default_source_font_size_base(self) -> float:
        """The 70%-of-translation divisor for the CURRENT translation size.

        Not the module constant — that one assumes the default translation size,
        so a window with a custom font size would not return to the proportion
        the user actually started from.
        """
        try:
            return float(self._saved_settings.font_size_base) / 0.7
        except (TypeError, ValueError, ZeroDivisionError):
            return DEFAULT_SOURCE_FONT_SIZE_BASE

    def _source_font_percent_text(self) -> str:
        """Original size as a percentage OF THE TRANSLATION size.

        Both values are divisors, so the ratio is translation/source. Showing it
        against the translation stays truthful when the subtitle font is resized;
        a percentage against a fixed constant would drift (and read e.g. "114%"
        for what is really still the default 70% proportion).
        """
        base = getattr(
            self._saved_settings, "source_font_size_base", DEFAULT_SOURCE_FONT_SIZE_BASE
        )
        try:
            translation_base = float(self._saved_settings.font_size_base)
            percent = round(translation_base / float(base) * 100)
        except (TypeError, ValueError, ZeroDivisionError):
            percent = 70
        return f"{percent}%"

    def _refresh_typography_controls(self) -> None:
        """Re-style/re-text the appearance controls (theme + language changes)."""
        btn = getattr(self, "typography_toggle_btn", None)
        if btn is None or not btn.winfo_exists():
            return
        btn.configure(
            text=self._typography_toggle_text(),
            fg_color=self._colors["button"],
            hover_color=self._colors["button_hover"],
            text_color=self._colors["text"],
        )
        value_label = getattr(self, "source_font_value_label", None)
        if value_label is not None and value_label.winfo_exists():
            value_label.configure(
                text=self._source_font_percent_text(), text_color=self._colors["text"]
            )
        for field, pick in getattr(self, "_color_pick_btns", {}).items():
            if not pick.winfo_exists():
                continue
            color = getattr(self._saved_settings, field, "") or ""
            if color:
                pick.configure(
                    text=color.upper(),
                    fg_color=color,
                    hover_color=color,
                    border_color=self._colors["border"],
                    text_color=_readable_on(color),
                )
            else:
                pick.configure(
                    text=self.gui_texts.get("color_choose", "Choose…"),
                    fg_color=self._colors["button"],
                    hover_color=self._colors["button_hover"],
                    border_color=self._colors["border"],
                    text_color=self._colors["text"],
                )
            reset = self._color_reset_btns.get(field)
            if reset is not None and reset.winfo_exists():
                # Nothing to reset while the theme colour is already in use —
                # the disabled state is what tells the operator which is active.
                reset.configure(state="normal" if color else "disabled")

    def _apply_typography_to_window(self) -> None:
        """Push the stored appearance values onto a live subtitle window."""
        win = getattr(self, "subtitle_window", None)
        if win is None or not win.winfo_exists():
            return
        win.set_source_font_size_base(self._saved_settings.source_font_size_base)
        win.set_translation_text_color(self._saved_settings.translation_text_color)
        win.set_source_text_color(self._saved_settings.source_text_color)

    def _scale_source_font_with_translation(
        self, old_base: float, new_base: float
    ) -> None:
        """Scale the original-text divisor when the translation size changes.

        The main "Schriftgröße −/+" moves the translation divisor; the original
        text should follow so the pair keeps the proportion set in this expander
        (the % shown here). Both are divisors, so preserving the ratio means
        multiplying the source divisor by the same factor the translation one
        moved by. The expander still sets that ratio independently.
        """
        try:
            old_base = float(old_base)
            new_base = float(new_base)
        except (TypeError, ValueError):
            return
        if not old_base or old_base == new_base:
            return
        source_base = getattr(
            self._saved_settings, "source_font_size_base", DEFAULT_SOURCE_FONT_SIZE_BASE
        )
        scaled = max(
            SOURCE_FONT_SIZE_BASE_MIN,
            min(SOURCE_FONT_SIZE_BASE_MAX, float(source_base) * (new_base / old_base)),
        )
        if scaled == source_base:
            return
        self._saved_settings.source_font_size_base = scaled
        win = getattr(self, "subtitle_window", None)
        if win is not None and win.winfo_exists():
            win.set_source_font_size_base(scaled)
        self._refresh_typography_controls()

    def _on_source_font_step(self, delta: float) -> None:
        """Step the divisor. Positive delta = larger divisor = smaller text."""
        current = getattr(
            self._saved_settings, "source_font_size_base", DEFAULT_SOURCE_FONT_SIZE_BASE
        )
        new_base = max(
            SOURCE_FONT_SIZE_BASE_MIN, min(SOURCE_FONT_SIZE_BASE_MAX, current + delta)
        )
        if new_base == current:
            return
        self._saved_settings.source_font_size_base = new_base
        self._apply_typography_to_window()
        self._refresh_typography_controls()
        self._save_current_settings()

    def _on_pick_subtitle_color(self, field: str) -> None:
        current = getattr(self._saved_settings, field, "") or None
        try:
            _rgb, chosen = colorchooser.askcolor(
                color=current,
                parent=self,
                title=self.gui_texts.get("subtitle_appearance", "Subtitle appearance"),
            )
        except tk.TclError:
            return
        if not chosen:
            return
        setattr(self._saved_settings, field, str(chosen).upper())
        self._apply_typography_to_window()
        self._refresh_typography_controls()
        self._save_current_settings()
        log(f"Subtitle {field} set to {chosen}", level="INFO")

    def _on_reset_subtitle_color(self, field: str) -> None:
        if not getattr(self._saved_settings, field, ""):
            return
        setattr(self._saved_settings, field, "")
        self._apply_typography_to_window()
        self._refresh_typography_controls()
        self._save_current_settings()
        log(f"Subtitle {field} reset to theme default", level="INFO")

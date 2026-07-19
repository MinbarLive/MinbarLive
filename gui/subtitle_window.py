"""Dedicated subtitle window (full-screen) for displaying translations."""

from __future__ import annotations

import os
import re
import sys
import tkinter as tk
from dataclasses import dataclass

from screeninfo import get_monitors

from config import (
    FOOTER_TRANSLATIONS_PATH,
    ICON_PATH,
    ICON_PATH_PNG,
    LINE_SPACING,
    MARGIN_BOTTOM,
    REALTIME_BLOCK_SPACING,
    REALTIME_LIVE_MAX_ROWS,
    REALTIME_MAX_BLOCK_CHARS,
)
from utils.icons import ICO_SUPPORTED, scaled_icon_photo
from utils.json_helpers import load_json
from utils.settings import (
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
)

_WHITESPACE_RE = re.compile(r"\s+")

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_get_display

    _ARABIC_SUPPORT = True
except ImportError:
    _ARABIC_SUPPORT = False

# Arabic-block codepoints that survive reshaping (؟ ، ؛ ٪ and Arabic-Indic
# digits have no presentation forms; reshaped letters become U+FBxx/FExx).
_ARABIC_BLOCK_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿ]")
# Windows Tk (GDI/Uniscribe) runs its own Arabic bidi+shaping pass over any
# string still containing Arabic-block codepoints. Pure presentation-form
# strings don't trigger it — that's why the reshape+bidi pipeline normally
# renders correctly here.
_TK_HANDLES_ARABIC = sys.platform == "win32"


def _reshape_rtl(text: str) -> str:
    """Reshape and apply bidi algorithm to Arabic/RTL text for correct rendering.

    Tkinter does not natively handle Arabic text shaping or RTL direction
    on all platforms, so we pre-process the text with arabic-reshaper and
    python-bidi before passing it to the canvas. Non-Arabic text is
    returned unchanged.
    """
    if not _ARABIC_SUPPORT:
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        visual = bidi_get_display(reshaped)
    except Exception as exc:
        # A silent fallback renders Arabic reversed with disconnected
        # letters — make the cause visible so it can be diagnosed.
        try:
            from utils.logging import log

            log(f"RTL reshaping failed, rendering raw text: {exc}", level="WARNING")
        except Exception:
            pass
        return text
    if _TK_HANDLES_ARABIC and _ARABIC_BLOCK_RE.search(visual):
        # ؟ ، ؛ or Arabic digits survived reshaping: their presence makes
        # Windows Tk re-run bidi over our already-visual string, reversing
        # it back to logical order with disconnected letters (the "breaks
        # only with ؟/!" bug). Tk renders the plain logical text correctly
        # on its own in exactly these cases — hand it the original text.
        return text
    return visual


if _ARABIC_SUPPORT:
    # Warm up the reshaper's lazy config load at import time, so the first
    # rendered Arabic line pays no first-call cost (and any init failure
    # surfaces here, once, instead of on live subtitles).
    _reshape_rtl("تهيئة")


# Sentence boundaries for splitting oversized Realtime blocks: terminal
# punctuation (Latin + Arabic), optionally followed by closing quotes.
_SENTENCE_RE = re.compile(r"[^.!?…؟؛]*[.!?…؟؛]+[\"'“”„»«]*\s*|[^.!?…؟؛]+$")

# Any Arabic-script character, incl. the presentation forms the reshaper
# emits — checked on rendered text to pick the source/live-line font.
_ARABIC_ANY_RE = re.compile(
    r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]"
)

# Ink classification for _stack_overlap. Deliberately biased LOOSE: the
# classes reserve more headroom than the ink probe measured, so a wrong
# guess can only widen the gap, never let letters touch.
# Capital letters with diacritics (Ä Ü Š İ Ç …) reach well above plain caps.
_TALL_DIACRITIC_RE = re.compile(r"[À-ÖØ-ÞĀ-ɏ̀-ͯ]")
# The Allah honorifics the translator inserts into TARGET-language lines
# (Allah ﷻ, Muhammad ﷺ) are Arabic presentation-form ligatures but render
# within plain Latin ink bounds (probed 33/30px vs plain text 35px below the
# box top at 64pt) — they must not push a German line into the loose Arabic
# headroom class.
_HONORIFIC_LIGATURE_RE = re.compile(r"[ﷺﷻ]")


def split_display_chunks(text: str, max_chars: int) -> list[str]:
    """Split a long settled translation at sentence boundaries into chunks
    of at most ``max_chars`` (a single over-long sentence stays whole).
    Continuous speech flushes up to 12s of speech as one utterance — its
    translation would otherwise render as a wall of text in the feed."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    sentences = [s for s in sentences if s]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


# Load footer translations from JSON file
FOOTER_TRANSLATIONS = load_json(FOOTER_TRANSLATIONS_PATH)

# Default footer for languages not in the list
DEFAULT_FOOTER = (
    "AI Translation! Our association assumes no liability for accuracy or completeness."
)

# Continuous scroll settings
SCROLL_INTERVAL_MS = 30  # Milliseconds between scroll updates (~33 fps)

# Live (in-progress) transcript line: cap the displayed text so a long
# utterance shows its tail instead of filling the screen. Truncation happens
# on the logical (pre-RTL-shaping) text so bidi ordering stays correct.
LIVE_TEXT_MAX_CHARS = 160

# Top padding of the live-feed mode's top-down layout
LIVE_FEED_TOP_MARGIN = 24

# Realtime feed: ease the top-down scroll toward its target instead of
# snapping, so an added subtitle glides the feed up (much easier to read than
# an instant jump). Tuned for a ~300 ms ease-out at ~33 fps.
LIVE_FEED_ANIM_FRAME_MS = SCROLL_INTERVAL_MS
LIVE_FEED_ANIM_EASE = 0.3  # fraction of the remaining gap closed per frame
LIVE_FEED_ANIM_MIN_STEP = 2.0  # px/frame floor so the tail doesn't crawl
LIVE_FEED_ANIM_SNAP_PX = 1.0  # within this, snap to target and stop

# Static mode also supports very shallow output windows (down to 5% of the
# monitor height). Below this surface height its normal card padding would use
# more room than the text itself, so spacing contracts proportionally while
# the ordinary layout remains byte-for-byte unchanged at common heights.
STATIC_COMPACT_LAYOUT_HEIGHT = 160
STATIC_MIN_FIT_FONT_SIZE = 6
STATIC_CARD_OUTLINE_ALLOWANCE = 2
# A visible disclaimer pill plus one bilingual subtitle pair cannot remain
# legible in a 54 px-tall 1080p surface. Keep the requested percentage in the
# settings, but protect the actual audience window with a small physical floor
# whenever the footer is present. Footer-free overlays still honour true 5%.
MIN_FOOTER_SURFACE_HEIGHT = 96


# V3 audience-surface tokens.  The subtitle window deliberately uses a much
# quieter version of the control deck's palette: the opaque canvas is deep
# navy, translated text is warm ivory, supporting copy is cool and muted,
# brass is reserved for outlines/disclaimers, and emerald only communicates
# the real stopped/live state.  Keeping these as data also makes theme changes
# deterministic and easy to regression-test without creating a Tk window.
_SUBTITLE_THEME_PALETTES = {
    "dark": {
        "bg_color": "#020A13",
        "primary_text": "#F7F3EA",
        "secondary_text": "#A9B8C3",
        "card_fill": "#071521",
        "card_outline": "#29414D",
        "accent_outline": "#9A7441",
        "footer_bg": "#0B1823",
        "footer_fg": "#D8B474",
        "footer_outline": "#765A35",
        "stopped_bg": "#082820",
        "stopped_fg": "#7DE2B5",
        "stopped_outline": "#2A9B72",
    },
    "light": {
        "bg_color": "#F3F0E8",
        "primary_text": "#0A1823",
        "secondary_text": "#586A73",
        "card_fill": "#FFFDF8",
        "card_outline": "#C8B997",
        "accent_outline": "#9A6C32",
        "footer_bg": "#FFF8EC",
        "footer_fg": "#75501F",
        "footer_outline": "#C3A06A",
        "stopped_bg": "#E1F2EA",
        "stopped_fg": "#0D6046",
        "stopped_outline": "#4A9E7A",
    },
}


def _prefers_reduced_motion() -> bool:
    """Return the operating-system animation preference when available.

    The environment override is intentionally undocumented UI plumbing: it
    keeps render harnesses/tests deterministic without adding another public
    setting.  On Windows the value follows "Show animations in Windows".
    Unsupported platforms retain the existing gentle feed movement.
    """
    override = os.environ.get("MINBARLIVE_REDUCED_MOTION", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        SPI_GETCLIENTAREAANIMATION = 0x1042
        enabled = wintypes.BOOL()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETCLIENTAREAANIMATION, 0, ctypes.byref(enabled), 0
        )
        return bool(ok) and not bool(enabled.value)
    except Exception:
        return False


@dataclass
class _SubtitleBlock:
    """Canvas items of one rendered subtitle (translation + optional source).

    ``line_items``/``source_items`` are lists of (text_id, box_id) canvas-item
    pairs; box_id is None when the line has no background card (non-static
    modes). ``text_id`` is the anchor item positioning works against — in
    static mode that is the last (bottom) line's text item.
    """

    text_id: int
    height: int  # total pixel height of the block incl. source lines
    line_items: list[tuple[int, int | None]] | None = None  # static per-line cards
    source_items: list[tuple[int, int | None]] | None = None  # bilingual source
    # Keep the logical, unshaped strings. Canvas text is already wrapped and
    # RTL-shaped, so reconstructing from itemcget() during a live font change
    # would either preserve stale line breaks or shape Arabic a second time.
    logical_text: str = ""
    logical_source: str | None = None


class SubtitleWindow(tk.Toplevel):
    """Full-screen window that renders subtitles in various modes."""

    def __init__(
        self,
        master: tk.Tk,
        on_close,
        monitor_index: int = 1,
        font_size_base: int = 40,
        target_language: str = "German",
        subtitle_mode: str = SUBTITLE_MODE_STATIC,
        scroll_speed: float = 1.0,
        transparent_static: bool = False,
        window_height_percent: int = 100,
        show_footer: bool = True,
        adaptive_catchup: bool = False,
        theme_mode: str = "dark",
        bilingual_mode: bool = False,
        always_on_top: bool = True,
        on_stop=None,
        source_font_size_base: float = 40 / 0.7,
        translation_text_color: str = "",
        source_text_color: str = "",
    ):
        super().__init__(master)
        # Build fully transparent: a fresh Toplevel paints white with a
        # caption until the dark background + borderless styling below land
        # — the "white window pops up" flash. Revealed at the end of
        # __init__. Same -alpha 0→1 pattern as the settings/batch windows
        # (withdraw() is the vanish trap — see gui/settings_view.py).
        try:
            self.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        is_windows = sys.platform == "win32"
        self._on_close = on_close
        self._on_stop = on_stop
        self._monitor_index = monitor_index
        self._target_language = target_language
        self._subtitle_mode = subtitle_mode  # static or continuous
        self._scroll_animation_id = None  # For cancelling continuous scroll animation
        self._scroll_speed = scroll_speed  # Current scroll speed (pixels per frame)
        self._transparent_static = (
            transparent_static  # Transparent background for static mode
        )
        self._window_height_percent = max(5, min(100, window_height_percent))
        self._show_footer = show_footer  # Show/hide footer disclaimer
        self._adaptive_catchup = adaptive_catchup
        self._bilingual_mode = bilingual_mode  # Show original text above translation
        # Keep the overlay above other windows (user setting). Off = the
        # overlay stays in normal stacking even as a partial/transparent
        # overlay. Applied via _apply_topmost / set_always_on_top.
        self._always_on_top = always_on_top
        self._effective_scroll_speed = scroll_speed
        self._theme_mode = theme_mode
        self._theme_palettes = _SUBTITLE_THEME_PALETTES
        # Empty overrides deliberately mean "follow the active theme". Keep
        # those raw values separate from the effective colours so switching
        # light/dark mode can update defaults without discarding a custom
        # operator choice.
        self._translation_text_color_override = (
            translation_text_color or ""
        ).strip()
        self._source_text_color_override = (source_text_color or "").strip()
        self._box_padding_x = 22
        self._box_padding_y = 8
        self._box_radius = 12
        self._line_gap = 10
        # Bounding-box gap between a source line and its translation in
        # static mode, where each line has its own background card (cards
        # must not overlap). The text-only modes use _stack_overlap instead
        # so source/translation and wrapped rows sit visibly tighter.
        self._pair_gap = 5
        self._transparent_key_color = "#00fe00"
        self._font_family = "Segoe UI" if is_windows else "Helvetica"
        # Regular-weight family for the italic source/live styling; translated
        # subtitles use the same broad-script family with an explicit bold
        # weight so Arabic and Latin fallbacks remain predictable.
        self._slant_font_family = "Segoe UI" if is_windows else "Helvetica"
        self._footer_font_family = "Segoe UI" if is_windows else "Helvetica"
        self._destroying = False
        self._is_hidden = False
        self._reduced_motion = _prefers_reduced_motion()
        self._delayed_font_job: str | None = None
        self._continuous_start_job: str | None = None
        self._apply_theme_palette(self._theme_mode)

        self.configure(bg=self._bg_color)

        # Configure window to be borderless but still visible to OBS/screen capture
        # We avoid overrideredirect(True) because it makes the window invisible to
        # OBS window capture on most platforms.
        self._setup_borderless_window()

        # Esc stops the translation (like the Stop button) but never closes
        # the overlay or the app — a mis-hit shouldn't take the display down
        # mid-session. The window's close protocol still routes to on_close.
        self.bind("<Escape>", self._on_escape)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Position on correct monitor BEFORE showing
        self._set_screen_position()

        self.canvas = tk.Canvas(self, bg=self._bg_color, highlightthickness=0)
        self.canvas.place(relx=0, rely=0.0, relwidth=1, relheight=1.0)

        # Font size base (divisor for calculating font size)
        self._font_size_base = font_size_base
        try:
            source_base = float(source_font_size_base)
        except (TypeError, ValueError):
            source_base = 40 / 0.7
        self._source_font_size_base = max(20.0, min(120.0, source_base))

        footer_text = FOOTER_TRANSLATIONS.get(target_language, DEFAULT_FOOTER)
        self.footer = tk.Label(
            self,
            text=footer_text,
            font=(self._footer_font_family, 14, "bold"),
            fg=self._footer_fg,
            bg=self._footer_bg,
            bd=0,
            padx=18,
            pady=10,
            highlightthickness=1,
            highlightbackground=self._footer_outline,
        )

        self.subtitle_stack: list[_SubtitleBlock] = []
        self.line_spacing = LINE_SPACING
        self.margin_bottom = MARGIN_BOTTOM
        self._canvas_footer_items: list[int] = []  # canvas item IDs for pill footer
        self._stopped_hint = False  # "translation stopped" pill while idle
        self._stopped_hint_items: list[int] = []
        # Live (in-progress) transcript line — Realtime mode only, never part
        # of the stack. "Settled" = utterance finished, translation in flight;
        # it remains a source-role line until the translation replaces it.
        self._live_text = ""
        self._live_settled = False
        self._live_items: list[tuple[int, int | None]] = []
        # Realtime mode: accumulated upward scroll of the top-down layout
        # (chat-style — content only ever moves up, never back down).
        # _live_feed_scroll is the *rendered* offset; it eases toward
        # _live_feed_scroll_target so shifts glide instead of snapping.
        self._live_feed_scroll = 0.0
        self._live_feed_scroll_target = 0.0
        self._feed_anim_job: str | None = None
        # Announcement overlay (megaphone): a big centred operator message.
        # Drawn above the subtitles and the live line but below the disclaimer
        # pills (kept visible). The text/timer are owned by AppGUI so an
        # announcement survives a translation stop and window recreation — this
        # window only renders whatever text it was last given.
        self._announcement_text = ""
        self._announcement_items: list[int] = []

        self.update_idletasks()
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()
        self._update_font()
        self._update_footer_visibility()

        # Fully built and styled — reveal. Restored BEFORE the transparent
        # mode below: that path manages its own alpha (macOS) and must win.
        try:
            self.attributes("-alpha", 1.0)
        except tk.TclError:
            pass

        if self._transparent_static and self._subtitle_mode == SUBTITLE_MODE_STATIC:
            self._apply_transparent_mode()

        self._delayed_font_job = self.after(100, self._delayed_font_update)

        if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            self._continuous_start_job = self.after(
                150, self._start_continuous_scroll
            )

    def _setup_borderless_window(self):
        """Configure a borderless window that remains visible to OBS/screen capture.

        Unlike overrideredirect(True), this approach keeps the window in the
        window manager's control, making it visible to OBS Window Capture.

        Platform-specific approaches:
        - Windows: Remove decorations via wm_attributes, use -toolwindow to hide from taskbar
        - macOS: Use -fullscreen or manual geometry with -toolwindow
        - Linux: Remove decorations via _MOTIF_WM_HINTS or fallback methods
        """
        # Give the window a title so OBS can identify it
        self.title("MinbarLive Subtitles")

        # The window deliberately stays in the taskbar/alt-tab (OBS window
        # capture needs WS_EX_APPWINDOW) — without an explicit icon it shows
        # Tk's default feather there. Same pattern as _set_toplevel_icon in
        # gui/widgets.py; no titlebar to re-theme here (caption is stripped
        # below).
        icon_loaded = False
        if ICO_SUPPORTED and os.path.exists(ICON_PATH):
            try:
                self.iconbitmap(ICON_PATH)
                icon_loaded = True
            except Exception:
                pass
        if not icon_loaded and os.path.exists(ICON_PATH_PNG):
            try:
                self.iconphoto(False, scaled_icon_photo(ICON_PATH_PNG))
            except Exception:
                pass

        # Store hwnd for later use (Windows only)
        self._hwnd = None

        if sys.platform == "win32":
            # Windows: Create a borderless window visible to OBS
            # We use Windows-specific styling to remove the title bar completely
            try:
                # Get the window handle and modify window styles to remove decorations
                # This needs to happen after the window is mapped
                self.update_idletasks()

                # Use Windows API via ctypes to remove window decorations
                # while keeping the window visible to capture software
                import ctypes
                from ctypes import wintypes

                # Window style constants
                GWL_STYLE = -16
                GWL_EXSTYLE = -20
                WS_CAPTION = 0x00C00000  # Title bar
                WS_THICKFRAME = 0x00040000  # Sizing border
                WS_MINIMIZEBOX = 0x00020000
                WS_MAXIMIZEBOX = 0x00010000
                WS_SYSMENU = 0x00080000  # System menu
                WS_EX_APPWINDOW = 0x00040000
                WS_EX_TOOLWINDOW = 0x00000080

                user32 = ctypes.windll.user32
                # HWND is pointer-sized.  Without explicit signatures ctypes
                # assumes a 32-bit int return value and can truncate handles in
                # a 64-bit process.
                user32.GetParent.argtypes = [wintypes.HWND]
                user32.GetParent.restype = wintypes.HWND
                user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
                user32.GetWindowLongW.restype = ctypes.c_long
                user32.SetWindowLongW.argtypes = [
                    wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_long,
                ]
                user32.SetWindowLongW.restype = ctypes.c_long

                # Get window handle
                self._hwnd = user32.GetParent(self.winfo_id())

                # Get current style
                style = user32.GetWindowLongW(self._hwnd, GWL_STYLE)

                # Remove title bar and borders but keep it a normal window
                style = style & ~WS_CAPTION & ~WS_THICKFRAME
                style = style & ~WS_MINIMIZEBOX & ~WS_MAXIMIZEBOX & ~WS_SYSMENU

                # Apply new style
                user32.SetWindowLongW(self._hwnd, GWL_STYLE, style)

                # Get and modify extended style to ensure it shows in window lists
                ex_style = user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
                # Remove toolwindow style, add APPWINDOW to ensure it appears in capture lists
                ex_style = (ex_style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
                user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex_style)

                # Note: We don't call SetWindowPos here - _set_screen_position will
                # handle positioning with _apply_window_position()

            except Exception:
                # Fallback: just use overrideredirect if ctypes fails
                self._hwnd = None
                self.overrideredirect(True)

        elif sys.platform == "darwin":
            # macOS: Use transparent title bar approach or fullscreen
            try:
                # Make the window borderless-looking while keeping it managed
                # On macOS, we can use the "transparent" appearance
                self.wm_attributes("-fullscreen", False)
                # Remove title bar but keep window managed
                self.tk.call(
                    "::tk::unsupported::MacWindowStyle",
                    "style",
                    self._w,
                    "plain",
                    "none",
                )
            except tk.TclError:
                # Fallback for older Tk versions
                self.overrideredirect(True)

        else:
            # Linux/Other: Use EWMH hints to remove decorations
            try:
                # Try to set _MOTIF_WM_HINTS to remove decorations
                # This keeps the window in the WM's control
                self.wm_attributes("-type", "splash")
            except tk.TclError:
                try:
                    # Alternative: try dock type
                    self.wm_attributes("-type", "dock")
                except tk.TclError:
                    # Final fallback
                    self.overrideredirect(True)

    def _delayed_font_update(self):
        """Update font after window is fully rendered."""
        self._delayed_font_job = None
        if self._destroying or self._is_hidden:
            return
        self.update_idletasks()
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()
        if self.canvas_width > 0:
            self._update_font()
            self._update_footer_visibility()

    def _apply_theme_palette(self, theme_mode: str):
        """Load the active visual palette for subtitle rendering."""
        if theme_mode not in self._theme_palettes:
            theme_mode = "dark"
        palette = self._theme_palettes[theme_mode]
        self._theme_mode = theme_mode
        self._bg_color = palette["bg_color"]
        self._primary_text = palette["primary_text"]
        self._secondary_text = palette["secondary_text"]
        self._translation_text = (
            getattr(self, "_translation_text_color_override", "")
            or self._primary_text
        )
        self._source_text = (
            getattr(self, "_source_text_color_override", "")
            or self._secondary_text
        )
        self._card_fill = palette["card_fill"]
        self._card_outline = palette["card_outline"]
        self._accent_outline = palette["accent_outline"]
        self._footer_bg = palette["footer_bg"]
        self._footer_fg = palette["footer_fg"]
        self._footer_outline = palette["footer_outline"]
        self._stopped_bg = palette["stopped_bg"]
        self._stopped_fg = palette["stopped_fg"]
        self._stopped_outline = palette["stopped_outline"]

    def set_theme(self, theme_mode: str):
        """Apply a new theme and repaint the current subtitle content."""
        self._apply_theme_palette(theme_mode)
        self.footer.configure(
            bg=self._footer_bg,
            fg=self._footer_fg,
            highlightbackground=self._footer_outline,
        )
        if self._canvas_footer_items:
            self._draw_canvas_footer()
        self._refresh_stopped_hint()
        if self._subtitle_mode == SUBTITLE_MODE_STATIC and self._transparent_static:
            self._apply_transparent_mode()
        else:
            self._apply_opaque_mode()
        self._refresh_subtitles()
        self._render_announcement()

    def _update_footer_visibility(self):
        """Draw or hide the footer pill and keep subtitle spacing readable."""
        if self._show_footer:
            # Rounded pill footer in every mode (continuous/static/live) so the
            # disclaimer looks identical everywhere. margin_bottom is set inside
            # _draw_canvas_footer to keep subtitles clear of the pill.
            self.footer.place_forget()
            self._draw_canvas_footer()
        else:
            self._remove_canvas_footer()
            self.footer.place_forget()
            self.margin_bottom = 8
        # The stopped-hint pill sits relative to the footer pill, so every
        # site that redraws the footer (resize, monitor, mode, height changes)
        # must reposition the hint too.
        self._refresh_stopped_hint()

    def _draw_canvas_footer(self):
        """Draw the footer as a centred rounded pill directly on the canvas."""
        import tkinter.font as tkfont

        self._remove_canvas_footer()
        if not self.canvas_width or not self.canvas_height:
            return

        footer_text = FOOTER_TRANSLATIONS.get(self._target_language, DEFAULT_FOOTER)
        font_spec = (self._footer_font_family, 12, "bold")
        font_obj = tkfont.Font(family=self._footer_font_family, size=12, weight="bold")

        pad_x, pad_y = 24, 8
        margin_h = 20  # min horizontal margin from canvas edge
        max_pill_w = self.canvas_width - margin_h * 2
        max_text_w = max(1, int(max_pill_w - pad_x * 2))
        # Footer translations can be longer than a compact/partial output
        # window. Wrap deliberately and size the pill to the actual rows so
        # the warning is never clipped or silently elided.
        lines = self._wrap_text_to_lines(footer_text, max_text_w, font_spec)
        text_w = max((font_obj.measure(line) for line in lines), default=0)
        text_h = font_obj.metrics("linespace") * max(1, len(lines))
        pill_w = min(text_w + pad_x * 2, max_pill_w)
        pill_h = text_h + pad_y * 2
        radius = pill_h / 2  # fully rounded ends (capsule shape)

        cx = self.canvas_width / 2
        # sit 10 px above the very bottom
        y2 = self.canvas_height - 10
        y1 = y2 - pill_h
        x1 = cx - pill_w / 2
        x2 = cx + pill_w / 2

        bg_id = self.canvas.create_polygon(
            self._rounded_rect_points(x1, y1, x2, y2, radius),
            smooth=True,
            splinesteps=18,
            fill=self._footer_bg,
            outline=self._footer_outline,
            width=1,
        )
        text_id = self.canvas.create_text(
            cx,
            (y1 + y2) / 2,
            # Lines have already been shaped by _wrap_text_to_lines.
            text="\n".join(lines),
            fill=self._footer_fg,
            font=font_spec,
            anchor="center",
            justify="center",
            width=int(pill_w - pad_x * 2),
        )
        self._canvas_footer_items = [bg_id, text_id]
        self.margin_bottom = pill_h + 18  # 10 px gap + pill + 8 px clearance above

    def _remove_canvas_footer(self):
        """Delete canvas-drawn pill footer items."""
        for item_id in self._canvas_footer_items:
            self.canvas.delete(item_id)
        self._canvas_footer_items = []

    def _raise_footer(self):
        """Keep the disclaimer pill above the subtitle text in the canvas
        z-order — items stack in creation order, so freshly added subtitles
        (and the live line) would otherwise draw over the warning as they
        scroll through its area.

        The announcement card is raised first (above subtitles/live line), then
        the two pills on top of it — the operator message covers the subtitles
        but the disclaimer stays visible (user decision)."""
        for item_id in self._announcement_items:
            self.canvas.tag_raise(item_id)
        for item_id in self._stopped_hint_items:
            self.canvas.tag_raise(item_id)
        for item_id in self._canvas_footer_items:
            self.canvas.tag_raise(item_id)

    def _cancel_after_job(self, attribute: str) -> None:
        """Cancel one tracked Tk callback without leaking Tcl errors.

        Window destruction, monitor changes and stop actions can race a
        callback that has just fired.  Clearing the attribute first makes
        cancellation idempotent and prevents a later cleanup path from
        cancelling a recycled Tcl callback id.
        """
        job = getattr(self, attribute, None)
        setattr(self, attribute, None)
        if job is None:
            return
        try:
            self.after_cancel(job)
        except (tk.TclError, TypeError):
            pass

    def _cancel_animation_jobs(self) -> None:
        """Cancel every delayed/animated callback owned by this window."""
        for attribute in (
            "_delayed_font_job",
            "_continuous_start_job",
            "_scroll_animation_id",
            "_feed_anim_job",
        ):
            self._cancel_after_job(attribute)

    def destroy(self):
        """Destroy the OBS surface after cancelling all of its Tk jobs."""
        if getattr(self, "_destroying", False):
            return
        self._destroying = True
        self._cancel_animation_jobs()
        super().destroy()

    def set_stopped_hint(self, visible: bool):
        """Show or remove the "translation stopped" pill.

        Shown while the window stays open with the pipeline stopped (the
        default hide_subtitle_on_stop=False setup), so the audience knows
        missing subtitles are deliberate. The text comes from
        status_messages in the target language, fetched at draw time so
        language and theme changes pick up the current wording.
        """
        self._stopped_hint = visible
        if visible:
            # A stopped audience surface must be visually still, and must not
            # leave Tk callbacks alive after the operator tears it down.
            self._cancel_after_job("_continuous_start_job")
            self._cancel_after_job("_scroll_animation_id")
            self._cancel_after_job("_feed_anim_job")
        elif self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            self._start_continuous_scroll()
        elif self._subtitle_mode == SUBTITLE_MODE_REALTIME:
            self._ensure_feed_anim()
        self._refresh_stopped_hint()

    def _refresh_stopped_hint(self):
        """(Re)draw or remove the stopped-hint pill above the footer pill.

        Tied to ``_show_footer`` (user decision) — it rides on the same
        disclaimer real estate, so turning the footer off also clears this."""
        for item_id in self._stopped_hint_items:
            self.canvas.delete(item_id)
        self._stopped_hint_items = []
        if (
            not self._stopped_hint
            or not self._show_footer
            or not self.canvas_width
            or not self.canvas_height
        ):
            return
        import tkinter.font as tkfont

        from utils.user_messages import get_user_message

        text = get_user_message("app_stopped")
        font_spec = (self._footer_font_family, 12, "bold")
        font_obj = tkfont.Font(family=self._footer_font_family, size=12, weight="bold")
        pad_x, pad_y = 22, 8
        pill_w = min(font_obj.measure(text) + pad_x * 2, self.canvas_width - 40)
        pill_h = font_obj.metrics("linespace") + pad_y * 2
        cx = self.canvas_width / 2
        # Directly above the disclaimer pill when it is shown. Footer copy may
        # wrap in compact output windows, so anchor to its actual canvas box
        # instead of assuming both pills have the same height.
        y2 = self.canvas_height - 10
        if self._canvas_footer_items:
            footer_bbox = self.canvas.bbox(self._canvas_footer_items[0])
            if footer_bbox:
                y2 = footer_bbox[1] - 8
        y1 = y2 - pill_h
        bg_id = self.canvas.create_polygon(
            self._rounded_rect_points(
                cx - pill_w / 2, y1, cx + pill_w / 2, y2, pill_h / 2
            ),
            smooth=True,
            splinesteps=18,
            fill=self._stopped_bg,
            outline=self._stopped_outline,
            width=1,
        )
        text_id = self.canvas.create_text(
            cx,
            (y1 + y2) / 2,
            text=_reshape_rtl(text),
            fill=self._stopped_fg,
            font=font_spec,
            anchor="center",
            justify="center",
            width=int(pill_w - pad_x * 2),
        )
        self._stopped_hint_items = [bg_id, text_id]

    def set_announcement(self, text: str):
        """Show (or replace) the big centred announcement card, or clear it
        when ``text`` is empty. Idempotent — re-setting the same text is a
        no-op so AppGUI can re-assert it cheaply on every window it creates."""
        text = (text or "").strip()
        if text == self._announcement_text:
            return
        self._announcement_text = text
        self._render_announcement()

    def clear_announcement(self):
        """Remove the announcement card from the canvas."""
        self.set_announcement("")

    def _remove_announcement_items(self):
        for item_id in self._announcement_items:
            self.canvas.delete(item_id)
        self._announcement_items = []

    def _render_announcement(self):
        """(Re)draw the announcement card centred on the canvas, big enough to
        read across a hall. Removed when there is no announcement text."""
        import tkinter.font as tkfont

        self._remove_announcement_items()
        if (
            not self._announcement_text
            or not self.canvas_width
            or not self.canvas_height
        ):
            return

        # A touch smaller than the max subtitle size so a longer message wraps
        # to a few lines inside the card instead of overflowing the screen.
        size = max(28, min(72, int(self.canvas_width / 22)))
        font_spec = (self._font_family, size, "bold")
        font_obj = tkfont.Font(family=self._font_family, size=size, weight="bold")

        max_text_w = int(self.canvas_width * 0.8)
        # Honour the operator's explicit line breaks, then width-wrap each
        # paragraph (a blank line stays blank for spacing).
        lines: list[str] = []
        for paragraph in self._announcement_text.split("\n"):
            lines.extend(self._wrap_text_to_lines(paragraph, max_text_w, font_spec))

        pad_x, pad_y = 44, 32
        line_h = font_obj.metrics("linespace")
        text_w = max((font_obj.measure(line) for line in lines), default=0)
        text_h = line_h * len(lines)
        card_w = min(text_w + pad_x * 2, self.canvas_width - 40)
        card_h = min(text_h + pad_y * 2, self.canvas_height - 40)
        cx = self.canvas_width / 2
        cy = self.canvas_height / 2
        x1, x2 = cx - card_w / 2, cx + card_w / 2
        y1, y2 = cy - card_h / 2, cy + card_h / 2

        bg_id = self.canvas.create_polygon(
            self._rounded_rect_points(x1, y1, x2, y2, 24),
            smooth=True,
            splinesteps=18,
            fill=self._card_fill,
            outline=self._accent_outline,
            width=2,
        )
        # lines are already shaped by _wrap_text_to_lines — do NOT reshape.
        text_id = self.canvas.create_text(
            cx,
            cy,
            text="\n".join(lines),
            fill=self._primary_text,
            font=font_spec,
            anchor="center",
            justify="center",
        )
        self._announcement_items = [bg_id, text_id]
        # Keep the disclaimer pills above the card (see _raise_footer).
        self._raise_footer()

    def _update_font(self):
        """Recalculate independent translation and source/live fonts."""
        font_size = (
            int(self.canvas_width / self._font_size_base) if self.canvas_width else 24
        )
        font_size = max(12, min(font_size, 120))  # Clamp between 12 and 120
        self._current_font_size = font_size
        self.font = (self._font_family, font_size, "bold")
        # Legacy/synthetic render harnesses may not have the new field yet;
        # derive their old 70% source size exactly. Real windows always carry
        # the independent persisted divisor set by the constructor.
        source_base = getattr(
            self, "_source_font_size_base", self._font_size_base / 0.7
        )
        source_size = (
            int(self.canvas_width / source_base) if self.canvas_width else 17
        )
        source_size = max(12, min(source_size, 120))
        self._current_source_font_size = source_size
        self.source_font = (self._font_family, source_size, "bold")
        # Latin-script source/live text: italic + regular weight, so it reads
        # apart from the bold upright translation even in the same script.
        # Arabic keeps the upright fonts — it has no italic tradition (Tk
        # would fake a slant) and the script itself already differs.
        self.source_font_latin = (self._slant_font_family, source_size, "italic")
        self.live_font_latin = self.source_font_latin

    def _source_font_for(self, text: str):
        return self.source_font if _ARABIC_ANY_RE.search(text) else self.source_font_latin

    def _live_font_for(self, text: str):
        return self.source_font if _ARABIC_ANY_RE.search(text) else self.live_font_latin

    def _translation_fill(self) -> str:
        """Effective translation colour, with legacy harness fallback."""
        return getattr(self, "_translation_text", self._primary_text)

    def _source_fill(self) -> str:
        """Effective original/live colour, with legacy harness fallback."""
        return getattr(self, "_source_text", self._secondary_text)

    def increase_font(self):
        """Increase subtitle font size."""
        new_base = max(20, self._font_size_base - 5)
        if new_base == self._font_size_base:
            return
        self._font_size_base = new_base
        self._update_font()
        self._refresh_subtitles(reflow=True)

    def decrease_font(self):
        """Decrease subtitle font size."""
        new_base = min(80, self._font_size_base + 5)
        if new_base == self._font_size_base:
            return
        self._font_size_base = new_base
        self._update_font()
        self._refresh_subtitles(reflow=True)

    def set_source_font_size_base(self, value: float) -> None:
        """Set the source/live font divisor and reflow the active surface."""
        try:
            new_base = max(20.0, min(120.0, float(value)))
        except (TypeError, ValueError):
            return
        if new_base == getattr(self, "_source_font_size_base", None):
            return
        self._source_font_size_base = new_base
        self._update_font()
        self._refresh_subtitles(reflow=True)

    def increase_source_font(self) -> None:
        """Increase original-text and live-transcript font size."""
        current = getattr(
            self, "_source_font_size_base", self._font_size_base / 0.7
        )
        self.set_source_font_size_base(current - 5.0)

    def decrease_source_font(self) -> None:
        """Decrease original-text and live-transcript font size."""
        current = getattr(
            self, "_source_font_size_base", self._font_size_base / 0.7
        )
        self.set_source_font_size_base(current + 5.0)

    def set_translation_text_color(self, color: str | None) -> None:
        """Apply a translation colour live; empty restores the theme default."""
        self._translation_text_color_override = (color or "").strip()
        self._translation_text = (
            self._translation_text_color_override or self._primary_text
        )
        self._refresh_subtitles()

    def set_source_text_color(self, color: str | None) -> None:
        """Apply an original/live colour live; empty restores the theme default."""
        self._source_text_color_override = (color or "").strip()
        self._source_text = self._source_text_color_override or self._secondary_text
        self._refresh_subtitles()

    def set_language(self, language: str):
        """Update the footer text based on target language."""
        self._target_language = language
        footer_text = FOOTER_TRANSLATIONS.get(language, DEFAULT_FOOTER)
        self.footer.configure(text=footer_text)
        if self._canvas_footer_items:
            self._draw_canvas_footer()
        self._refresh_stopped_hint()

    def set_show_footer(self, enabled: bool):
        """Show or hide the footer disclaimer."""
        self._show_footer = enabled
        self._update_footer_visibility()
        self._reposition_subtitles()

    def get_font_size_base(self) -> int:
        """Get current font size base (divisor) value for settings persistence."""
        return self._font_size_base

    def get_current_font_size(self) -> int:
        """Get the actual rendered font pixel size."""
        return getattr(self, "_current_font_size", 24)

    def get_source_font_size_base(self) -> float:
        """Get the independent source/live divisor for persistence."""
        return getattr(
            self, "_source_font_size_base", self._font_size_base / 0.7
        )

    def get_current_source_font_size(self) -> int:
        """Get the actual rendered original/live font pixel size."""
        return getattr(self, "_current_source_font_size", 17)

    def get_translation_text_color(self) -> str:
        """Return the saved override; empty means use the theme colour."""
        return getattr(self, "_translation_text_color_override", "")

    def get_source_text_color(self) -> str:
        """Return the saved override; empty means use the theme colour."""
        return getattr(self, "_source_text_color_override", "")

    def increase_scroll_speed(self) -> float:
        """Increase continuous scroll speed."""
        self._scroll_speed = min(5.0, self._scroll_speed + 0.5)  # Max 5 px/frame
        self._effective_scroll_speed = max(
            self._effective_scroll_speed, self._scroll_speed
        )
        return self._scroll_speed

    def decrease_scroll_speed(self) -> float:
        """Decrease continuous scroll speed."""
        self._scroll_speed = max(0.5, self._scroll_speed - 0.5)  # Min 0.5 px/frame
        self._effective_scroll_speed = min(
            self._effective_scroll_speed, self._scroll_speed
        )
        return self._scroll_speed

    def set_adaptive_catchup(self, enabled: bool):
        """Enable or disable adaptive catch-up for subtitle rendering."""
        self._adaptive_catchup = enabled

    def set_bilingual_mode(self, enabled: bool):
        """Show or hide the original text above the translation.

        Applies to subtitles added after the change; existing ones stay
        as-is. Since 2026-07-15 this no longer gates the live transcript
        line — that follows its own "Show live transcript" setting.
        """
        self._bilingual_mode = enabled
        self._render_live_line()

    def set_live_text(self, text: str | None, settled: bool = False):
        """Update or remove the live (in-progress) transcript line.

        ``settled`` marks a finished utterance whose translation is still in
        flight. It remains in the source/live visual role until the translated
        subtitle replaces it.

        Called on every GUI poll tick in streaming mode; no-ops when
        nothing changed, so the frequent calls are cheap.
        """
        text = (text or "").strip()
        if len(text) > LIVE_TEXT_MAX_CHARS:
            # Show the tail of a long utterance, word-aligned, truncated on
            # the logical text (before RTL shaping).
            tail = text[-LIVE_TEXT_MAX_CHARS:].split(" ", 1)[-1]
            text = "… " + tail
        if text == self._live_text and settled == self._live_settled:
            return
        self._live_text = text
        self._live_settled = settled
        self._render_live_line()

    def _remove_live_items(self):
        for text_id, box_id in self._live_items:
            self.canvas.delete(text_id)
            if box_id:
                self.canvas.delete(box_id)
        self._live_items = []

    def _render_live_line(self):
        """(Re)draw the live line — Realtime mode only.

        A text block at the feed's writing cursor, below the last settled
        translation (see _layout_live_feed), using the independent source/live
        size and colour. The other modes never show the live line.
        """
        self._remove_live_items()
        if self._subtitle_mode != SUBTITLE_MODE_REALTIME:
            return
        if not self._live_text:
            # The live line is gated solely by the "Show live transcript"
            # setting — the GUI mirrors "" while it is off. "Show original
            # text" only affects settled bilingual blocks (decoupled
            # 2026-07-15; supersedes the 2026-07-14 coupling — the two
            # switches were confusingly both required).
            self._layout_live_feed()
            return

        # Use the maintained canvas_width/height attributes — every resize
        # path keeps them current. Re-reading winfo here is stale when called
        # synchronously inside set_window_height_percent (the <Configure>
        # event hasn't been processed yet) and clobbered the fresh size,
        # corrupting the feed scroll target.
        max_width = self.canvas_width - 140
        live_font = self._live_font_for(self._live_text)
        lines = self._wrap_text_to_lines(self._live_text, max_width, live_font)
        # Show only the newest row(s): a long interim otherwise wraps to
        # several rows and shoves the settled history up by that much at once
        # (the feed never scrolls back down). The full utterance still
        # arrives as the settled translation block.
        if len(lines) > REALTIME_LIVE_MAX_ROWS:
            lines = lines[-REALTIME_LIVE_MAX_ROWS:]
        text_id = self.canvas.create_text(
            self.canvas_width / 2,
            self.canvas_height,
            text="\n".join(lines),
            fill=self._source_fill(),
            font=live_font,
            anchor="s",
            justify="center",
        )
        self._live_items = [(text_id, None)]
        self._layout_live_feed()
        self._raise_footer()

    def _layout_live_feed(self):
        """Top-down feed layout (Realtime mode): settled blocks stack from
        the top, the live line writes below the last one. When content
        reaches the bottom limit everything shifts up (chat-style, never
        back down) and blocks that leave through the top edge are deleted.

        The upward shift is eased (see _step_feed_anim) instead of applied
        instantly, so an added subtitle glides the feed up rather than
        snapping — much easier to follow while reading."""
        if self._subtitle_mode != SUBTITLE_MODE_REALTIME:
            return
        _bottoms, content_bottom = self._feed_natural_layout()
        if content_bottom is None:
            return
        limit = self.canvas_height - self.margin_bottom

        # Grow the target scroll so the newest content reaches the bottom
        # limit. Monotonic: never lower it, so the feed doesn't jump back down
        # when the live line shrinks or clears between utterances.
        needed = content_bottom - limit
        if needed > self._live_feed_scroll_target:
            self._live_feed_scroll_target = needed

        # A large gap (window resize, mode switch, big backlog) reads worse as
        # a long slide than as an instant settle — snap those; ease the normal
        # one-subtitle append.
        if self._live_feed_scroll_target - self._live_feed_scroll > max(limit, 1):
            self._live_feed_scroll = self._live_feed_scroll_target

        if self._reduced_motion:
            self._live_feed_scroll = self._live_feed_scroll_target

        self._render_feed_positions()
        if not self._reduced_motion:
            self._ensure_feed_anim()

    def _feed_natural_layout(self):
        """Bottoms of each settled block and the overall content bottom at
        zero scroll (Realtime feed). Returns (None, None) when there is
        nothing to lay out."""
        cursor = LIVE_FEED_TOP_MARGIN
        bottoms: list[float] = []
        for block in self.subtitle_stack:
            bottoms.append(cursor + block.height)
            # Wider than continuous mode's line_spacing so a bilingual pair
            # (tight _pair_gap inside) reads as one group per utterance.
            cursor = bottoms[-1] + REALTIME_BLOCK_SPACING
        if self._live_items:
            live_h = self._measure_block_height(
                self._live_items[0][0], self._live_items, None
            )
            return bottoms, cursor + live_h
        if bottoms:
            return bottoms, bottoms[-1]
        return None, None

    def _render_feed_positions(self):
        """Draw the feed at the current (animated) rendered scroll and evict
        blocks that have scrolled off the top edge."""
        bottoms, content_bottom = self._feed_natural_layout()
        if content_bottom is None:
            # Nothing left to show — reset so the next content starts at top.
            self._live_feed_scroll = 0.0
            self._live_feed_scroll_target = 0.0
            return
        cx = self.canvas_width / 2
        scroll = self._live_feed_scroll

        for block, natural_bottom in zip(
            self.subtitle_stack, bottoms, strict=True
        ):
            fill = self._translation_fill()
            if block.line_items:
                # Multi-row block: bottom row anchors the block, the rows
                # above re-chain at the tight ink-aware distance.
                self._stack_rows_tight(block.line_items, natural_bottom - scroll)
                for line_id, _box_id in block.line_items:
                    self.canvas.itemconfig(line_id, fill=fill)
                anchor_id = block.line_items[0][0]
            else:
                self.canvas.coords(block.text_id, cx, natural_bottom - scroll)
                self.canvas.itemconfig(block.text_id, fill=fill)
                anchor_id = block.text_id
            self._position_source_above(anchor_id, block.source_items)

        if self._live_items:
            self.canvas.coords(self._live_items[0][0], cx, content_bottom - scroll)

        # Evict blocks fully above the top edge (by their on-screen position).
        # Compensate both scroll values so survivors keep their positions.
        while self.subtitle_stack and bottoms and (bottoms[0] - scroll) <= 0:
            evicted = self.subtitle_stack.pop(0)
            self._delete_item(evicted)
            # Must mirror _feed_natural_layout's block spacing exactly, or
            # the scroll compensation drifts per eviction.
            extent = evicted.height + REALTIME_BLOCK_SPACING
            self._live_feed_scroll -= extent
            self._live_feed_scroll_target -= extent
            bottoms.pop(0)

    def _ensure_feed_anim(self):
        """Start the ease-to-target scroll animation if it isn't already
        running and there's a gap left to close."""
        if self._feed_anim_job is not None:
            return
        if (
            self._destroying
            or self._is_hidden
            or self._stopped_hint
            or self._reduced_motion
            or self._subtitle_mode != SUBTITLE_MODE_REALTIME
        ):
            return
        if self._live_feed_scroll_target - self._live_feed_scroll < LIVE_FEED_ANIM_SNAP_PX:
            return
        self._feed_anim_job = self.after(
            LIVE_FEED_ANIM_FRAME_MS, self._step_feed_anim
        )

    def _step_feed_anim(self):
        """One eased frame of the top-down feed scroll toward its target."""
        self._feed_anim_job = None
        if (
            self._destroying
            or self._is_hidden
            or self._stopped_hint
            or self._reduced_motion
            or self._subtitle_mode != SUBTITLE_MODE_REALTIME
        ):
            return
        gap = self._live_feed_scroll_target - self._live_feed_scroll
        if gap <= LIVE_FEED_ANIM_SNAP_PX:
            self._live_feed_scroll = self._live_feed_scroll_target
            self._render_feed_positions()
            return
        step = max(gap * LIVE_FEED_ANIM_EASE, LIVE_FEED_ANIM_MIN_STEP)
        self._live_feed_scroll = min(
            self._live_feed_scroll + step, self._live_feed_scroll_target
        )
        self._render_feed_positions()
        # Eviction inside the render may have closed the gap; re-check.
        remaining = self._live_feed_scroll_target - self._live_feed_scroll
        if remaining >= LIVE_FEED_ANIM_SNAP_PX:
            self._feed_anim_job = self.after(
                LIVE_FEED_ANIM_FRAME_MS, self._step_feed_anim
            )

    def get_subtitle_backlog_count(self) -> int:
        """Estimate how many subtitles are waiting below the visible anchor line."""
        if self._subtitle_mode != SUBTITLE_MODE_CONTINUOUS:
            return 0

        visible_anchor = self.canvas_height - self.margin_bottom
        backlog = 0
        for block in self.subtitle_stack:
            coords = self.canvas.coords(block.text_id)
            if coords and coords[1] > visible_anchor:
                backlog += 1
        return backlog

    def _current_scroll_speed(self) -> float:
        """Compute smoothed scroll speed with optional readability-first catch-up."""
        target_speed = self._scroll_speed
        if self._adaptive_catchup and self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            backlog = self.get_subtitle_backlog_count()
            # Readability-first: accelerate gently and cap at 2x.
            multiplier = min(2.0, 1.0 + (0.25 * backlog))
            target_speed = self._scroll_speed * multiplier

        self._effective_scroll_speed = (0.85 * self._effective_scroll_speed) + (
            0.15 * target_speed
        )
        return self._effective_scroll_speed

    def set_transparent_static(self, enabled: bool):
        """Enable or disable transparent background for static mode."""
        self._transparent_static = enabled
        if self._subtitle_mode == SUBTITLE_MODE_STATIC:
            if enabled:
                self._apply_transparent_mode()
            else:
                self._apply_opaque_mode()
            # Refresh subtitles to apply new style
            self._refresh_subtitles()

    def _apply_transparent_mode(self):
        """Make the window background transparent on desktop and keyable in OBS.

        Platform-specific transparency:
        - Windows: -transparentcolor makes green invisible on desktop
        - macOS: -transparent attribute with transparent background
        - Linux: Compositor-dependent, uses alpha channel if available
        """
        # Use a dedicated chroma key that does not appear in the control panel.
        chroma_color = self._transparent_key_color

        if sys.platform == "win32":
            # Windows: use transparent color (chroma green becomes invisible)
            self.configure(bg=chroma_color)
            self.canvas.configure(bg=chroma_color)
            self.wm_attributes("-transparentcolor", chroma_color)

        elif sys.platform == "darwin":
            # macOS: use native transparency
            try:
                self.wm_attributes("-transparent", True)
                # Use systemTransparent for true transparency
                self.configure(bg="systemTransparent")
                self.canvas.configure(bg="systemTransparent")
            except tk.TclError:
                # Fallback to chroma key if transparency not supported
                self.configure(bg=chroma_color)
                self.canvas.configure(bg=chroma_color)

        else:
            # Linux: Try compositor alpha transparency
            try:
                # Set background to a color we can make transparent
                self.configure(bg=chroma_color)
                self.canvas.configure(bg=chroma_color)
                # Request RGBA visual for true transparency
                # This works with compositing window managers (KDE, GNOME, etc.)
                self.wait_visibility()
                self.wm_attributes("-alpha", 0.99)  # Triggers RGBA mode
                # Now we can use transparent areas
                self.configure(bg="")
                self.canvas.configure(bg="")
            except tk.TclError:
                # Fallback to chroma key for OBS
                self.configure(bg=chroma_color)
                self.canvas.configure(bg=chroma_color)

        # Make always-on-top so subtitles stay visible over other windows
        # (respects the user's always_on_top setting).
        self._apply_topmost()

    def _apply_opaque_mode(self):
        """Restore the polished opaque subtitle surface."""
        if sys.platform == "win32":
            try:
                self.wm_attributes("-transparentcolor", "")
            except tk.TclError:
                pass
        else:
            try:
                self.wm_attributes("-alpha", 1.0)
            except tk.TclError:
                pass

        self.configure(bg=self._bg_color)
        self.canvas.configure(bg=self._bg_color)

        self._apply_topmost()

    def set_subtitle_mode(self, mode: str):
        """Set subtitle display mode: realtime, continuous or static."""
        # Stop any running animation
        self._cancel_after_job("_continuous_start_job")
        self._cancel_after_job("_scroll_animation_id")

        old_mode = self._subtitle_mode
        self._subtitle_mode = mode
        self._effective_scroll_speed = self._scroll_speed
        # Clear existing subtitles when changing mode
        self._clear_all_subtitles()

        # Handle transparent mode when switching to/from static
        if mode == SUBTITLE_MODE_STATIC and self._transparent_static:
            self._apply_transparent_mode()
        elif old_mode == SUBTITLE_MODE_STATIC and self._transparent_static:
            # Switching away from static transparent mode
            self._apply_opaque_mode()

        # Start continuous scroll animation if needed
        if mode == SUBTITLE_MODE_CONTINUOUS and not self._stopped_hint:
            self._start_continuous_scroll()

        # Recalculate margin since static mode uses a tighter footer gap
        self._update_footer_visibility()

        # Re-anchor the live line against the new mode's footer margin
        self._render_live_line()
        # The announcement card is mode-independent; redraw to restore its
        # z-order above the freshly re-added subtitle items.
        self._render_announcement()

    def get_subtitle_mode(self) -> str:
        """Get current subtitle mode."""
        return self._subtitle_mode

    def _delete_item(self, block: _SubtitleBlock):
        """Delete all canvas objects belonging to one subtitle entry."""
        self.canvas.delete(block.text_id)
        for group in (block.line_items, block.source_items):
            if group:
                for group_text_id, box_id in group:
                    if group_text_id and group_text_id != block.text_id:
                        self.canvas.delete(group_text_id)
                    if box_id:
                        self.canvas.delete(box_id)

    @staticmethod
    def _block_text_ids(block: _SubtitleBlock) -> list[int]:
        """Return each text item in a block exactly once."""
        ids: list[int] = []
        seen: set[int] = set()
        for item_id, _box_id in block.line_items or [(block.text_id, None)]:
            if item_id and item_id not in seen:
                ids.append(item_id)
                seen.add(item_id)
        for item_id, _box_id in block.source_items or []:
            if item_id and item_id not in seen:
                ids.append(item_id)
                seen.add(item_id)
        return ids

    def _block_bbox(self, block: _SubtitleBlock) -> tuple[int, int, int, int] | None:
        """Bounding box of all source and translation text in one block."""
        boxes = [
            bbox
            for item_id in self._block_text_ids(block)
            if (bbox := self.canvas.bbox(item_id)) is not None
        ]
        if not boxes:
            return None
        return (
            min(box[0] for box in boxes),
            min(box[1] for box in boxes),
            max(box[2] for box in boxes),
            max(box[3] for box in boxes),
        )

    def _move_block(self, block: _SubtitleBlock, dy: float) -> None:
        """Move every text/background item belonging to a block vertically."""
        moved: set[int] = set()
        for group in (block.line_items or [(block.text_id, None)], block.source_items or []):
            for text_id, box_id in group:
                for item_id in (text_id, box_id):
                    if item_id and item_id not in moved:
                        self.canvas.move(item_id, 0, dy)
                        moved.add(item_id)

    def _move_block_to_top(self, block: _SubtitleBlock, top: float) -> None:
        bbox = self._block_bbox(block)
        if bbox is not None:
            self._move_block(block, top - bbox[1])

    def _measure_block_height(self, text_id, line_items, source_items) -> int:
        """Total pixel height of a subtitle block (source + translation)."""
        ids = [li[0] for li in line_items] if line_items else [text_id]
        if source_items:
            ids += [si[0] for si in source_items]
        tops: list[int] = []
        bottoms: list[int] = []
        for item_id in ids:
            bbox = self.canvas.bbox(item_id)
            if bbox:
                tops.append(bbox[1])
                bottoms.append(bbox[3])
        return (max(bottoms) - min(tops)) if tops else 75

    def _stack_overlap(self, lower_text: str) -> int:
        """How far a line's bounding box may overlap the box of `lower_text`
        (translation font) directly below it.

        Tk boxes are font-METRIC sized: even at zero box gap the glyphs sit
        ~0.5em apart, because the lower font's leading above its tallest ink
        is blank pixels inside the box. Overlapping by that (ink-probe
        measured, loose-biased) amount pulls the lines visually together
        while the ink itself can never touch.

        The upper line always keeps its full descent zone, even when its
        text has no descenders: baseline distances must stay CONSTANT for
        the spacing to READ as even — the eye measures baselines, not
        descender tips, so tucking a descender-less line closer makes it an
        outlier (live-session feedback 2026-07-15)."""
        size = self._current_font_size
        lower_text = _HONORIFIC_LIGATURE_RE.sub("", lower_text)
        if _ARABIC_ANY_RE.search(lower_text):
            lower_ws = 0.03 * size  # Arabic marks reach almost the box top
        elif _TALL_DIACRITIC_RE.search(lower_text):
            lower_ws = 0.28 * size  # capital diacritics (probed 0.33×size)
        else:
            lower_ws = 0.50 * size  # plain caps/ascenders (probed 0.55×size)
        return max(0, round(lower_ws - 0.12 * size))

    def _stack_rows_tight(self, line_items, bottom_y: float | None = None):
        """Position a multi-row Realtime block's rows bottom-up so each row
        sits at the tight ink-aware distance above the one below
        (_stack_overlap). `bottom_y` anchors the bottom row; None keeps its
        current position (e.g. re-chaining after a font change)."""
        cx = self.canvas_width / 2
        below_id = below_text = None
        for line_id, _box_id in reversed(line_items):
            if below_id is None:
                if bottom_y is not None:
                    self.canvas.coords(line_id, cx, bottom_y)
            else:
                self.canvas.coords(
                    line_id,
                    cx,
                    self.canvas.bbox(below_id)[1] + self._stack_overlap(below_text),
                )
            below_id = line_id
            below_text = self.canvas.itemcget(line_id, "text")

    def _position_source_above(self, text_id: int, source_items):
        """Anchor the source text directly above the translation text.

        Used in Realtime and continuous mode, where the source is a single
        canvas text item; static mode positions per-line source cards itself.
        In Realtime mode `text_id` is the block's TOP row when the
        translation wraps to several per-row items.
        """
        if not source_items:
            return
        bbox = self.canvas.bbox(text_id)
        if not bbox:
            return
        lower_first_line = self.canvas.itemcget(text_id, "text").split("\n")[0]
        source_y = bbox[1] + self._stack_overlap(lower_first_line)
        for source_id, _box_id in source_items:
            self.canvas.coords(source_id, self.canvas_width / 2, source_y)

    def _clear_all_subtitles(self):
        """Remove all subtitle items from the canvas."""
        for block in self.subtitle_stack:
            self._delete_item(block)
        self.subtitle_stack.clear()
        self._cancel_after_job("_feed_anim_job")
        self._live_feed_scroll = 0.0
        self._live_feed_scroll_target = 0.0

    def _on_escape(self, _event=None):
        """Esc → stop the translation (never close). No-op when no stop
        callback is wired (e.g. a standalone render harness)."""
        if self._on_stop is not None:
            self._on_stop()

    def hide(self):
        """Make the window fully invisible on screen AND in OBS.

        Uses the Windows DWM color-key transparency so that OBS Window Capture
        (which uses Windows Graphics Capture) also sees a fully transparent
        window instead of a frozen last frame.
        """
        self._is_hidden = True
        self._cancel_animation_jobs()

        # 1. Remove all subtitle text from the canvas (incl. the live line)
        self._clear_all_subtitles()
        self._live_text = ""
        self._live_settled = False
        self._remove_live_items()

        # 2. Hide the footer (both label and canvas pill) and the stopped
        # hint (the flag survives, so show() restores the pill)
        self.footer.place_forget()
        self._remove_canvas_footer()
        for item_id in self._stopped_hint_items:
            self.canvas.delete(item_id)
        self._stopped_hint_items = []
        # Remove the announcement card too; the text is kept so show() can
        # restore it (the flag survives, like the stopped hint).
        self._remove_announcement_items()

        if sys.platform == "win32":
            chroma_color = self._transparent_key_color
            self.configure(bg=chroma_color)
            self.canvas.configure(bg=chroma_color)
            self.wm_attributes("-transparentcolor", chroma_color)
        else:
            self.attributes("-alpha", 0.0)

    def show(self):
        """Restore the window to full visibility (reverses hide())."""
        self._is_hidden = False
        if sys.platform == "win32":
            try:
                self.wm_attributes("-transparentcolor", "")
            except tk.TclError:
                pass
        else:
            self.attributes("-alpha", 1.0)

        if self._subtitle_mode == SUBTITLE_MODE_STATIC and self._transparent_static:
            self._apply_transparent_mode()
        else:
            self._apply_opaque_mode()
        if self._show_footer:
            self._update_footer_visibility()
        else:
            self._refresh_stopped_hint()
        self._render_announcement()
        if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS and not self._stopped_hint:
            self._start_continuous_scroll()

    def _rounded_rect_points(
        self, x1: float, y1: float, x2: float, y2: float, radius: float
    ) -> list[float]:
        """Build polygon points for a rounded rectangle on the canvas."""
        radius = max(0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
        return [
            x1 + radius,
            y1,
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]

    def _create_line_background(self, bbox: tuple[int, int, int, int]) -> int:
        """Create a tight dark background strip behind one rendered line."""
        padding_y = self._static_card_padding_y()
        x1 = bbox[0] - self._box_padding_x
        y1 = bbox[1] - padding_y
        x2 = bbox[2] + self._box_padding_x
        y2 = bbox[3] + padding_y
        return self.canvas.create_polygon(
            self._rounded_rect_points(x1, y1, x2, y2, self._box_radius),
            smooth=True,
            splinesteps=18,
            fill=self._card_fill,
            outline=self._card_outline,
            width=1,
        )

    def _update_line_background(self, box_id: int, bbox: tuple[int, int, int, int]):
        """Resize a rounded line background after font or position changes."""
        padding_y = self._static_card_padding_y()
        x1 = bbox[0] - self._box_padding_x
        y1 = bbox[1] - padding_y
        x2 = bbox[2] + self._box_padding_x
        y2 = bbox[3] + padding_y
        self.canvas.itemconfig(
            box_id,
            fill=self._card_fill,
            outline=self._card_outline,
            width=1,
        )
        self.canvas.coords(
            box_id,
            *self._rounded_rect_points(x1, y1, x2, y2, self._box_radius),
        )

    def _static_spacing_token(self, normal: int, minimum: int) -> int:
        """Scale one vertical token only for unusually shallow static surfaces."""
        if getattr(self, "_subtitle_mode", None) != SUBTITLE_MODE_STATIC:
            return normal
        height = max(1, int(getattr(self, "canvas_height", STATIC_COMPACT_LAYOUT_HEIGHT)))
        scale = min(1.0, height / STATIC_COMPACT_LAYOUT_HEIGHT)
        return max(minimum, min(normal, round(normal * scale)))

    def _static_card_padding_y(self) -> int:
        return self._static_spacing_token(self._box_padding_y, 2)

    def _static_line_spacing(self) -> int:
        return self._static_spacing_token(self._line_gap, 2)

    def _static_pair_spacing(self) -> int:
        return self._static_spacing_token(self._pair_gap, 1)

    def _static_top_margin(self) -> int:
        return self._static_spacing_token(16, 2)

    def _wrap_text_to_lines(self, text: str, max_width: int, font=None) -> list[str]:
        """Split text into rendered lines that fit within max_width pixels.

        Wrapping happens on the LOGICAL text (reading order) and every line
        is shaped individually afterwards. Wrapping the already-shaped
        visual-order string (the previous approach) put the sentence END of
        RTL text on the first line and, with a degenerate width, stacked
        words in reverse order. The returned lines are shaped and ready for
        canvas.create_text().
        """
        import tkinter.font as tkfont

        shaped_text = _reshape_rtl(text)
        if max_width <= 0:
            # Canvas not laid out yet — render unwrapped rather than
            # producing one reversed word per line.
            return [shaped_text]

        font_obj = tkfont.Font(font=font or self.font)

        # If the whole shaped text already fits, return it as one line.
        if font_obj.measure(shaped_text) <= max_width:
            return [shaped_text]

        # Fill lines with LOGICAL words, measuring the shaped candidate so
        # widths reflect the actual rendered glyphs.
        words = text.split()
        lines: list[str] = []
        current_line: list[str] = []

        for word in words:
            test_line = " ".join(current_line + [word])
            if font_obj.measure(_reshape_rtl(test_line)) <= max_width or not (
                current_line
            ):
                current_line.append(word)
            else:
                lines.append(_reshape_rtl(" ".join(current_line)))
                current_line = [word]

        if current_line:
            lines.append(_reshape_rtl(" ".join(current_line)))

        return lines if lines else [shaped_text]

    def _static_pair_height(
        self,
        text: str,
        source: str,
        translation_font,
        source_font,
    ) -> int:
        """Estimate the vertical ink/card extent of one static subtitle pair."""
        import tkinter.font as tkfont

        max_width = max(1, self.canvas_width - 140)
        line_gap = self._static_line_spacing()
        pair_gap = self._static_pair_spacing()
        translation_lines = self._wrap_text_to_lines(
            text, max_width, translation_font
        )
        translation_line_height = tkfont.Font(font=translation_font).metrics(
            "linespace"
        )
        height = (
            len(translation_lines) * translation_line_height
            + max(0, len(translation_lines) - 1) * line_gap
        )
        if source:
            source_lines = self._wrap_text_to_lines(source, max_width, source_font)
            source_line_height = tkfont.Font(font=source_font).metrics("linespace")
            height += (
                pair_gap
                + len(source_lines) * source_line_height
                + max(0, len(source_lines) - 1) * line_gap
            )
        # Only the outermost card padding expands the pair's total extent;
        # neighbouring line cards deliberately sit close together.
        return height + (2 * self._static_card_padding_y()) + (
            2 * STATIC_CARD_OUTLINE_ALLOWANCE
        )

    def _static_fonts_for_content(self, text: str, source: str):
        """Preserve the configured size ratio while fitting a static pair.

        Static mode cannot scroll. Exceptionally long bilingual utterances are
        therefore reduced only as much as needed to keep every line readable
        between the top margin and footer. The operator's configured sizes are
        untouched and apply again to the next shorter block.
        """
        translation_size = self._current_font_size
        source_size = self._current_source_font_size
        available_height = max(
            1,
            self.canvas_height - self.margin_bottom - self._static_top_margin(),
        )

        while True:
            translation_font = (self._font_family, translation_size, "bold")
            source_font = (
                (self._font_family, source_size, "bold")
                if _ARABIC_ANY_RE.search(source)
                else (self._slant_font_family, source_size, "italic")
            )
            required_height = self._static_pair_height(
                text, source, translation_font, source_font
            )
            if required_height <= available_height or (
                translation_size <= STATIC_MIN_FIT_FONT_SIZE
                and source_size <= STATIC_MIN_FIT_FONT_SIZE
            ):
                return translation_font, source_font

            scale = max(0.5, min(0.92, available_height / required_height))
            next_translation = max(
                STATIC_MIN_FIT_FONT_SIZE, int(translation_size * scale)
            )
            next_source = max(STATIC_MIN_FIT_FONT_SIZE, int(source_size * scale))
            if (
                next_translation == translation_size
                and translation_size > STATIC_MIN_FIT_FONT_SIZE
            ):
                next_translation -= 1
            if next_source == source_size and source_size > STATIC_MIN_FIT_FONT_SIZE:
                next_source -= 1
            translation_size, source_size = next_translation, next_source

    def _create_outlined_text(
        self, x: float, y: float, text: str, font=None, fill=None
    ) -> tuple:
        """Create per-line subtitle cards for the static display mode."""
        font = font or self.font
        fill = fill or self._translation_fill()
        # Use the maintained size rather than winfo_width(): immediately after
        # a native monitor/height resize Tk can still report the old surface.
        max_width = self.canvas_width - 140
        lines = self._wrap_text_to_lines(text, max_width, font)

        import tkinter.font as tkfont

        font_obj = tkfont.Font(font=font)
        line_height = font_obj.metrics("linespace")
        line_gap = self._static_line_spacing()
        line_items = []
        current_y = y

        # lines are already shaped by _wrap_text_to_lines – do NOT reshape again.
        for line_text in reversed(lines):
            text_id = self.canvas.create_text(
                x,
                current_y,
                text=line_text,
                fill=fill,
                font=font,
                anchor="s",
                justify="center",
            )

            bbox = self.canvas.bbox(text_id)
            if bbox:
                box_id = self._create_line_background(bbox)
                self.canvas.tag_raise(text_id, box_id)
            else:
                box_id = None

            line_items.append((text_id, box_id))
            current_y -= line_height + line_gap

        line_items.reverse()
        main_text_id = line_items[-1][0]

        return main_text_id, line_items

    def _refresh_subtitles(self, *, reflow: bool = False):
        """Refresh existing subtitles after a visual change.

        A font-size change must rebuild every block from its logical source so
        wrapping is recalculated. Merely applying a larger font to the old
        canvas rows makes a formerly fitting line extend beyond both screen
        edges. Theme-only refreshes retain the cheaper in-place repaint.
        """
        # A few lifecycle paths are deliberately exercised with a minimal
        # object harness before the canvas stack has been initialised. Treat
        # that state like an empty surface while still refreshing its layout.
        blocks = getattr(self, "subtitle_stack", ())
        if reflow and blocks:
            self._reflow_subtitle_blocks()
            return

        for block in blocks:
            if block.line_items:
                for line_text_id, box_id in block.line_items:
                    if line_text_id:
                        self.canvas.itemconfig(
                            line_text_id,
                            fill=self._translation_fill(),
                        )
                    if box_id:
                        bbox = self.canvas.bbox(line_text_id)
                        if bbox:
                            self._update_line_background(box_id, bbox)
            else:
                self.canvas.itemconfig(
                    block.text_id,
                    fill=self._translation_fill(),
                )
            if block.source_items:
                for source_id, box_id in block.source_items:
                    self.canvas.itemconfig(
                        source_id,
                        fill=self._source_fill(),
                    )
                    if box_id:
                        bbox = self.canvas.bbox(source_id)
                        if bbox:
                            self._update_line_background(box_id, bbox)

        for block in blocks:
            if block.line_items and self._subtitle_mode == SUBTITLE_MODE_REALTIME:
                # Multi-row Realtime blocks: re-chain the rows with the new
                # font BEFORE measuring — their height is position-dependent.
                self._stack_rows_tight(block.line_items)
            block.height = self._measure_block_height(
                block.text_id, block.line_items, block.source_items
            )
        self._reposition_subtitles()
        # Redraw the live line with the new font/theme colors
        self._render_live_line()

    def _reflow_subtitle_blocks(self):
        """Rebuild visible blocks for the current font and settle their layout.

        Realtime scrolling is intentionally snapped after this explicit user
        action. Easing from the old geometry would leave the newly enlarged
        live/newest text below the footer limit for several frames.
        """
        continuous_anchor_top = None
        if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS and self.subtitle_stack:
            anchor_bbox = self._block_bbox(self.subtitle_stack[0])
            if anchor_bbox is not None:
                continuous_anchor_top = float(anchor_bbox[1])
        entries = [
            (block.logical_text, block.logical_source)
            for block in self.subtitle_stack
            if block.logical_text
        ]
        if not entries or len(entries) != len(self.subtitle_stack):
            # Compatibility guard for synthetic/legacy blocks that predate the
            # logical fields: keep the old in-place behavior instead of erasing
            # content we cannot safely reconstruct (especially shaped RTL).
            self._refresh_subtitles(reflow=False)
            return

        self._cancel_after_job("_feed_anim_job")
        for block in self.subtitle_stack:
            self._delete_item(block)
        self.subtitle_stack.clear()
        self._live_feed_scroll = 0.0
        self._live_feed_scroll_target = 0.0

        for text, source in entries:
            self._add_subtitle_block(
                text,
                source,
                defer_layout=True,
                refresh_geometry=False,
            )

        if self._subtitle_mode == SUBTITLE_MODE_REALTIME:
            # Recreate/re-wrap the interim line first so it participates in the
            # final content height, then place everything at the visible limit.
            self._render_live_line()
            self._settle_live_feed_after_reflow()
        elif self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            self._layout_continuous_after_reflow(continuous_anchor_top)
            self._render_live_line()
        else:
            self._reposition_subtitles()
            self._render_live_line()
        self._raise_footer()

    def _layout_continuous_after_reflow(self, anchor_top: float | None) -> None:
        """Rebuild the ticker downward without consuming its queued backlog.

        The first block keeps the same top edge/read progress. Recomputed block
        heights then push every not-yet-seen block farther down in queue order,
        instead of anchoring the newest block and throwing older rows above the
        viewport.
        """
        if not self.subtitle_stack:
            return
        first_bbox = self._block_bbox(self.subtitle_stack[0])
        if first_bbox is None:
            return
        if anchor_top is None:
            visible_bottom = self.canvas_height - self.margin_bottom
            anchor_top = max(16.0, visible_bottom - (first_bbox[3] - first_bbox[1]))

        next_top = anchor_top
        for block in self.subtitle_stack:
            self._move_block_to_top(block, next_top)
            bbox = self._block_bbox(block)
            if bbox is None:
                continue
            block.height = bbox[3] - bbox[1]
            next_top = bbox[3] + self.line_spacing

    def _settle_live_feed_after_reflow(self):
        """Bottom-clamp Realtime content immediately after a font reflow."""
        self._cancel_after_job("_feed_anim_job")
        _bottoms, content_bottom = self._feed_natural_layout()
        if content_bottom is None:
            self._live_feed_scroll = 0.0
            self._live_feed_scroll_target = 0.0
            return
        limit = self.canvas_height - self.margin_bottom
        settled_scroll = max(0.0, content_bottom - limit)
        self._live_feed_scroll = settled_scroll
        self._live_feed_scroll_target = settled_scroll
        self._render_feed_positions()

    def _monitor_work_area(self, mon) -> tuple[int, int, int, int]:
        """Physical (x, y, w, h) work area — the monitor minus the taskbar —
        of the monitor ``mon`` lives on. Falls back to the full monitor bounds
        off Windows or on any failure."""
        full = (mon.x, mon.y, mon.width, mon.height)
        if sys.platform != "win32":
            return full
        try:
            import ctypes
            from ctypes import wintypes

            class _RECT(ctypes.Structure):
                _fields_ = [
                    ("left", wintypes.LONG),
                    ("top", wintypes.LONG),
                    ("right", wintypes.LONG),
                    ("bottom", wintypes.LONG),
                ]

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", _RECT),
                    ("rcWork", _RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            user32 = ctypes.windll.user32
            # restype must be a pointer-sized type or the 64-bit HMONITOR is
            # truncated and GetMonitorInfo then fails.
            user32.MonitorFromPoint.restype = ctypes.c_void_p
            user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
            user32.GetMonitorInfoW.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_MONITORINFO),
            ]
            user32.GetMonitorInfoW.restype = wintypes.BOOL
            MONITOR_DEFAULTTONEAREST = 2
            pt = wintypes.POINT(mon.x + mon.width // 2, mon.y + mon.height // 2)
            hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                w = info.rcWork
                return (w.left, w.top, w.right - w.left, w.bottom - w.top)
        except Exception:
            pass
        return full

    def _set_screen_position(self, force_redraw: bool = False):
        monitors = get_monitors()
        if self._monitor_index < len(monitors):
            mon = monitors[self._monitor_index]
        elif len(monitors) > 1:
            mon = monitors[1]
        else:
            mon = monitors[0]

        # A full-height overlay always fills the whole monitor so OBS captures
        # the entire frame (no uncovered taskbar strip at the bottom). Only a
        # *partial* overlay with always-on-top off is clamped to the work area
        # (monitor minus taskbar): as an ordinary window the topmost taskbar
        # would otherwise cover its bottom strip on screen. Topmost overlays
        # paint above the taskbar, so they never need the clamp.
        if self._window_height_percent >= 100 or self._always_on_top:
            base_x, base_y, base_w, base_h = mon.x, mon.y, mon.width, mon.height
        else:
            base_x, base_y, base_w, base_h = self._monitor_work_area(mon)

        if self._window_height_percent >= 100:
            # Full height - use the whole monitor
            x, y, width, height = base_x, base_y, base_w, base_h
        else:
            # Partial height - anchor at the bottom of the base rect
            height = int(base_h * self._window_height_percent / 100)
            if getattr(self, "_show_footer", False):
                height = max(height, min(base_h, MIN_FOOTER_SURFACE_HEIGHT))
            y = base_y + (base_h - height)
            x, width = base_x, base_w
        # Size just applied to the window. winfo_width/height lag the native
        # resize until the resulting <Configure> event is processed, so
        # callers that need the fresh size right away read this instead.
        self._applied_size = (width, height)

        # On Windows with hwnd, use SetWindowPos for precise borderless positioning
        if sys.platform == "win32" and self._hwnd:
            try:
                import ctypes
                from ctypes import wintypes

                SWP_NOZORDER = 0x0004
                SWP_FRAMECHANGED = 0x0020

                # Withdraw and redraw to force clean positioning
                if force_redraw:
                    self.withdraw()
                    self.update()

                # Use SetWindowPos for exact positioning (bypasses frame adjustments)
                user32 = ctypes.windll.user32
                user32.SetWindowPos.argtypes = [
                    wintypes.HWND,
                    wintypes.HWND,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    wintypes.UINT,
                ]
                user32.SetWindowPos.restype = wintypes.BOOL
                positioned = user32.SetWindowPos(
                    self._hwnd,
                    None,
                    x,
                    y,
                    width,
                    height,
                    SWP_NOZORDER | SWP_FRAMECHANGED,
                )
                if not positioned:
                    raise ctypes.WinError()

                if force_redraw:
                    self.deiconify()

            except Exception:
                # Fallback to tk geometry
                geom = f"{width}x{height}+{x}+{y}"
                self.geometry(geom)
        else:
            # Non-Windows or fallback: use tk geometry
            geom = f"{width}x{height}+{x}+{y}"
            if force_redraw:
                self.withdraw()
                self.geometry(geom)
                self.update()
                self.deiconify()
            else:
                self.geometry(geom)

        # Keep the overlay above other windows when it's a partial or
        # transparent overlay; a full-screen opaque overlay stays in normal
        # stacking. Gated by the user's always_on_top setting.
        self._apply_topmost()

    def _desired_topmost(self) -> bool:
        """Whether the overlay should sit above other windows, ignoring the
        user's always_on_top switch. A partial or transparent overlay needs it
        (otherwise it disappears behind other windows); a full-screen opaque
        overlay deliberately does not (it would hide the desktop)."""
        if self._window_height_percent < 100:
            return True
        return self._subtitle_mode == SUBTITLE_MODE_STATIC and self._transparent_static

    def _apply_topmost(self) -> None:
        """Apply the -topmost attribute from the current mode and the user's
        always_on_top setting."""
        try:
            self.wm_attributes(
                "-topmost", self._always_on_top and self._desired_topmost()
            )
        except tk.TclError:
            pass

    def set_always_on_top(self, enabled: bool):
        """Toggle always-on-top live. Off = the overlay no longer floats above
        other windows AND is laid out inside the work area (so the taskbar,
        which stays topmost, never covers it); on = full monitor, above the
        taskbar. _set_screen_position re-applies -topmost at the end, so it
        covers both. (The control panel's own topmost is handled in AppGUI.)"""
        self._cancel_animation_jobs()
        self._always_on_top = enabled
        self._set_screen_position(force_redraw=True)
        self.canvas_width, self.canvas_height = self._applied_size
        self._update_font()
        self._update_footer_visibility()
        self._refresh_subtitles(reflow=True)
        self._render_announcement()
        if not self._stopped_hint:
            if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
                self._start_continuous_scroll()
            elif self._subtitle_mode == SUBTITLE_MODE_REALTIME:
                self._ensure_feed_anim()

    def set_monitor(self, monitor_index: int):
        """Change the monitor where the subtitle window is displayed."""
        # A monitor switch can cross DPI domains and rebuild the native window
        # surface.  Never let callbacks created for the old surface fire into
        # the new one; the active mode is resumed after the redraw below.
        self._cancel_animation_jobs()
        self._monitor_index = monitor_index

        # Reposition window to the new monitor (winfo lags the native
        # resize — trust the size that was just applied, see
        # set_window_height_percent)
        self._set_screen_position()
        self.canvas_width, self.canvas_height = self._applied_size
        self._update_font()
        self._update_footer_visibility()  # pill is drawn in canvas coords
        self._refresh_subtitles(reflow=True)
        self._render_announcement()
        if not self._stopped_hint:
            if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
                self._start_continuous_scroll()
            elif self._subtitle_mode == SUBTITLE_MODE_REALTIME:
                self._ensure_feed_anim()

    def set_window_height_percent(self, percent: int):
        """Set window height as percentage of screen height (5-100)."""
        self._window_height_percent = max(5, min(100, percent))
        # Use force_redraw to prevent visual glitches during resize
        old_width = self.canvas_width
        old_height = self.canvas_height
        self._set_screen_position(force_redraw=True)
        # The canvas fills the window (relwidth/relheight=1), so take the
        # size _set_screen_position just applied — winfo still reports the
        # pre-resize size until the <Configure> event is processed.
        self.canvas_width, self.canvas_height = self._applied_size
        # The window is bottom-anchored on screen, so a height change moves
        # only the top edge. The Realtime feed is laid out from the canvas
        # top — shift its scroll by the height delta so the text keeps its
        # on-screen position instead of jumping with the top edge. (Shrinking
        # raises the scroll; blocks pushed above the new top edge are evicted
        # by _render_feed_positions as usual.)
        if (
            self._subtitle_mode == SUBTITLE_MODE_REALTIME
            and old_height > 1
            and self.canvas_height > 1
        ):
            delta = self.canvas_height - old_height
            if delta:
                self._live_feed_scroll -= delta
                self._live_feed_scroll_target -= delta
        elif self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            # The window is bottom-anchored: growing it moves the canvas top
            # upward. Shift ticker items by the same delta so their physical
            # screen position/read progress does not jump.
            delta = self.canvas_height - old_height
            if delta:
                for block in self.subtitle_stack:
                    self._move_block(block, delta)
        self._update_font()
        # The pill is drawn at fixed canvas coordinates near the bottom; the
        # canvas origin (window top-left) just moved, so redraw it at the new
        # canvas_height — otherwise it rides up on grow and sinks below the
        # screen on shrink.
        self._update_footer_visibility()
        if self.canvas_width != old_width or self._subtitle_mode == SUBTITLE_MODE_STATIC:
            self._refresh_subtitles(reflow=True)
        elif self._subtitle_mode == SUBTITLE_MODE_REALTIME:
            self._reposition_subtitles()
            self._render_live_line()
        self._render_announcement()
        # Bring window to front after resize
        self.lift()

    def add_subtitle(self, text: str, source_text: str | None = None):
        """Render a subtitle; in bilingual mode the original transcription is
        shown above the translation in a smaller, muted font.

        In Realtime mode an oversized settled translation (continuous speech
        flushes up to 12s of speech as one utterance) is split at sentence
        boundaries into separate, readable feed blocks. Bilingual pairs stay
        whole — the original can't be aligned to the translation
        per-sentence."""
        if not (text or "").strip():
            return
        if (
            self._subtitle_mode == SUBTITLE_MODE_REALTIME
            and not (self._bilingual_mode and source_text)
            and len(text) > REALTIME_MAX_BLOCK_CHARS
        ):
            for chunk in split_display_chunks(text, REALTIME_MAX_BLOCK_CHARS):
                self._add_subtitle_block(chunk, None)
            return
        self._add_subtitle_block(text, source_text)

    def _add_subtitle_block(
        self,
        text: str,
        source_text: str | None = None,
        *,
        defer_layout: bool = False,
        refresh_geometry: bool = True,
    ):
        if not (text or "").strip():
            return

        if refresh_geometry:
            self.update_idletasks()
            self.canvas_width = self.canvas.winfo_width()
            self.canvas_height = self.canvas.winfo_height()

        if self._subtitle_mode == SUBTITLE_MODE_STATIC:
            self._clear_all_subtitles()

        # Collapse any newlines / repeated spaces the STT or buffer join left
        # in the source so the original line renders as one clean run of text.
        source = (
            _WHITESPACE_RE.sub(" ", source_text).strip()
            if (self._bilingual_mode and source_text)
            else ""
        )

        use_outline = self._subtitle_mode == SUBTITLE_MODE_STATIC

        if use_outline:
            translation_font, fitted_source_font = self._static_fonts_for_content(
                text, source
            )
            text_id, line_items = self._create_outlined_text(
                self.canvas_width / 2,
                self.canvas_height - 4,
                text=text,
                font=translation_font,
            )
            source_items = None
            if source:
                top_bbox = self.canvas.bbox(line_items[0][0])
                source_y = (
                    top_bbox[1] if top_bbox else self.canvas_height - 80
                ) - self._static_pair_spacing()
                _, source_items = self._create_outlined_text(
                    self.canvas_width / 2,
                    source_y,
                    text=source,
                    font=fitted_source_font,
                    fill=self._source_fill(),
                )
            text_height = self._measure_block_height(text_id, line_items, source_items)
            self.subtitle_stack.append(
                _SubtitleBlock(
                    text_id=text_id,
                    height=text_height,
                    line_items=line_items,
                    source_items=source_items,
                    logical_text=text,
                    logical_source=source or None,
                )
            )
        else:
            # Use the same manual wrapping so Arabic is reshaped correctly
            # (Tkinter's built-in width= wrapping does not handle RTL/Arabic).
            max_width = self.canvas_width - 140
            lines = self._wrap_text_to_lines(text, max_width)
            line_items = None
            if self._subtitle_mode == SUBTITLE_MODE_REALTIME and len(lines) > 1:
                # One canvas item per wrapped row: a single multiline item is
                # locked to the font's linespace, but the rows should stack
                # at the same tight ink-aware distance as the source line
                # above the translation (_stack_overlap).
                line_items = [
                    (
                        self.canvas.create_text(
                            self.canvas_width / 2,
                            self.canvas_height,
                            text=line_text,
                            fill=self._translation_fill(),
                            font=self.font,
                            anchor="s",
                            justify="center",
                        ),
                        None,
                    )
                    for line_text in lines
                ]
                self._stack_rows_tight(line_items)
                text_id = line_items[-1][0]
            else:
                text_id = self.canvas.create_text(
                    self.canvas_width / 2,
                    self.canvas_height,
                    text="\n".join(lines),
                    fill=self._translation_fill(),
                    font=self.font,
                    anchor="s",
                    justify="center",
                )
            source_items = None
            if source:
                source_font = self._source_font_for(source)
                source_lines = self._wrap_text_to_lines(
                    source, max_width, source_font
                )
                source_id = self.canvas.create_text(
                    self.canvas_width / 2,
                    self.canvas_height,
                    text="\n".join(source_lines),
                    fill=self._source_fill(),
                    font=source_font,
                    anchor="s",
                    justify="center",
                )
                source_items = [(source_id, None)]
                self._position_source_above(
                    line_items[0][0] if line_items else text_id, source_items
                )
            text_height = self._measure_block_height(text_id, line_items, source_items)
            self.subtitle_stack.append(
                _SubtitleBlock(
                    text_id=text_id,
                    height=text_height,
                    line_items=line_items,
                    source_items=source_items,
                    logical_text=text,
                    logical_source=source or None,
                )
            )

        if defer_layout:
            return

        if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            lowest_y = self.canvas_height - self.margin_bottom
            for existing in self.subtitle_stack[:-1]:
                coords = self.canvas.coords(existing.text_id)
                if coords:
                    text_bottom = coords[1]
                    potential_y = text_bottom + text_height + self.line_spacing
                    if potential_y > lowest_y:
                        lowest_y = potential_y

            self.canvas.coords(text_id, self.canvas_width / 2, lowest_y)
            self._position_source_above(text_id, source_items)
            if len(self.subtitle_stack) == 1:
                # A block taller than the available viewport must start with
                # its first line visible. Its lower rows can then enter in
                # normal reading order as the ticker scrolls upward.
                block = self.subtitle_stack[-1]
                bbox = self._block_bbox(block)
                if bbox is not None and bbox[1] < 16:
                    self._move_block(block, 16 - bbox[1])
        else:
            self._reposition_subtitles()

        # Newly created items sit above the live line in the canvas z-order;
        # re-raise it (Realtime mode) so transient overlaps during layout
        # shifts pass behind it. The disclaimer pill goes on top of
        # everything — the warning must never disappear behind text.
        for live_text_id, live_box_id in self._live_items:
            if live_box_id:
                self.canvas.tag_raise(live_box_id)
            self.canvas.tag_raise(live_text_id)
        self._raise_footer()

    def _start_continuous_scroll(self):
        """Start the continuous upward scroll animation."""
        self._continuous_start_job = None
        if (
            self._destroying
            or self._is_hidden
            or self._stopped_hint
            or self._subtitle_mode != SUBTITLE_MODE_CONTINUOUS
            or self._scroll_animation_id is not None
        ):
            return
        self._animate_continuous_scroll()

    def _animate_continuous_scroll(self):
        """Animation frame for continuous scroll mode."""
        # The callback currently executing is no longer cancellable.
        self._scroll_animation_id = None
        if (
            self._destroying
            or self._is_hidden
            or self._stopped_hint
            or self._subtitle_mode != SUBTITLE_MODE_CONTINUOUS
        ):
            return

        current_speed = self._current_scroll_speed()
        items_to_remove = []

        for i, block in enumerate(self.subtitle_stack):
            # Move text upward using current scroll speed
            self.canvas.move(block.text_id, 0, -current_speed)
            if block.source_items:
                for source_id, _box_id in block.source_items:
                    self.canvas.move(source_id, 0, -current_speed)

            # Check if text is completely off screen (above top)
            coords = self.canvas.coords(block.text_id)
            if coords:
                y = coords[1]
                # If the bottom of the text is above the screen, remove it
                if y + block.height < 0:
                    items_to_remove.append(i)

        # Remove items that scrolled off screen (in reverse order to preserve indices)
        for i in reversed(items_to_remove):
            self._delete_item(self.subtitle_stack.pop(i))

        # Schedule next frame
        self._scroll_animation_id = self.after(
            SCROLL_INTERVAL_MS, self._animate_continuous_scroll
        )

    def _reposition_subtitles(self):
        if self._subtitle_mode == SUBTITLE_MODE_REALTIME:
            # Feed layout owns all positioning in Realtime mode (and must run
            # even with an empty stack — the live line may need placing)
            self._layout_live_feed()
            return
        if not self.subtitle_stack:
            return

        import tkinter.font as tkfont

        current_y = self.canvas_height - self.margin_bottom
        line_gap = self._static_line_spacing()
        pair_gap = self._static_pair_spacing()

        for i in range(len(self.subtitle_stack) - 1, -1, -1):
            block = self.subtitle_stack[i]

            if block.line_items:
                translation_font = self.canvas.itemcget(
                    block.line_items[-1][0], "font"
                )
                font_obj = tkfont.Font(font=translation_font)
                line_height = font_obj.metrics("linespace")
                line_y = current_y

                for line_text_id, box_id in reversed(block.line_items):
                    self.canvas.coords(line_text_id, self.canvas_width / 2, line_y)

                    if box_id:
                        bbox = self.canvas.bbox(line_text_id)
                        if bbox:
                            self._update_line_background(box_id, bbox)

                    self.canvas.itemconfig(
                        line_text_id, fill=self._translation_fill()
                    )

                    line_y -= line_height + line_gap

                if block.source_items:
                    # Anchor the bottom source row to the actual top ink bound
                    # of the translation. This keeps the intended bilingual
                    # pair gap even when compact static spacing is active.
                    top_translation_bbox = self.canvas.bbox(block.line_items[0][0])
                    source_y = (
                        top_translation_bbox[1]
                        if top_translation_bbox is not None
                        else line_y
                    ) - pair_gap
                    source_font = self.canvas.itemcget(
                        block.source_items[-1][0], "font"
                    )
                    source_font_obj = tkfont.Font(font=source_font)
                    source_line_height = source_font_obj.metrics("linespace")
                    for source_id, box_id in reversed(block.source_items):
                        self.canvas.coords(
                            source_id, self.canvas_width / 2, source_y
                        )
                        if box_id:
                            bbox = self.canvas.bbox(source_id)
                            if bbox:
                                self._update_line_background(box_id, bbox)
                        self.canvas.itemconfig(source_id, fill=self._source_fill())
                        source_y -= source_line_height + line_gap
            else:
                self.canvas.coords(block.text_id, self.canvas_width / 2, current_y)
                self._position_source_above(block.text_id, block.source_items)

                self.canvas.itemconfig(
                    block.text_id, fill=self._translation_fill()
                )

            current_y -= block.height + self.line_spacing

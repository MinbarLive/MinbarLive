"""Dedicated subtitle window (full-screen) for displaying translations."""

from __future__ import annotations

import re
import sys
import tkinter as tk
from dataclasses import dataclass
from screeninfo import get_monitors

_WHITESPACE_RE = re.compile(r"\s+")

try:
    import arabic_reshaper
    from bidi.algorithm import get_display as bidi_get_display

    _ARABIC_SUPPORT = True
except ImportError:
    _ARABIC_SUPPORT = False


def _reshape_rtl(text: str) -> str:
    """Reshape and apply bidi algorithm to Arabic/RTL text for correct rendering.

    Tkinter does not natively handle Arabic text shaping or RTL direction,
    so we pre-process the text with arabic-reshaper and python-bidi before
    passing it to the canvas.  Non-Arabic text is returned unchanged.
    """
    if not _ARABIC_SUPPORT:
        return text
    try:
        reshaped = arabic_reshaper.reshape(text)
        return bidi_get_display(reshaped)
    except Exception as exc:
        # A silent fallback renders Arabic reversed with disconnected
        # letters — make the cause visible so it can be diagnosed.
        try:
            from utils.logging import log

            log(f"RTL reshaping failed, rendering raw text: {exc}", level="WARNING")
        except Exception:
            pass
        return text


if _ARABIC_SUPPORT:
    # Warm up the reshaper's lazy config load at import time, so the first
    # rendered Arabic line pays no first-call cost (and any init failure
    # surfaces here, once, instead of on live subtitles).
    _reshape_rtl("تهيئة")


from config import FOOTER_TRANSLATIONS_PATH, LINE_SPACING, MARGIN_BOTTOM
from utils.json_helpers import load_json
from utils.settings import (
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
)


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
    ):
        super().__init__(master)
        is_windows = sys.platform == "win32"
        self._on_close = on_close
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
        self._effective_scroll_speed = scroll_speed
        self._theme_mode = theme_mode
        self._theme_palettes = {
            "dark": {
                "bg_color": "#040914",
                "primary_text": "#ffffff",
                "secondary_text": "#c7d2e3",
                "card_fill": "#0a0a0a",
                "card_outline": "",
                "footer_bg": "#0e1828",
                "footer_fg": "#f4d18a",
                "footer_outline": "#8f6a29",
            },
            "light": {
                "bg_color": "#eef3fb",
                "primary_text": "#000000",
                "secondary_text": "#54657d",
                "card_fill": "#ffffff",
                "card_outline": "",
                "footer_bg": "#f6f9ff",
                "footer_fg": "#3b4f67",
                "footer_outline": "#d8e1ee",
            },
        }
        self._box_padding_x = 18
        self._box_padding_y = 6
        self._box_radius = 8
        self._line_gap = 8
        self._transparent_key_color = "#00fe00"
        self._font_family = "Segoe UI Semibold" if is_windows else "Helvetica"
        self._footer_font_family = "Segoe UI" if is_windows else "Helvetica"
        self._apply_theme_palette(self._theme_mode)

        self.configure(bg=self._bg_color)

        # Configure window to be borderless but still visible to OBS/screen capture
        # We avoid overrideredirect(True) because it makes the window invisible to
        # OBS window capture on most platforms.
        self._setup_borderless_window()

        self.bind("<Escape>", lambda e: self._on_close())
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Position on correct monitor BEFORE showing
        self._set_screen_position()

        self.canvas = tk.Canvas(self, bg=self._bg_color, highlightthickness=0)
        self.canvas.place(relx=0, rely=0.0, relwidth=1, relheight=1.0)

        # Font size base (divisor for calculating font size)
        self._font_size_base = font_size_base

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
        # Live (in-progress) transcript line — Realtime mode only, never part
        # of the stack. "Settled" = utterance finished, translation in flight
        # (rendered in the primary color instead of the muted one).
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

        self.update_idletasks()
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()
        self._update_font()
        self._update_footer_visibility()

        if self._transparent_static and self._subtitle_mode == SUBTITLE_MODE_STATIC:
            self._apply_transparent_mode()

        self.after(100, self._delayed_font_update)

        if self._subtitle_mode == SUBTITLE_MODE_CONTINUOUS:
            self.after(150, self._start_continuous_scroll)

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

                # Get window handle
                self._hwnd = ctypes.windll.user32.GetParent(self.winfo_id())

                # Get current style
                style = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_STYLE)

                # Remove title bar and borders but keep it a normal window
                style = style & ~WS_CAPTION & ~WS_THICKFRAME
                style = style & ~WS_MINIMIZEBOX & ~WS_MAXIMIZEBOX & ~WS_SYSMENU

                # Apply new style
                ctypes.windll.user32.SetWindowLongW(self._hwnd, GWL_STYLE, style)

                # Get and modify extended style to ensure it shows in window lists
                ex_style = ctypes.windll.user32.GetWindowLongW(self._hwnd, GWL_EXSTYLE)
                # Remove toolwindow style, add APPWINDOW to ensure it appears in capture lists
                ex_style = (ex_style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
                ctypes.windll.user32.SetWindowLongW(self._hwnd, GWL_EXSTYLE, ex_style)

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
        self._card_fill = palette["card_fill"]
        self._card_outline = palette["card_outline"]
        self._footer_bg = palette["footer_bg"]
        self._footer_fg = palette["footer_fg"]
        self._footer_outline = palette["footer_outline"]
        # Footer is always app orange with black text, regardless of theme
        self._footer_bg = "#F5820D"
        self._footer_fg = "#000000"
        self._footer_outline = "#c06308"

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
        if self._subtitle_mode == SUBTITLE_MODE_STATIC and self._transparent_static:
            self._apply_transparent_mode()
        else:
            self._apply_opaque_mode()
        self._refresh_subtitles()

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

    def _draw_canvas_footer(self):
        """Draw the footer as a centred rounded pill directly on the canvas."""
        import tkinter.font as tkfont

        self._remove_canvas_footer()
        if not self.canvas_width or not self.canvas_height:
            return

        footer_text = FOOTER_TRANSLATIONS.get(self._target_language, DEFAULT_FOOTER)
        font_spec = (self._footer_font_family, 13, "bold")
        font_obj = tkfont.Font(family=self._footer_font_family, size=13, weight="bold")
        text_w = font_obj.measure(footer_text)
        text_h = font_obj.metrics("linespace")

        pad_x, pad_y = 22, 9
        margin_h = 20  # min horizontal margin from canvas edge
        max_pill_w = self.canvas_width - margin_h * 2
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
            text=_reshape_rtl(footer_text),
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
        scroll through its area."""
        for item_id in self._canvas_footer_items:
            self.canvas.tag_raise(item_id)

    def _update_font(self):
        """Recalculate font based on canvas width and font size base (divisor)."""
        font_size = (
            int(self.canvas_width / self._font_size_base) if self.canvas_width else 24
        )
        font_size = max(12, min(font_size, 120))  # Clamp between 12 and 120
        self._current_font_size = font_size
        self.font = (self._font_family, font_size, "bold")
        # Bilingual mode renders the original text smaller above the translation
        self.source_font = (self._font_family, max(12, int(font_size * 0.7)), "bold")

    def increase_font(self):
        """Increase subtitle font size."""
        self._font_size_base = max(20, self._font_size_base - 5)
        self._update_font()
        self._refresh_subtitles()

    def decrease_font(self):
        """Decrease subtitle font size."""
        self._font_size_base = min(80, self._font_size_base + 5)
        self._update_font()
        self._refresh_subtitles()

    def set_language(self, language: str):
        """Update the footer text based on target language."""
        self._target_language = language
        footer_text = FOOTER_TRANSLATIONS.get(language, DEFAULT_FOOTER)
        self.footer.configure(text=footer_text)
        if self._canvas_footer_items:
            self._draw_canvas_footer()

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

        Applies to subtitles added after the change; existing ones stay as-is.
        """
        self._bilingual_mode = enabled

    def set_live_text(self, text: str | None, settled: bool = False):
        """Update or remove the live (in-progress) transcript line.

        ``settled`` marks a finished utterance whose translation is still
        in flight — the line turns to the primary text color in place
        ("finished") until the translation subtitle replaces it.

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
        translation (see _layout_live_feed): full subtitle size, muted
        while the speaker is still talking, primary ("finished") once the
        utterance settled and its translation is in flight. The other
        modes never show the live line.
        """
        self._remove_live_items()
        if self._subtitle_mode != SUBTITLE_MODE_REALTIME:
            return
        if not self._live_text:
            self._layout_live_feed()
            return

        self.update_idletasks()
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()

        max_width = self.canvas_width - 140
        lines = self._wrap_text_to_lines(self._live_text, max_width, self.font)
        text_id = self.canvas.create_text(
            self.canvas_width / 2,
            self.canvas_height,
            text="\n".join(lines),
            fill=self._primary_text if self._live_settled else self._secondary_text,
            font=self.font,
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

        self._render_feed_positions()
        self._ensure_feed_anim()

    def _feed_natural_layout(self):
        """Bottoms of each settled block and the overall content bottom at
        zero scroll (Realtime feed). Returns (None, None) when there is
        nothing to lay out."""
        cursor = LIVE_FEED_TOP_MARGIN
        bottoms: list[float] = []
        for block in self.subtitle_stack:
            bottoms.append(cursor + block.height)
            cursor = bottoms[-1] + self.line_spacing
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

        last = len(self.subtitle_stack) - 1
        for i, (block, natural_bottom) in enumerate(
            zip(self.subtitle_stack, bottoms, strict=True)
        ):
            self.canvas.coords(block.text_id, cx, natural_bottom - scroll)
            self._position_source_above(block.text_id, block.source_items)
            self.canvas.itemconfig(
                block.text_id,
                fill=self._primary_text if i == last else self._secondary_text,
            )

        if self._live_items:
            self.canvas.coords(self._live_items[0][0], cx, content_bottom - scroll)

        # Evict blocks fully above the top edge (by their on-screen position).
        # Compensate both scroll values so survivors keep their positions.
        while self.subtitle_stack and bottoms and (bottoms[0] - scroll) <= 0:
            evicted = self.subtitle_stack.pop(0)
            self._delete_item(evicted)
            extent = evicted.height + self.line_spacing
            self._live_feed_scroll -= extent
            self._live_feed_scroll_target -= extent
            bottoms.pop(0)

    def _ensure_feed_anim(self):
        """Start the ease-to-target scroll animation if it isn't already
        running and there's a gap left to close."""
        if self._feed_anim_job is not None:
            return
        if self._subtitle_mode != SUBTITLE_MODE_REALTIME:
            return
        if self._live_feed_scroll_target - self._live_feed_scroll < LIVE_FEED_ANIM_SNAP_PX:
            return
        self._feed_anim_job = self.after(
            LIVE_FEED_ANIM_FRAME_MS, self._step_feed_anim
        )

    def _step_feed_anim(self):
        """One eased frame of the top-down feed scroll toward its target."""
        self._feed_anim_job = None
        if self._subtitle_mode != SUBTITLE_MODE_REALTIME:
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
        self.wm_attributes("-topmost", True)

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

        self.wm_attributes("-topmost", True)

    def set_subtitle_mode(self, mode: str):
        """Set subtitle display mode: realtime, continuous or static."""
        # Stop any running animation
        if self._scroll_animation_id:
            self.after_cancel(self._scroll_animation_id)
            self._scroll_animation_id = None

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
        if mode == SUBTITLE_MODE_CONTINUOUS:
            self._start_continuous_scroll()

        # Recalculate margin since static mode uses a tighter footer gap
        self._update_footer_visibility()

        # Re-anchor the live line against the new mode's footer margin
        self._render_live_line()

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

    def _position_source_above(self, text_id: int, source_items):
        """Anchor the source text directly above the translation text.

        Used in continuous mode, where the source is a single canvas
        text item; static mode positions per-line source cards itself.
        """
        if not source_items:
            return
        bbox = self.canvas.bbox(text_id)
        if not bbox:
            return
        source_y = bbox[1] - self._line_gap
        for source_id, _box_id in source_items:
            self.canvas.coords(source_id, self.canvas_width / 2, source_y)

    def _clear_all_subtitles(self):
        """Remove all subtitle items from the canvas."""
        for block in self.subtitle_stack:
            self._delete_item(block)
        self.subtitle_stack.clear()
        if self._feed_anim_job is not None:
            self.after_cancel(self._feed_anim_job)
            self._feed_anim_job = None
        self._live_feed_scroll = 0.0
        self._live_feed_scroll_target = 0.0

    def hide(self):
        """Make the window fully invisible on screen AND in OBS.

        Uses the Windows DWM color-key transparency so that OBS Window Capture
        (which uses Windows Graphics Capture) also sees a fully transparent
        window instead of a frozen last frame.
        """
        # 1. Remove all subtitle text from the canvas (incl. the live line)
        self._clear_all_subtitles()
        self._live_text = ""
        self._live_settled = False
        self._remove_live_items()

        # 2. Hide the footer (both label and canvas pill)
        self.footer.place_forget()
        self._remove_canvas_footer()

        if sys.platform == "win32":
            chroma_color = self._transparent_key_color
            self.configure(bg=chroma_color)
            self.canvas.configure(bg=chroma_color)
            self.wm_attributes("-transparentcolor", chroma_color)
        else:
            self.attributes("-alpha", 0.0)

    def show(self):
        """Restore the window to full visibility (reverses hide())."""
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
        x1 = bbox[0] - self._box_padding_x
        y1 = bbox[1] - self._box_padding_y
        x2 = bbox[2] + self._box_padding_x
        y2 = bbox[3] + self._box_padding_y
        return self.canvas.create_polygon(
            self._rounded_rect_points(x1, y1, x2, y2, self._box_radius),
            smooth=True,
            splinesteps=18,
            fill=self._card_fill,
            outline="",
        )

    def _update_line_background(self, box_id: int, bbox: tuple[int, int, int, int]):
        """Resize a rounded line background after font or position changes."""
        x1 = bbox[0] - self._box_padding_x
        y1 = bbox[1] - self._box_padding_y
        x2 = bbox[2] + self._box_padding_x
        y2 = bbox[3] + self._box_padding_y
        self.canvas.itemconfig(
            box_id,
            fill=self._card_fill,
            outline="",
        )
        self.canvas.coords(
            box_id,
            *self._rounded_rect_points(x1, y1, x2, y2, self._box_radius),
        )

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

    def _create_outlined_text(
        self, x: float, y: float, text: str, font=None, fill=None
    ) -> tuple:
        """Create per-line subtitle cards for the static display mode."""
        font = font or self.font
        fill = fill or self._primary_text
        max_width = self.canvas.winfo_width() - 140
        lines = self._wrap_text_to_lines(text, max_width, font)

        import tkinter.font as tkfont

        font_obj = tkfont.Font(font=font)
        line_height = font_obj.metrics("linespace")
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
            current_y -= line_height + self._line_gap

        line_items.reverse()
        main_text_id = line_items[-1][0]

        return main_text_id, line_items

    def _refresh_subtitles(self):
        """Update font for all existing subtitles."""
        for block in self.subtitle_stack:
            if block.line_items:
                for line_text_id, box_id in block.line_items:
                    if line_text_id:
                        self.canvas.itemconfig(line_text_id, font=self.font)
                    if box_id:
                        bbox = self.canvas.bbox(line_text_id)
                        if bbox:
                            self._update_line_background(box_id, bbox)
            else:
                self.canvas.itemconfig(block.text_id, font=self.font)
            if block.source_items:
                for source_id, box_id in block.source_items:
                    self.canvas.itemconfig(source_id, font=self.source_font)
                    if box_id:
                        bbox = self.canvas.bbox(source_id)
                        if bbox:
                            self._update_line_background(box_id, bbox)

        for block in self.subtitle_stack:
            block.height = self._measure_block_height(
                block.text_id, block.line_items, block.source_items
            )
        self._reposition_subtitles()
        # Redraw the live line with the new font/theme colors
        self._render_live_line()

    def _set_screen_position(self, force_redraw: bool = False):
        monitors = get_monitors()
        if self._monitor_index < len(monitors):
            mon = monitors[self._monitor_index]
        elif len(monitors) > 1:
            mon = monitors[1]
        else:
            mon = monitors[0]

        if self._window_height_percent >= 100:
            # Full screen - use exact monitor dimensions
            x, y, width, height = mon.x, mon.y, mon.width, mon.height
        else:
            # Partial height - anchor at bottom of screen
            height = int(mon.height * self._window_height_percent / 100)
            y = mon.y + (mon.height - height)
            x, width = mon.x, mon.width

        # On Windows with hwnd, use SetWindowPos for precise borderless positioning
        if sys.platform == "win32" and self._hwnd:
            try:
                import ctypes

                SWP_NOZORDER = 0x0004
                SWP_FRAMECHANGED = 0x0020

                # Withdraw and redraw to force clean positioning
                if force_redraw:
                    self.withdraw()
                    self.update()

                # Use SetWindowPos for exact positioning (bypasses frame adjustments)
                ctypes.windll.user32.SetWindowPos(
                    self._hwnd,
                    None,
                    x,
                    y,
                    width,
                    height,
                    SWP_NOZORDER | SWP_FRAMECHANGED,
                )

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

        # Keep window on top when not full-screen (otherwise it disappears behind other windows)
        if self._window_height_percent < 100:
            self.wm_attributes("-topmost", True)
        else:
            # Full-screen: only use topmost if transparent mode is active
            if not (
                self._subtitle_mode == SUBTITLE_MODE_STATIC and self._transparent_static
            ):
                self.wm_attributes("-topmost", False)

    def set_monitor(self, monitor_index: int):
        """Change the monitor where the subtitle window is displayed."""
        self._monitor_index = monitor_index

        # Reposition window to the new monitor
        self._set_screen_position()
        self.update_idletasks()
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()
        self._update_font()
        self._reposition_subtitles()
        self._render_live_line()

    def set_window_height_percent(self, percent: int):
        """Set window height as percentage of screen height (5-100)."""
        self._window_height_percent = max(5, min(100, percent))
        # Use force_redraw to prevent visual glitches during resize
        self._set_screen_position(force_redraw=True)
        self.canvas_width = self.canvas.winfo_width()
        self.canvas_height = self.canvas.winfo_height()
        self._update_font()
        self._reposition_subtitles()
        self._render_live_line()
        # Bring window to front after resize
        self.lift()

    def add_subtitle(self, text: str, source_text: str | None = None):
        """Render a subtitle; in bilingual mode the original transcription is
        shown above the translation in a smaller, muted font."""
        if not (text or "").strip():
            return

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
            text_id, line_items = self._create_outlined_text(
                self.canvas_width / 2,
                self.canvas_height - 4,
                text=text,
            )
            source_items = None
            if source:
                top_bbox = self.canvas.bbox(line_items[0][0])
                source_y = (
                    top_bbox[1] if top_bbox else self.canvas_height - 80
                ) - self._line_gap
                _, source_items = self._create_outlined_text(
                    self.canvas_width / 2,
                    source_y,
                    text=source,
                    font=self.source_font,
                    fill=self._secondary_text,
                )
            text_height = self._measure_block_height(text_id, line_items, source_items)
            self.subtitle_stack.append(
                _SubtitleBlock(text_id, text_height, line_items, source_items)
            )
        else:
            # Use the same manual wrapping so Arabic is reshaped correctly
            # (Tkinter's built-in width= wrapping does not handle RTL/Arabic).
            max_width = self.canvas_width - 140
            lines = self._wrap_text_to_lines(text, max_width)
            shaped_text = "\n".join(lines)
            text_id = self.canvas.create_text(
                self.canvas_width / 2,
                self.canvas_height,
                text=shaped_text,
                fill=self._primary_text,
                font=self.font,
                anchor="s",
                justify="center",
            )
            source_items = None
            if source:
                source_lines = self._wrap_text_to_lines(
                    source, max_width, self.source_font
                )
                source_id = self.canvas.create_text(
                    self.canvas_width / 2,
                    self.canvas_height,
                    text="\n".join(source_lines),
                    fill=self._secondary_text,
                    font=self.source_font,
                    anchor="s",
                    justify="center",
                )
                source_items = [(source_id, None)]
                self._position_source_above(text_id, source_items)
            text_height = self._measure_block_height(text_id, None, source_items)
            self.subtitle_stack.append(
                _SubtitleBlock(text_id, text_height, None, source_items)
            )

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
        self._animate_continuous_scroll()

    def _animate_continuous_scroll(self):
        """Animation frame for continuous scroll mode."""
        if self._subtitle_mode != SUBTITLE_MODE_CONTINUOUS:
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

        for i in range(len(self.subtitle_stack) - 1, -1, -1):
            block = self.subtitle_stack[i]

            if block.line_items:
                font_obj = tkfont.Font(font=self.font)
                line_height = font_obj.metrics("linespace")
                line_y = current_y

                for line_text_id, box_id in reversed(block.line_items):
                    self.canvas.coords(line_text_id, self.canvas_width / 2, line_y)

                    if box_id:
                        bbox = self.canvas.bbox(line_text_id)
                        if bbox:
                            self._update_line_background(box_id, bbox)

                    if i == len(self.subtitle_stack) - 1:
                        self.canvas.itemconfig(line_text_id, fill=self._primary_text)
                    else:
                        self.canvas.itemconfig(line_text_id, fill=self._secondary_text)

                    line_y -= line_height + self._line_gap

                if block.source_items:
                    # Source line cards continue upward above the translation
                    source_font_obj = tkfont.Font(font=self.source_font)
                    source_line_height = source_font_obj.metrics("linespace")
                    for source_id, box_id in reversed(block.source_items):
                        self.canvas.coords(source_id, self.canvas_width / 2, line_y)
                        if box_id:
                            bbox = self.canvas.bbox(source_id)
                            if bbox:
                                self._update_line_background(box_id, bbox)
                        self.canvas.itemconfig(source_id, fill=self._secondary_text)
                        line_y -= source_line_height + self._line_gap
            else:
                self.canvas.coords(block.text_id, self.canvas_width / 2, current_y)
                self._position_source_above(block.text_id, block.source_items)

                if i == len(self.subtitle_stack) - 1:
                    self.canvas.itemconfig(block.text_id, fill=self._primary_text)
                else:
                    self.canvas.itemconfig(block.text_id, fill=self._secondary_text)

            current_y -= block.height + self.line_spacing

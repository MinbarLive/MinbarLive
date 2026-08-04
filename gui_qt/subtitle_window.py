"""Subtitle overlay (Qt) — the audience-facing window.

Port of ``gui/subtitle_window.py``. The behaviour is intended to match; what
changes is how much machinery it takes, and three whole classes of bug that
stop being possible:

* **No text shaping layer.** ``_reshape_rtl`` and its ``_TK_HANDLES_ARABIC`` /
  ``_TK_SHAPES_ARABIC`` platform branches are gone: logical text goes straight
  to Qt, which shapes and bidi-orders it with HarfBuzz everywhere.
* **No manual line wrapping.** The Tk version wrapped text itself, and wrapping
  *shaped* text is what put the end of an RTL sentence on the first line. Qt
  still breaks every line here — ``QTextLayout`` wraps after shaping — this
  module only chooses where each finished line SITS.

Ink measurement, on the other hand, had to come back. An earlier pass dropped
``_stack_overlap`` on the grounds that ``QFontMetrics.lineSpacing()`` is a real
baseline rhythm and therefore the correct model. It is a real rhythm, but a
looser one than the overlay had: Segoe UI's line spacing is ~1.33 em, of which
~0.29 em is blank band above the cap height, and against the Tk overlay
side by side the Qt one read as double-spaced (wrapped lines, bilingual pairs
and the gaps between blocks alike). ``_reclaim`` closes that band back up, on
the same measured terms Tk used — see it for why the figure is per-script.

Transparency is genuine per-pixel alpha, not the ``-transparentcolor`` green
chroma key, so anti-aliased glyph edges no longer fringe over video and no
colour is forbidden in subtitle text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QWidget

from config import (
    REALTIME_BLOCK_SPACING,
    REALTIME_LIVE_MAX_ROWS,
    REALTIME_MAX_BLOCK_CHARS,
)
from gui_qt.fonts import source_font, subtitle_font
from gui_qt.palette import palette
from gui_qt.widgets import is_window_on_top, set_window_on_top
from utils.settings import (
    BACKDROP_OPACITY_MAX,
    BACKDROP_OPACITY_MIN,
    DEFAULT_BACKDROP_OPACITY,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
)

# Gap between the source line and its translation inside one block, ON TOP of
# whatever _reclaim closes up. They are one utterance and have to read as one:
# in the Tk overlay the Arabic sits almost on its translation, and anything
# more here separated them into two unrelated lines.
PAIR_GAP = 2

# Ink that must remain between two stacked lines, so they never actually touch.
# Tuned against the Tk overlay side by side: it is the only thing standing
# between "one utterance" and "two lines that happen to be near each other".
_STACK_INK_GAP_EM = 0.04

# Honorific ligatures (ﷺ, ﷻ). Excluded when measuring how tall a line's ink is:
# they are far taller than the script around them, and one of them in a line of
# German would otherwise widen the gap above the WHOLE line — the honorific
# would visibly push its own paragraph away from the one above it. Letting the
# ligature sit a little closer is the lesser evil, and it is the same trade the
# Tk overlay makes (gui/subtitle_window.py _HONORIFIC_LIGATURE_RE).
_HONORIFIC_LIGATURE_RE = re.compile(r"[ﷺﷻ]")
# Distance from the window's bottom edge to the footer pill, and between pills.
FOOTER_MARGIN = 18
# Pill text size. Fixed, never derived from the subtitle size: the Tk overlay
# draws its footer at a constant 14pt bold, which is ~19 logical px at 96 DPI.
PILL_FONT_PX = 19
PILL_GAP = 12
# Side margin as a fraction of window width, so a line never runs edge to edge.
SIDE_MARGIN_RATIO = 0.06
# Continuous mode advances by this many pixels per frame at speed 1.0.
SCROLL_PIXELS_PER_FRAME = 1.0
FRAME_MS = 16

# Realtime feed: a new block moves the feed's TARGET, and the visible offset
# eases toward it instead of jumping a whole block's height the instant the
# translation lands. The Tk overlay's figures (LIVE_FEED_ANIM_* in
# gui/subtitle_window.py), which were tuned on real sessions.
FEED_ANIM_FRAME_MS = 30
FEED_ANIM_EASE = 0.3  # fraction of the remaining gap closed per frame
FEED_ANIM_MIN_STEP = 2.0  # px/frame floor, or the tail of a slide crawls
FEED_ANIM_SNAP_PX = 1.0  # within this, land on the target and stop


@dataclass
class Block:
    """One settled utterance: a translation, optionally with its original."""

    translation: str
    source: str | None = None
    # Absolute top edge in content coordinates. Continuous mode only: assigned
    # when the block is added and decremented every frame as it scrolls up.
    # Realtime and static lay out from scratch each paint and ignore it.
    y: float = field(default=0.0, repr=False)


class SubtitleWindow(QWidget):
    """Frameless, always-on-top, per-pixel-alpha subtitle overlay."""

    def __init__(
        self,
        on_close=None,
        monitor_index: int = 1,
        font_size_base: int = 40,
        source_font_size_base: float = 40 / 0.7,
        translation_text_color: str = "",
        source_text_color: str = "",
        target_language: str = "German",
        subtitle_mode: str = SUBTITLE_MODE_STATIC,
        scroll_speed: float = 1.0,
        transparent_static: bool = False,
        window_height_percent: int = 100,
        backdrop_opacity: int = DEFAULT_BACKDROP_OPACITY,
        show_footer: bool = True,
        theme_mode: str = "dark",
        bilingual_mode: bool = False,
        always_on_top: bool = True,
        adaptive_catchup: bool = False,
        on_stop=None,
    ):
        super().__init__()
        self._on_close = on_close
        self._on_stop = on_stop
        self._monitor_index = monitor_index
        self._target_language = target_language
        self._mode = subtitle_mode
        self._scroll_speed = scroll_speed
        self._transparent_static = transparent_static
        self._height_percent = window_height_percent
        self._backdrop_opacity = backdrop_opacity
        self._show_footer = show_footer
        self._theme_mode = theme_mode
        self._bilingual = bilingual_mode
        self._font_size_base = font_size_base
        self._source_font_size_base = source_font_size_base
        self._translation_color = translation_text_color
        self._source_color = source_text_color

        self._blocks: list[Block] = []
        self._live_text: str | None = None
        self._live_settled = False
        self._announcement: str | None = None
        self._stopped_hint = False
        self._scroll_offset = 0.0
        # Where the feed is easing TO (see _step_feed_anim). Kept apart
        # from _scroll_offset so a new block can move the destination
        # without moving what is on screen this frame.
        self._feed_target = 0.0
        self._adaptive_catchup = adaptive_catchup
        self._effective_scroll_speed = scroll_speed

        # A REAL window, not a Qt.Tool: a tool window is kept out of the
        # taskbar and the alt-tab list, and out of OBS's window-capture list
        # with it. The Tk overlay goes to some length for the same thing
        # (stripping the caption by hand but forcing WS_EX_APPWINDOW), because
        # window capture is how most operators put subtitles into a stream.
        # It still never takes focus, and that is deliberately done with
        # WA_ShowWithoutActivating rather than Qt.WindowDoesNotAcceptFocus:
        # the latter sets WS_EX_NOACTIVATE, and Windows documents such a window
        # as staying OFF the taskbar unless WS_EX_APPWINDOW is forced on too —
        # which is exactly the flag the Tk overlay sets by hand. Whether the
        # button appeared was then down to when the shell looked, so it came
        # and went between runs. WA_ShowWithoutActivating gives the half that
        # matters (showing the overlay never pulls focus off the panel) with no
        # taskbar side effect, and the window is transparent to the mouse
        # anyway, so it cannot be clicked into focus either.
        self.setWindowFlag(Qt.Window, True)
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # The title OBS lists it under; kept identical to the Tk overlay's so
        # an existing capture source keeps matching.
        self.setWindowTitle("MinbarLive Subtitles")
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.set_always_on_top(always_on_top)

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setInterval(FRAME_MS)
        self._scroll_timer.timeout.connect(self._advance_scroll)
        self._feed_timer = QTimer(self)
        self._feed_timer.setInterval(FEED_ANIM_FRAME_MS)
        self._feed_timer.timeout.connect(self._step_feed_anim)

        self._apply_geometry()
        self._sync_scroll_timer()

    # ── colours ──────────────────────────────────────────────────────────
    @property
    def _colors(self) -> dict[str, str]:
        return palette(self._theme_mode)

    def _translation_qcolor(self) -> QColor:
        return QColor(self._translation_color or self._colors["text"])

    def _source_qcolor(self) -> QColor:
        return QColor(self._source_color or self._colors["muted"])

    def _history_qcolor(self) -> QColor:
        """Translation colour for a block that is no longer the newest.

        Deliberately the theme's muted tone and never the configured SOURCE
        colour, even though they default to the same value: the dim means
        "already said", not "this is the original", and a custom source colour
        must not bleed into it (the rule gui/subtitle_window.py states at
        _render_feed_positions).
        """
        return QColor(self._colors["muted"])

    def _transparent_static_active(self) -> bool:
        """Transparent backdrop is a static-mode option only."""
        return self._mode == SUBTITLE_MODE_STATIC and self._transparent_static

    def _backdrop(self) -> QColor:
        """Window backdrop, drawn behind everything else."""
        if self._transparent_static_active():
            # Fully transparent: contrast comes from per-line cards instead, so
            # the text stays readable over arbitrary video.
            return QColor(0, 0, 0, 0)
        base = QColor(self._colors["app_bg"])
        base.setAlpha(round(self._backdrop_opacity * 255 / 100))
        return base

    # ── geometry ─────────────────────────────────────────────────────────
    def _screen(self):
        screens = QGuiApplication.screens()
        if not screens:
            return None
        idx = max(0, min(self._monitor_index, len(screens) - 1))
        return screens[idx]

    def _apply_geometry(self) -> None:
        """Occupy the bottom ``height_percent`` of the chosen screen.

        Which bottom depends on the stacking: a topmost window paints OVER the
        Windows taskbar, so it can have the whole screen. A window that is not
        topmost is painted over BY the taskbar, which would swallow the
        disclaimer pill and the last line of every subtitle — so it is laid out
        inside the work area instead, ending above the taskbar. Same reasoning
        the Tk overlay applies to the macOS Dock, which is always above it.

        Qt reports both rectangles in logical units already, so there is no DPI
        arithmetic here — that is what gui/scaling.py does by hand.
        """
        screen = self._screen()
        if screen is None:
            return
        g = screen.geometry() if is_window_on_top(self) else screen.availableGeometry()
        h = max(1, int(g.height() * self._height_percent / 100))
        self.setGeometry(QRect(g.x(), g.y() + g.height() - h, g.width(), h))

    # ── layout helpers ───────────────────────────────────────────────────
    def _content_width(self) -> int:
        return max(1, int(self.width() * (1 - 2 * SIDE_MARGIN_RATIO)))

    # ``font_size_base`` and ``source_font_size_base`` are DIVISORS, not pixel
    # sizes: the rendered size is the window width divided by the base, so text
    # keeps its proportion on any monitor. Smaller base => larger text.
    def _translation_px(self) -> int:
        if not self.width():
            return 24
        return max(12, min(120, int(self.width() / self._font_size_base)))

    def _source_px(self) -> int:
        if not self.width():
            return 17
        return max(12, min(120, int(self.width() / self._source_font_size_base)))

    @staticmethod
    def _ink(text: str, font: QFont) -> tuple[int, int]:
        """``(reclaim, overhang)`` for one line of ``text``.

        ``reclaim`` is the blank band above the ink that stacking may close
        up. A font box reserves the whole ascent whether the glyphs use it or
        not, and that leftover is what makes metric-spaced lines read as
        double-spaced. Reclaiming it pulls the lines visually together while
        leaving ``_STACK_INK_GAP_EM`` of clearance, so the ink never touches.

        ``overhang`` is the opposite problem at the other end: ink that reaches
        BELOW the font's descent, which the box does not account for. Arabic
        descends deeper than a Latin-derived descent metric allows for, and
        deeper still when the glyphs come from a fallback family — Qt reports
        the metrics of the family it was asked for and paints from whichever
        one has the glyph. That is what made an Arabic original overlap its
        German translation on Linux.

        Both MEASURED from the string, where the Tk overlay had to guess. It
        approximated the first with a per-script table (``_INK_TOP_EM_ARABIC``
        and friends) because a Tk canvas bbox reports metrics, not ink, and the
        figures had to be re-derived per font family — they broke on Linux.
        ``tightBoundingRect`` is the real ink of the real glyphs, so Arabic,
        Latin, diacritics and any script added later are simply correct, on
        every platform, with nothing to keep in step.
        """
        # Honorifics excluded: see _HONORIFIC_LIGATURE_RE. Their own line still
        # gets a sensible gap from whatever else is in it.
        measured = _HONORIFIC_LIGATURE_RE.sub("", text).strip()
        if not measured:
            return 0, 0
        metrics = QFontMetrics(font)
        ink = metrics.tightBoundingRect(measured)
        if ink.isEmpty():
            return 0, 0
        # ink.top() is negative above the baseline, so this is the distance
        # from the box's ascent line down to the tallest ink in the line.
        leading = metrics.ascent() + ink.top()
        em = font.pixelSize() or font.pointSize()
        # No clearance term on the overhang: the line BELOW already holds
        # _STACK_INK_GAP_EM back through its own reclaim, and adding it at both
        # ends would double the gap this whole mechanism exists to close.
        return (
            max(0, round(leading - _STACK_INK_GAP_EM * em)),
            max(0, ink.bottom() - metrics.descent()),
        )

    @classmethod
    def _reclaim(cls, text: str, font: QFont) -> int:
        """Blank band above ``text``'s ink that stacking may close up."""
        return cls._ink(text, font)[0]

    def _layout_text(self, text: str, font: QFont) -> tuple[QTextLayout, int]:
        """``text`` wrapped to the content width, at a tightened line rhythm.

        Qt does the line breaking — after shaping and bidi, so an RTL sentence
        cannot be broken in the wrong place — and this only sets where each
        finished line sits: one ``_reclaim`` closer than the font's own line
        spacing would put it.

        Returns the laid-out object (draw it with ``QTextLayout.draw``) and the
        height it occupies, so measuring and drawing can never disagree.
        """
        layout = QTextLayout(text, font)
        option = QTextOption(Qt.AlignHCenter)
        option.setWrapMode(QTextOption.WordWrap)
        # QTextOption defaults to LEFT-TO-RIGHT, not to "work it out" — so an
        # Arabic paragraph was laid out with an LTR base direction and its
        # full stop, a neutral character, attached to the LTR paragraph and
        # landed on the RIGHT. The words themselves still ran right-to-left
        # (that is the bidi algorithm inside the paragraph), which is why it
        # looked almost correct. LayoutDirectionAuto applies the Unicode
        # first-strong-character rule instead: Arabic text becomes an RTL
        # paragraph and its terminator sits at the left, where the sentence
        # ends; German stays LTR. Verified over both, including lines opening
        # with a quote, a digit or the elision ellipsis.
        option.setTextDirection(Qt.LayoutDirectionAuto)
        layout.setTextOption(option)

        fm = QFontMetrics(font)
        reclaim, overhang = self._ink(text, font)
        # ``overhang`` also has to widen the pitch, not just the block's foot:
        # two wrapped lines of the same Arabic sentence stack at this distance
        # and would collide with each other otherwise.
        pitch = max(1, fm.lineSpacing() - reclaim + overhang)
        width = self._content_width()
        count, y = 0, 0.0
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0, y))
            y += pitch
            count += 1
        layout.endLayout()
        if not count:
            return layout, 0
        # The last line keeps its full box: baseline distances have to stay
        # constant for the rhythm to READ as even, so only the space ABOVE a
        # line is ever reclaimed, never its descent — plus whatever ink hangs
        # below that descent, or the next block starts inside this one.
        return layout, (count - 1) * pitch + fm.ascent() + fm.descent() + overhang

    def _measure(self, text: str, font: QFont) -> int:
        """Height ``text`` occupies at ``font`` within the content width."""
        return self._layout_text(text, font)[1]

    def _block_fonts(self, block: Block) -> tuple[QFont, QFont | None]:
        trans = subtitle_font(self._translation_px(), text=block.translation)
        src = None
        if self._bilingual and block.source:
            src = source_font(self._source_px(), block.source)
        return trans, src

    def _pair_gap(self, block: Block) -> int:
        """Space between a block's source line and its translation.

        Reclaimed like any other stacking boundary — otherwise the pair sits a
        whole blank band further apart than PAIR_GAP claims, which is what made
        a bilingual block read as two unrelated lines.
        """
        trans_font, _src = self._block_fonts(block)
        # Deliberately allowed to go NEGATIVE: the reclaim is normally larger
        # than PAIR_GAP, and clamping at zero threw the remainder away and left
        # the pair as far apart as before. The boxes overlap; the INK cannot,
        # because _reclaim already holds _STACK_INK_GAP_EM back. Tk stacks the
        # same way (_stack_rows_tight positions at bbox top + overlap).
        return PAIR_GAP - self._reclaim(block.translation, trans_font)

    def _measure_block(self, block: Block) -> int:
        trans_font, src_font = self._block_fonts(block)
        h = self._measure(block.translation, trans_font)
        if src_font is not None and block.source:
            h += self._measure(block.source, src_font) + self._pair_gap(block)
        return h

    def _block_gap(self, block: Block) -> int:
        """Space above ``block`` when it follows another.

        REALTIME_BLOCK_SPACING is shared with the Tk overlay, which positions
        ink-aware and so gets the gap the number describes. Qt stacks metric
        boxes, so the same constant has to give back the incoming block's blank
        band to land in the same place.
        """
        trans_font, src_font = self._block_fonts(block)
        if src_font is not None and block.source:
            first_text, first_font = block.source, src_font
        else:
            first_text, first_font = block.translation, trans_font
        # Not clamped at zero, for the reason given in _pair_gap — though a
        # block usually opens on an Arabic source line, whose ink reaches the
        # ascent and reclaims nothing, so this mostly IS the constant.
        return REALTIME_BLOCK_SPACING - self._reclaim(first_text, first_font)

    def _draw_card(self, p: QPainter, text: str, font: QFont, rect: QRect) -> None:
        """Rounded backing card behind one line, sized to the text it holds.

        Only used when the backdrop is transparent: without it the subtitle
        would have to compete with whatever video is underneath.
        """
        fm = QFontMetrics(font)
        tw = min(fm.horizontalAdvance(text), rect.width())
        pad_x, pad_y = 20, 8
        cw = min(rect.width(), tw + pad_x * 2)
        cx = rect.x() + (rect.width() - cw) // 2
        path = QPainterPath()
        path.addRoundedRect(cx, rect.y() - pad_y, cw, rect.height() + pad_y * 2, 14, 14)
        p.fillPath(path, QColor(0, 0, 0, 150))

    def _draw_block(
        self, p: QPainter, block: Block, x: int, y: int, newest: bool = True
    ) -> int:
        """Draw ``block`` with its top edge at ``y``; return the height used.

        ``newest`` carries the full translation colour; everything above it is
        history and drops to the muted tone, so the eye lands on the line
        being spoken now without having to search for it.
        """
        trans_font, src_font = self._block_fonts(block)
        w = self._content_width()
        cards = self._transparent_static_active()
        used = 0
        if src_font is not None and block.source:
            layout, sh = self._layout_text(block.source, src_font)
            if cards:
                self._draw_card(p, block.source, src_font, QRect(x, y, w, sh))
            p.setPen(self._source_qcolor())
            layout.draw(p, QPointF(x, y))
            used += sh + self._pair_gap(block)
        layout, th = self._layout_text(block.translation, trans_font)
        if cards:
            self._draw_card(p, block.translation, trans_font, QRect(x, y + used, w, th))
        p.setPen(self._translation_qcolor() if newest else self._history_qcolor())
        layout.draw(p, QPointF(x, y + used))
        return used + th

    # ── painting ─────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), self._backdrop())

        if self._announcement:
            self._paint_announcement(p)
        elif self._mode == SUBTITLE_MODE_REALTIME:
            self._paint_realtime(p)
        elif self._mode == SUBTITLE_MODE_CONTINUOUS:
            self._paint_continuous(p)
        else:
            self._paint_static(p)

        # Pills paint last so they always sit above subtitle text. In the Tk
        # version z-order followed canvas item creation order, so every new
        # item had to be followed by an explicit _raise_footer() call.
        self._paint_pills(p)

    def _paint_realtime(self, p: QPainter) -> None:
        """Top-down feed: settled blocks stack from the top, live line below.

        Once the feed reaches the bottom it shifts up chat-style so the newest
        line stays visible. The shift only ever grows — content must never
        slide back down, which would read as the text jumping around.
        """
        x = int(self.width() * SIDE_MARGIN_RATIO)
        top = int(self.height() * 0.06)
        heights = [self._measure_block(b) for b in self._blocks]
        # The advance past each block: its own height plus the gap the NEXT one
        # wants above it. One list, used by all three passes below, so the
        # total, the draw loop and the eviction compensation cannot disagree —
        # if they did, the feed would jump every time a block scrolled off.
        # ``followers`` is empty when there are no blocks — [*[], None] would be
        # a one-element list against zero heights.
        followers = [*self._blocks[1:], None] if self._blocks else []
        advances = [
            h + (self._block_gap(nxt) if nxt is not None else REALTIME_BLOCK_SPACING)
            for h, nxt in zip(heights, followers, strict=True)
        ]

        total = sum(advances)
        if self._live_text:
            total += self._live_line_height()
        overflow = top + total - self._content_height()
        if overflow > self._feed_target:
            # The TARGET moves at once; the rendered offset eases toward it in
            # _step_feed_anim, so the feed slides up the way the Tk overlay
            # does instead of teleporting a whole block's height on arrival.
            self._feed_target = overflow
            self._start_feed_anim()

        y = top - self._scroll_offset
        evicted = 0
        newest = len(self._blocks) - 1
        for i, (block, h, advance) in enumerate(
            zip(self._blocks, heights, advances, strict=True)
        ):
            if y + h < 0:  # scrolled off the top edge
                evicted += 1
            else:
                self._draw_block(p, block, x, int(y), newest=i == newest)
            y += advance
        if self._live_text:
            self._draw_live_line(p, x, int(y))

        if evicted:
            # Drop what is off-screen and shorten the shift by the same extent,
            # so the remaining blocks do not jump (eviction-compensated).
            drop = sum(advances[:evicted])
            del self._blocks[:evicted]
            self._scroll_offset -= drop
            self._feed_target -= drop

    def _place_continuous(self, block: Block) -> None:
        """Assign a new block's absolute y so it is visible straight away.

        Anchored so its bottom sits at the bottom of the content area — a
        subtitle must appear the moment it arrives, not after scrolling up
        into view. It is pushed lower only when the previous block still
        occupies that space, which is what creates a genuine backlog for
        ``get_subtitle_backlog_count`` and adaptive catch-up to drain.
        """
        anchor = self._content_height() - self._measure_block(block)
        if self._blocks:
            last = self._blocks[-1]
            stacked = last.y + self._measure_block(last) + self._block_gap(block)
            block.y = max(anchor, stacked)
        else:
            block.y = anchor

    def _paint_continuous(self, p: QPainter) -> None:
        """Steady upward scroll; new text appears at the bottom."""
        x = int(self.width() * SIDE_MARGIN_RATIO)
        limit = self._content_height()
        survivors: list[Block] = []
        newest = len(self._blocks) - 1
        for i, block in enumerate(self._blocks):
            h = self._measure_block(block)
            if block.y + h < 0:  # scrolled fully past the top edge
                continue
            survivors.append(block)
            if block.y <= limit:  # otherwise still queued below the viewport
                self._draw_block(p, block, x, int(block.y), newest=i == newest)
        # Each block carries its own absolute y, so dropping off-screen blocks
        # cannot shift the survivors — no offset compensation needed.
        self._blocks = survivors

    def _paint_static(self, p: QPainter) -> None:
        """Only the newest block, vertically centred."""
        if not self._blocks:
            return
        block = self._blocks[-1]
        x = int(self.width() * SIDE_MARGIN_RATIO)
        h = self._measure_block(block)
        self._draw_block(p, block, x, max(0, (self._content_height() - h) // 2))

    def _live_line_height(self) -> int:
        font = source_font(self._translation_px(), self._live_text or "")
        return QFontMetrics(font).lineSpacing() * REALTIME_LIVE_MAX_ROWS

    def _draw_live_line(self, p: QPainter, x: int, y: int) -> None:
        """In-progress transcript: muted while speaking, primary once settled."""
        text = self._live_text or ""
        font = source_font(self._font_size_base, text)
        fm = QFontMetrics(font)
        # Show only the newest row: a long interim that wrapped would otherwise
        # shove the settled history up by several rows at once.
        max_h = fm.lineSpacing() * REALTIME_LIVE_MAX_ROWS
        p.setFont(font)
        p.setPen(self._translation_qcolor() if self._live_settled else self._source_qcolor())
        p.drawText(
            QRect(x, y, self._content_width(), max_h),
            int(Qt.AlignHCenter | Qt.AlignTop),
            fm.elidedText(text, Qt.ElideLeft, self._content_width()),
        )

    def _pill_font(self) -> QFont:
        """Fixed size and bold, exactly as the Tk overlay draws it.

        The pills are a disclaimer and a status note, not subtitle content, so
        they must not grow with the subtitle font — at a large size the
        disclaimer took a third of the overlay.
        """
        return subtitle_font(PILL_FONT_PX, bold=True)

    def _pill_height(self) -> int:
        return QFontMetrics(self._pill_font()).height() + 16

    def _footer_text(self) -> str:
        from gui_qt.subtitle_text import DEFAULT_FOOTER, FOOTER_TRANSLATIONS

        return FOOTER_TRANSLATIONS.get(self._target_language, DEFAULT_FOOTER)

    def _stopped_text(self) -> str:
        from utils.user_messages import get_user_message

        # Reads the target language from settings itself, so it re-localises
        # whenever the pill is repainted.
        return get_user_message("app_stopped")

    def reserved_bottom(self) -> int:
        """Height at the bottom the pills occupy — subtitles must stay above it.

        Computed before any content is laid out, so a block can never be drawn
        underneath the disclaimer.
        """
        r = 0
        if self._show_footer:
            r += self._pill_height() + FOOTER_MARGIN
        if self._stopped_hint:
            r += self._pill_height() + PILL_GAP
        return r

    def _content_height(self) -> int:
        return max(1, self.height() - self.reserved_bottom())

    def _pill(
        self,
        p: QPainter,
        text: str,
        bottom: int,
        fill: QColor,
        fg: QColor,
        *,
        pause_icon: bool = False,
    ) -> int:
        """Draw a centred rounded pill with its bottom edge at ``bottom``.

        ``pause_icon`` prefixes two bars. They are DRAWN, not typed: every
        media-control code point (U+23F8, U+275A, …) either has no glyph or
        carries side bearings so wide that the pair reads as two loose blocks
        — the same conclusion the Tk overlay reached.
        """
        font = self._pill_font()
        fm = QFontMetrics(font)
        pad_x = 18
        line_h = fm.height()
        bar_w = max(2, round(line_h * 0.17))
        bar_h = round(line_h * 0.66)
        bar_gap = max(2, round(bar_w * 0.75))
        icon_w = (bar_w * 2 + bar_gap) if pause_icon else 0
        icon_gap = round(pad_x * 0.5) if pause_icon else 0
        w = fm.horizontalAdvance(text) + icon_w + icon_gap + pad_x * 2
        h = self._pill_height()
        x = (self.width() - w) // 2
        y = bottom - h
        path = QPainterPath()
        path.addRoundedRect(x, y, w, h, h / 2, h / 2)
        p.fillPath(path, fill)
        if pause_icon:
            # A pause mark is symmetric, so it needs no mirroring for RTL.
            mid_y = y + h / 2
            for i in range(2):
                bar = QPainterPath()
                bar.addRoundedRect(
                    x + pad_x + i * (bar_w + bar_gap),
                    mid_y - bar_h / 2,
                    bar_w,
                    bar_h,
                    bar_w * 0.35,
                    bar_w * 0.35,
                )
                p.fillPath(bar, fg)
        p.setFont(font)
        p.setPen(fg)
        p.drawText(
            QRect(x + pad_x + icon_w + icon_gap, y, w - pad_x * 2 - icon_w - icon_gap, h),
            int(Qt.AlignVCenter | Qt.AlignLeft) if pause_icon else int(Qt.AlignCenter),
            text,
        )
        return y

    def _paint_pills(self, p: QPainter) -> None:
        """Footer last-but-one, stopped hint stacked directly above it."""
        bottom = self.height() - FOOTER_MARGIN
        if self._show_footer:
            bottom = self._pill(
                p,
                self._footer_text(),
                bottom,
                QColor(self._colors["warning"]),
                QColor("#111827"),
            ) - PILL_GAP
        if self._stopped_hint:
            c = self._colors
            self._pill(
                p,
                self._stopped_text(),
                bottom,
                QColor(c["card"]),
                QColor(c["muted"]),
                pause_icon=True,
            )

    def _paint_announcement(self, p: QPainter) -> None:
        block = Block(self._announcement or "")
        x = int(self.width() * SIDE_MARGIN_RATIO)
        h = self._measure_block(block)
        self._draw_block(p, block, x, max(0, (self._content_height() - h) // 2))

    # ── scrolling ────────────────────────────────────────────────────────
    def _sync_scroll_timer(self) -> None:
        if self._mode == SUBTITLE_MODE_CONTINUOUS:
            self._scroll_timer.start()
        else:
            self._scroll_timer.stop()

    # ── realtime feed animation ──────────────────────────────────────────
    def _start_feed_anim(self) -> None:
        """Ease the feed up to its target rather than jumping there.

        Only the top-down feed animates. Continuous mode has its own steady
        scroll and static mode replaces its block outright, so neither has a
        gap to close.
        """
        if self._mode != SUBTITLE_MODE_REALTIME or self._stopped_hint:
            return
        if self._feed_target - self._scroll_offset < FEED_ANIM_SNAP_PX:
            return
        if not self._feed_timer.isActive():
            self._feed_timer.start()

    def _step_feed_anim(self) -> None:
        """One eased frame toward the target, then repaint."""
        gap = self._feed_target - self._scroll_offset
        if gap < FEED_ANIM_SNAP_PX:
            # Close the last sub-pixel outright: easing toward it forever
            # would repaint every frame for movement nobody can see.
            self._scroll_offset = self._feed_target
            self._feed_timer.stop()
            self.update()
            return
        # A floor under the eased step, or the tail of a long slide crawls.
        self._scroll_offset = min(
            self._scroll_offset + max(gap * FEED_ANIM_EASE, FEED_ANIM_MIN_STEP),
            self._feed_target,
        )
        self.update()

    def get_subtitle_backlog_count(self) -> int:
        """How many blocks are queued below the visible anchor line.

        Continuous mode only — the other modes do not queue.
        """
        if self._mode != SUBTITLE_MODE_CONTINUOUS:
            return 0
        limit = self._content_height()
        return sum(1 for b in self._blocks if b.y > limit)

    def _current_scroll_speed(self) -> float:
        """Smoothed scroll speed, with optional readability-first catch-up.

        Accelerates gently and caps at 2x so a backlog is worked off without
        the text becoming unreadable, then EMA-smoothed so the speed change
        itself is not visible as a jolt.
        """
        target = self._scroll_speed
        if self._adaptive_catchup and self._mode == SUBTITLE_MODE_CONTINUOUS:
            target *= min(2.0, 1.0 + 0.25 * self.get_subtitle_backlog_count())
        self._effective_scroll_speed = (
            0.85 * self._effective_scroll_speed + 0.15 * target
        )
        return self._effective_scroll_speed

    def _advance_scroll(self) -> None:
        step = SCROLL_PIXELS_PER_FRAME * self._current_scroll_speed()
        for block in self._blocks:
            block.y -= step
        self.update()

    # ── public API ───────────────────────────────────────────────────────
    def add_subtitle(self, text: str, source_text: str | None = None) -> None:
        """Append a settled utterance.

        Long realtime blocks are split at sentence boundaries so one 12 s
        utterance does not land as a wall of text. Display-only: no extra API
        calls, and a bilingual pair is never split (the halves have no
        per-sentence alignment).
        """
        if not text:
            return
        from gui_qt.subtitle_text import split_display_chunks

        if self._mode == SUBTITLE_MODE_REALTIME and not source_text:
            chunks = split_display_chunks(text, REALTIME_MAX_BLOCK_CHARS)
        else:
            chunks = [text]
        for i, chunk in enumerate(chunks):
            block = Block(chunk, source_text if i == 0 else None)
            if self._mode == SUBTITLE_MODE_CONTINUOUS:
                self._place_continuous(block)
            self._blocks.append(block)
        self.update()

    def set_live_text(self, text: str | None, settled: bool = False) -> None:
        self._live_text = text or None
        self._live_settled = settled
        self.update()

    def set_subtitle_mode(self, mode: str) -> None:
        self._mode = mode
        self._scroll_offset = self._feed_target = 0.0
        self._feed_timer.stop()
        if mode == SUBTITLE_MODE_CONTINUOUS:
            # Blocks carried over from another mode have no meaningful y yet:
            # re-stack them from the bottom so the newest stays visible.
            self._restack_continuous()
        self._sync_scroll_timer()
        self.update()

    def _restack_continuous(self) -> None:
        """Re-place every block bottom-up, newest anchored at the bottom edge."""
        y = self._content_height()
        for block in reversed(self._blocks):
            h = self._measure_block(block)
            y -= h
            block.y = y
            # The gap belongs to the block that follows it, so it is this
            # block's own — the same rule _paint_realtime stacks by.
            y -= self._block_gap(block)

    def get_subtitle_mode(self) -> str:
        return self._mode

    def set_bilingual_mode(self, enabled: bool) -> None:
        self._bilingual = enabled
        self.update()

    def set_theme(self, theme_mode: str) -> None:
        self._theme_mode = theme_mode
        self.update()

    def set_language(self, language: str) -> None:
        self._target_language = language
        self.update()

    def set_show_footer(self, enabled: bool) -> None:
        self._show_footer = enabled
        self.update()

    def set_stopped_hint(self, visible: bool) -> None:
        self._stopped_hint = visible
        self.update()

    def set_announcement(self, text: str) -> None:
        self._announcement = text
        self.update()

    def clear_announcement(self) -> None:
        self._announcement = None
        self.update()

    def set_monitor(self, monitor_index: int) -> None:
        self._monitor_index = monitor_index
        self._apply_geometry()
        self.update()

    def set_window_height_percent(self, percent: int) -> None:
        self._height_percent = percent
        self._apply_geometry()
        self.update()

    def set_always_on_top(self, enabled: bool) -> None:
        """Toggle the stays-on-top flag, and re-place the overlay for it.

        The flag goes through set_window_on_top, which does not recreate the
        native window — so the overlay neither vanishes nor flashes. The
        geometry still has to be recomputed: only a topmost window paints over
        the taskbar, so a non-topmost overlay is laid out above it instead.
        """
        if is_window_on_top(self) == enabled:
            return
        set_window_on_top(self, enabled)
        self._apply_geometry()

    def set_backdrop_opacity(self, percent: int) -> None:
        """Set backdrop opacity 0-100. 0 leaves the video fully visible.

        Only possible because Qt composites real per-pixel alpha; the Tk
        window could not do this outside static mode's chroma key.
        """
        self._backdrop_opacity = max(
            BACKDROP_OPACITY_MIN, min(BACKDROP_OPACITY_MAX, int(percent))
        )
        self.update()

    def get_backdrop_opacity(self) -> int:
        return self._backdrop_opacity

    def set_transparent_static(self, enabled: bool) -> None:
        self._transparent_static = enabled
        self.update()

    def set_translation_text_color(self, color: str | None) -> None:
        self._translation_color = color or ""
        self.update()

    def set_source_text_color(self, color: str | None) -> None:
        self._source_color = color or ""
        self.update()

    def get_translation_text_color(self) -> str:
        return self._translation_color or self._colors["text"]

    def get_source_text_color(self) -> str:
        return self._source_color or self._colors["muted"]

    # The base is a divisor, so "increase font" LOWERS it. Bounds match the Tk
    # version (20-80 for the translation, 20-120 for the original text).
    def increase_font(self) -> None:
        self._font_size_base = max(20, self._font_size_base - 5)
        self.update()

    def decrease_font(self) -> None:
        self._font_size_base = min(80, self._font_size_base + 5)
        self.update()

    def get_font_size_base(self) -> int:
        return self._font_size_base

    def get_current_font_size(self) -> int:
        return self._translation_px()

    def set_source_font_size_base(self, value: float) -> None:
        try:
            self._source_font_size_base = max(20.0, min(120.0, float(value)))
        except (TypeError, ValueError):
            return
        self.update()

    def increase_source_font(self) -> None:
        self.set_source_font_size_base(self._source_font_size_base - 5.0)

    def decrease_source_font(self) -> None:
        self.set_source_font_size_base(self._source_font_size_base + 5.0)

    def get_source_font_size_base(self) -> float:
        return self._source_font_size_base

    def get_current_source_font_size(self) -> int:
        return self._source_px()

    def set_scroll_speed(self, speed: float) -> None:
        """Set the scroll speed directly (the settings stepper drives this)."""
        self._scroll_speed = max(0.25, min(5.0, float(speed)))

    def increase_scroll_speed(self) -> float:
        self._scroll_speed = min(5.0, self._scroll_speed + 0.25)
        return self._scroll_speed

    def decrease_scroll_speed(self) -> float:
        self._scroll_speed = max(0.25, self._scroll_speed - 0.25)
        return self._scroll_speed

    def set_adaptive_catchup(self, enabled: bool) -> None:
        self._adaptive_catchup = enabled

    def clear(self) -> None:
        self._blocks.clear()
        self._live_text = None
        self._scroll_offset = self._feed_target = 0.0
        self._feed_timer.stop()
        self._effective_scroll_speed = self._scroll_speed
        self.update()

    def hide(self) -> None:
        """Hide the overlay and stop animating.

        The live line is dropped: no further interims will arrive to correct
        it, so leaving it on screen would strand a half-finished sentence.
        """
        self._scroll_timer.stop()
        self._live_text = None
        super().hide()

    def show(self) -> None:
        super().show()
        self._apply_geometry()
        self._sync_scroll_timer()

    def destroy(self) -> None:
        """Tear the overlay down. Named to match the Tk window's API."""
        self._scroll_timer.stop()
        self.close()
        self.deleteLater()

    def closeEvent(self, event) -> None:
        # Closing the overlay stops the session; it never quits the app. The Tk
        # version wired this to full shutdown, so an Alt+F4 on the OBS-visible
        # overlay took the whole app down (fixed in PR #24).
        if self._on_stop:
            self._on_stop()
        if self._on_close:
            self._on_close()
        super().closeEvent(event)

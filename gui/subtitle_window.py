"""Subtitle overlay — the audience-facing window.

Two things this module deliberately does NOT do, each of which was a whole
class of bug in the CustomTkinter overlay it replaced:

* **No text shaping layer.** Logical text goes straight to Qt, which shapes and
  bidi-orders it with HarfBuzz on every platform. Never reintroduce a
  reshaping call — ``arabic-reshaper``/``python-bidi`` and their per-platform
  branches were removed on purpose.
* **No manual line wrapping.** Wrapping *shaped* text is what used to put the
  end of an RTL sentence on the first line. ``QTextLayout`` breaks every line
  here, after shaping; this module only chooses where each finished line SITS.

Ink measurement, on the other hand, is necessary and was once removed by
mistake. An earlier pass dropped it on the grounds that
``QFontMetrics.lineSpacing()`` is a real baseline rhythm and therefore the
correct model. It is a real rhythm, but a looser one than an overlay wants:
Segoe UI's line spacing is ~1.33 em, of which ~0.29 em is blank band above the
cap height, and the result read as double-spaced — wrapped lines, bilingual
pairs and the gaps between blocks alike. ``_reclaim`` closes that band back up;
see it for why the figure is per-script.

Transparency is genuine per-pixel alpha, not a chroma key, so anti-aliased
glyph edges never fringe over video and no colour is forbidden in subtitle
text.
"""

from __future__ import annotations

import re
import sys
import unicodedata
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRect, Qt, QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QFontMetricsF,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QTextCharFormat,
    QTextLayout,
    QTextOption,
)
from PySide6.QtWidgets import QWidget

from config import (
    REALTIME_BLOCK_SPACING,
    REALTIME_LIVE_MAX_ROWS,
    REALTIME_MAX_BLOCK_CHARS,
)
from gui.fonts import is_arabic_text, source_font, subtitle_font
from gui.palette import palette
from gui.widgets import is_window_on_top, needs_remap, set_window_on_top
from utils.logging import log
from utils.settings import (
    BACKDROP_OPACITY_MAX,
    BACKDROP_OPACITY_MIN,
    DEFAULT_BACKDROP_OPACITY,
    STATIC_LIFT_PERCENT_MAX,
    STATIC_LIFT_PERCENT_MIN,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    WINDOW_HEIGHT_PERCENT_MIN,
)

# Gap between the source line and its translation inside one block, ON TOP of
# whatever _reclaim closes up. They are one utterance and have to read as one:
# in the Tk overlay the Arabic sits almost on its translation.
#
# Raised from 2 to 10 on 2026-08-07 at the maintainer's request, after seeing
# the per-line ribbon (_ribbon_rects) on a real screen: the backdrop made the
# join visible in a way bare text never did, and at 2 the two boxes read as one
# jammed-together slab. This is REAL ink distance — the reclaim and the
# descent slack are both subtracted from it — so it is the whole of the gap the
# eye actually sees, not a metrics figure.
PAIR_GAP = 10

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
# Clearance kept between the lowest subtitle line and the topmost pill. The Tk
# overlay reserves the same 8 px (_draw_canvas_footer). Without it the feed,
# which is anchored to the bottom of the content area, lands its last line
# directly on the disclaimer.
PILL_CLEARANCE = 8
# Pill text size. Fixed, never derived from the subtitle size: the Tk overlay
# draws its footer at a constant 14pt bold, which is ~19 logical px at 96 DPI.
PILL_FONT_PX = 19
PILL_GAP = 12
# Side margin as a fraction of window width, so a line never runs edge to edge.
SIDE_MARGIN_RATIO = 0.06
# Gutter between the two columns of the side-by-side layout, as a fraction of
# window width. Wider than PAIR_GAP by a lot and deliberately so: stacked, the
# original and its translation have to read as ONE utterance, and side by side
# they have to read as two columns. The same distance that binds them
# vertically would leave two scripts running into each other horizontally.
COLUMN_GAP_RATIO = 0.018
# The panel drawn behind each column. Two of them, the same size, always — they
# are what makes the layout read as two columns rather than two loose stacks of
# text. Deliberately NOT sized to their contents, the way the transparent-static
# cards are: a panel that changed shape with every utterance would be the
# opposite of a column.
#
# In this layout the panels ARE the backdrop — the window one is not painted at
# all (see _backdrop). So they reach much closer to the window edge than the
# text ever did: SIDE_MARGIN_RATIO keeps a LINE off the edge, and a panel that
# kept the same distance would read as a small box floating inside a big one.
COLUMN_PANEL_MARGIN_RATIO = 0.02
# Text inset inside a panel. The columns are measured to what is left, so this
# is the only thing standing between a line and the panel's edge.
COLUMN_PANEL_PAD_X = 30
COLUMN_PANEL_PAD_Y = 18
COLUMN_PANEL_RADIUS = 18
# The per-line backdrop of transparent static mode (see _ribbon_rects and
# _card_fill). It is a flat black or white rather than the theme's backdrop
# colour: the whole point of the mode is that there is no window backdrop, so
# this is the only thing between the text and arbitrary video, and only an
# extreme works over a bright frame as well as a dark one.
_CARD_PAD_X = 20
_CARD_PAD_Y = 8
_CARD_RADIUS = 14
# Fitting a static block into a band too short for it (see _static_fit_scale).
#
# The REAL floor is the 12 px clamp in _translation_px / _source_px — text
# nobody can read from the back of a hall is not an improvement on text that is
# cut off. This constant only stops the refinement burning layouts below the
# point where the clamp has taken over and further shrinking changes nothing.
# It must stay under that point: at 0.35 it stopped one step early and a block
# still ran 5 px past the bottom of a 5%-height overlay, where the clamped
# minimum would have fitted with room to spare.
_FIT_MIN_SCALE = 0.2
# Bisection steps between that floor and 1.0. Eight brings the interval under
# 0.4%, which is far finer than a whole pixel of font size, and the cost is one
# re-layout of the block per step — paid once per utterance, since static mode
# has no animation timer repainting behind it.
_FIT_SEARCH_STEPS = 8
# Where the realtime feed's first line sits, as a fraction of window height.
FEED_TOP_RATIO = 0.06
# Continuous mode advances by this many pixels per frame at speed 1.0.
SCROLL_PIXELS_PER_FRAME = 1.0
FRAME_MS = 16

# A window manager can grant a different rectangle than the one asked for. An
# X11 WM may honour the requested SIZE but not the requested POSITION — GNOME
# keeps a frameless window clear of its top bar and dock — and a full-monitor
# overlay pushed down by that much loses its bottom strip, which is where the
# disclaimer pill lives, off the bottom of the screen. What was granted is
# only knowable once the WM has replied, so it is read back shortly after each
# placement rather than in _apply_geometry, exactly as the Tk overlay does it
# (gui/subtitle_window.py _fit_geometry_to_monitor).
#
# This cannot rescue a Wayland session: there a client is not told where it
# was put, and the position Qt reports back is the one we asked for.
GEOMETRY_FIT_MS = 250
# Floors for that repair: below these the measurement is likelier to be wrong
# than the window, and a sliver of an overlay helps nobody.
MIN_FITTED_WIDTH = 240
MIN_FITTED_HEIGHT = 120

# Realtime feed: a new block moves the feed's TARGET, and the visible offset
# eases toward it instead of jumping a whole block's height the instant the
# translation lands. The Tk overlay's figures (LIVE_FEED_ANIM_* in
# gui/subtitle_window.py), which were tuned on real sessions.
FEED_ANIM_FRAME_MS = 30
FEED_ANIM_EASE = 0.3  # fraction of the remaining gap closed per frame
FEED_ANIM_MIN_STEP = 2.0  # px/frame floor, or the tail of a slide crawls
FEED_ANIM_SNAP_PX = 1.0  # within this, land on the target and stop


# macOS keeps the Dock and the menu bar above every window level a Qt client
# can ask for — a stays-on-top window floats above other applications and still
# under both — so the overlay is laid out inside the work area there whatever
# its stacking, or its bottom strip disappears behind the Dock. The Tk overlay
# reached the same conclusion (_MACOS_MENU_BAR_HEIGHT). A module constant
# rather than an inline check, so a test can drive the other platform's branch
# without faking sys.platform for the whole process.
_MACOS = sys.platform == "darwin"
# Windows keeps its taskbar in the SAME topmost band as the overlay, and the
# order inside that band is whoever was raised last — so a click on the taskbar
# lifts the shell over the subtitles and nothing ever lowers it again. The
# overlay puts itself back (_keep_on_top) on this interval. A module constant
# for the same reason as _MACOS: a test drives the branch without faking
# sys.platform for the whole process.
_WINDOWS = sys.platform == "win32"
# Slow on purpose. The fix is for a click that happened, not a race, and
# raising a window already at the top of its band costs nothing — but a client
# that restacks itself many times a second is one that fights every other
# topmost window on the desktop.
RESTACK_MS = 1000


@dataclass
class _Run:
    """One laid-out paragraph, ready to draw and to measure a backdrop from.

    Carries the layout rather than the text, because both the backdrop and the
    glyphs have to come from the SAME layout: laying the paragraph out twice
    invites the two to disagree about where a line broke. It also keeps the
    layout referenced while its ``QTextLine``s are read — a line borrows from
    its layout, and reading one whose layout has been collected takes the
    process down with a heap error rather than an exception.
    """

    layout: QTextLayout
    top: float
    height: int


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
        static_lift_percent: int = 0,
        backdrop_opacity: int = DEFAULT_BACKDROP_OPACITY,
        show_footer: bool = True,
        theme_mode: str = "dark",
        bilingual_mode: bool = False,
        side_by_side: bool = False,
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
        self._lift_percent = static_lift_percent
        self._backdrop_opacity = backdrop_opacity
        self._show_footer = show_footer
        self._theme_mode = theme_mode
        self._bilingual = bilingual_mode
        self._side_by_side = side_by_side
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
        # Live only while _paint_static is shrinking a block into a band too
        # short for it (see _static_fit_scale). 1.0 everywhere else, so no
        # other mode pays anything for it.
        self._fit_scale = 1.0

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
        # Before set_always_on_top, which re-places the window: _apply_geometry
        # arms this timer and fills these in. Parented, so it dies with the
        # window — a fit check that outlived the overlay would call into a
        # deleted C++ object.
        self._requested: QRect | None = None
        self._remapped = False
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.setInterval(GEOMETRY_FIT_MS)
        self._fit_timer.timeout.connect(self._fit_to_screen)
        # Parented, so it dies with the window. Started and stopped by
        # set_always_on_top, which runs on the next line.
        self._restack_timer = QTimer(self)
        self._restack_timer.setInterval(RESTACK_MS)
        self._restack_timer.timeout.connect(self._keep_on_top)
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
        """Transparent backdrop is a static-mode option only.

        It means the same thing in both layouts: no large background, a card
        around each sentence instead. Side by side that takes the two column
        panels away — they are the background there — and gives each column's
        sentence its own card, so nothing ever draws a card inside a panel.
        """
        return self._mode == SUBTITLE_MODE_STATIC and self._transparent_static

    def _backdrop(self) -> QColor:
        """Window backdrop, drawn behind everything else."""
        if self._transparent_static_active() or self._columns_active():
            # Fully transparent. In static mode the contrast comes from
            # per-line cards; in the side-by-side layout the two column panels
            # carry it, and painting a window-wide backdrop as well would put a
            # third, larger box behind the two the layout exists to show.
            return QColor(0, 0, 0, 0)
        return self._backdrop_fill()

    def _backdrop_fill(self) -> QColor:
        """The backdrop's colour at the configured opacity, wherever it lands —
        the window in the stacked layout, the two panels in side by side."""
        base = QColor(self._colors["app_bg"])
        base.setAlpha(round(self._backdrop_opacity * 255 / 100))
        return base

    def _card_fill(self) -> QColor:
        """The per-line card's colour in transparent static mode.

        **Black under the dark subtitle theme, white under the light one.** Not
        the theme's own backdrop tone — over arbitrary video only the extremes
        are a safe backing, which is the reason given at _CARD_PAD_X — but it
        has to be the extreme the TEXT is not. The text colour comes from the
        palette, so a card fixed at black put the light theme's near-black text
        on a near-black box and the subtitles could not be read at all.

        At the same opacity as every other backdrop: the mode takes the
        window's backdrop away, it does not take away the operator's say in how
        strongly the text sits on it. A fixed alpha left Hintergrund-Deckkraft
        with nothing to do here, which is why the row used to grey out.
        """
        light = self._theme_mode == "light"
        value = 255 if light else 0
        return QColor(value, value, value, round(self._backdrop_opacity * 255 / 100))

    # ── geometry ─────────────────────────────────────────────────────────
    def _screen(self):
        screens = QGuiApplication.screens()
        if not screens:
            return None
        idx = max(0, min(self._monitor_index, len(screens) - 1))
        return screens[idx]

    def _effective_height_percent(self) -> int:
        """Height the overlay actually takes, whatever the slider says.

        **Transparent static takes the whole monitor.** It draws ONE block,
        sized to whatever the speaker just said, and a band shorter than that
        block has nowhere to put the overflow: the first lines were cut off at
        the top edge and the last ran under the disclaimer pill and off the
        bottom of the screen, with no scrolling to rescue it — the feed modes
        shift as they fill, static does not. Nothing is lost by the full
        height, because there the overlay has no backdrop of its own: the
        contrast comes from a ribbon drawn around the text (_ribbon_rects), so
        a full-height window paints exactly as much as the text needs and the
        video shows through everywhere else. The slider becomes a LIFT there
        instead — see _static_lift.

        Everywhere else the overlay IS a band and the slider is its height,
        including static with the backdrop on: making that full height would
        wash the whole screen at the backdrop opacity instead of the bottom
        strip the operator asked for.

        Still clamped, although the lift now has a field of its own
        (``_lift_percent``) and can no longer arrive here: a band thinner than
        ``WINDOW_HEIGHT_PERCENT_MIN`` holds no text at all, and this is where
        the number is turned into pixels. A hand-edited settings.json is enough
        to reach it.
        """
        if self._transparent_static_active():
            return 100
        return max(WINDOW_HEIGHT_PERCENT_MIN, self._height_percent)

    def _static_lift(self) -> int:
        """Pixels the static content sits above the bottom edge.

        The height slider's other meaning, and its own stored field (see
        STATIC_LIFT_PERCENT_* in utils/settings). With no band to resize, it
        moves the subtitles and the footer pill UP the screen together — both
        are offset by this one figure, so the disclaimer keeps its place under
        the text rather than the two drifting apart.

        Zero everywhere else, so the feed modes and opaque static are untouched
        and this costs them nothing.

        Clamped to what the block can actually clear: lifting further would
        push the text off the TOP while trying to move it away from the bottom,
        which is the same bug at the other end. Both stop together, so the pill
        never climbs past the text it belongs to.
        """
        if not self._transparent_static_active():
            return 0
        percent = max(
            STATIC_LIFT_PERCENT_MIN, min(STATIC_LIFT_PERCENT_MAX, self._lift_percent)
        )
        lift = int(self.height() * percent / 100)
        if self._blocks:
            room = self._content_height() - self._measure_block(self._blocks[-1])
            lift = min(lift, max(0, room))
        return lift

    def _apply_geometry(self) -> None:
        """Occupy the bottom ``height_percent`` of the chosen screen.

        Which bottom depends on the stacking: a topmost window paints OVER the
        Windows taskbar, so it can have the whole screen. A window that is not
        topmost is painted over BY the taskbar, which would swallow the
        disclaimer pill and the last line of every subtitle — so it is laid out
        inside the work area instead, ending above the taskbar. Same reasoning
        the Tk overlay applies to the macOS Dock, which is always above it.

        macOS is the exception to all of it: nothing puts a window above the
        Dock or the menu bar there — a stays-on-top window sits at the floating
        window level, which is still below both — so a full-height overlay lost
        its bottom strip, disclaimer pill and all, behind the Dock. It is laid
        out inside the work area whatever the stacking, which is the same call
        the Tk overlay makes (gui/subtitle_window.py _set_screen_position) and
        what the README already tells macOS users to expect.

        Qt reports both rectangles in logical units already, so there is no DPI
        arithmetic here — that is what gui/scaling.py does by hand.
        """
        screen = self._screen()
        if screen is None:
            return
        over_the_taskbar = is_window_on_top(self) and not _MACOS
        g = screen.geometry() if over_the_taskbar else screen.availableGeometry()
        h = max(1, int(g.height() * self._effective_height_percent() / 100))
        # Kept, because it is a REQUEST: _fit_to_screen compares it against what
        # the window manager actually did.
        self._requested = QRect(g.x(), g.y() + g.height() - h, g.width(), h)
        self._remapped = False
        self.setGeometry(self._requested)
        self._fit_timer.start()

    def _fit_to_screen(self) -> None:
        """Make what the window manager granted match what was asked for.

        Two things can go wrong, in this order. **The move can be refused.** An
        X11 WM applies a mapped window's geometry at its own discretion, and
        the one place it always honours a position is the next map — the same
        rule that makes _NET_WM_STATE_ABOVE unwritable while mapped
        (gui/widgets.needs_remap). That is why lowering the overlay's height
        left the top edge where it was and walked the footer UP: the size was
        applied and the move was not. So the request is made again with the
        window unmapped, exactly as the Tk overlay does it
        (_set_screen_position(force_redraw=True)) — once per placement, or a
        WM that simply will not comply would flash the overlay forever.

        **Or the rectangle can be refused outright** — GNOME keeps a frameless
        window clear of its struts — and then the overlay hangs off the bottom
        of the screen with the disclaimer pill on it. Nothing can be done about
        the position at that point, so it is shrunk into what is on screen.
        Only ever shrinks, keeps the top edge, and ignores an implausible
        measurement.

        On a platform that places windows exactly, both checks see the request
        honoured and this returns without touching anything.
        """
        screen = self._screen()
        if screen is None or not self.isVisible():
            return
        if self._requested is not None and self.geometry() != self._requested:
            granted = self.geometry()
            log(
                "Overlay geometry: asked for "
                f"{self._requested.width()}x{self._requested.height()}"
                f"+{self._requested.x()}+{self._requested.y()}, "
                f"the window manager gave {granted.width()}x{granted.height()}"
                f"+{granted.x()}+{granted.y()}",
                level="INFO",
            )
            if needs_remap() and not self._remapped:
                self._remapped = True
                # QWidget's own hide/show, never this class's: hide() drops the
                # live transcript and show() re-enters _apply_geometry.
                QWidget.hide(self)
                self.setGeometry(self._requested)
                QWidget.show(self)
                # Re-mapping hands the stacking back to the window manager, and
                # a window that never takes focus can come back under every
                # other application. An overlay nobody can see is worse than
                # one briefly in front of the panel — and clicking the panel
                # puts it back, which is not true the other way round.
                self.raise_()
                # Re-checked once the map has been through the WM, in case the
                # rectangle itself was refused too.
                self._fit_timer.start()
                return
        g = screen.geometry()
        have = self.geometry()
        fit_w = min(have.width(), g.x() + g.width() - have.x())
        fit_h = min(have.height(), g.y() + g.height() - have.y())
        if fit_w < MIN_FITTED_WIDTH or fit_h < MIN_FITTED_HEIGHT:
            return
        if (fit_w, fit_h) == (have.width(), have.height()):
            return  # the request was granted
        self.resize(fit_w, fit_h)
        self.update()

    # ── layout helpers ───────────────────────────────────────────────────
    def _content_width(self) -> int:
        return max(1, int(self.width() * (1 - 2 * SIDE_MARGIN_RATIO)))

    # ``font_size_base`` and ``source_font_size_base`` are DIVISORS, not pixel
    # sizes: the rendered size is the window width divided by the base, so text
    # keeps its proportion on any monitor. Smaller base => larger text.
    #
    # ``_fit_scale`` is applied on top, and is 1.0 except while static mode is
    # shrinking a block into a band too short for it (see _static_fit_scale).
    # It multiplies BOTH sizes, so the original keeps its proportion to its
    # translation however far the pair has to shrink.
    def _translation_px(self) -> int:
        if not self.width():
            return 24
        size = self.width() / self._font_size_base * self._fit_scale
        return max(12, min(120, int(size)))

    def _source_px(self) -> int:
        if not self.width():
            return 17
        size = self.width() / self._source_font_size_base * self._fit_scale
        return max(12, min(120, int(size)))

    def _measure_at(self, block: Block, scale: float) -> int:
        """``block``'s height with the fonts scaled by ``scale``."""
        previous = self._fit_scale
        self._fit_scale = scale
        try:
            return self._measure_block(block)
        finally:
            self._fit_scale = previous

    def _static_fit_scale(self, block: Block) -> float:
        """Shrink factor that makes ``block`` fit the band it is drawn in.

        Static draws one block and never scrolls, so a block taller than the
        overlay simply loses its ends — the first lines cut off at the top, the
        last under the disclaimer pill and off the screen. Where the overlay is
        the whole monitor that cannot happen and this returns 1.0. Where it is a
        BAND — static with the backdrop on, whose height is exactly what the
        slider is for — the text is fitted to the band instead, which is what
        the Tk overlay did (_static_fonts_for_content).

        The answer is the LARGEST scale that fits, found by bisection, and it
        has to be searched for in both directions. Height is only roughly
        linear in font size — halve the size and each line is half as tall, but
        twice as much text fits on it, so the line COUNT roughly halves too —
        and "roughly" is the whole problem: wrapping moves in whole words, so
        the linear estimate ``available / measured`` usually UNDERSHOOTS. A
        search that only ever shrinks from it stops at the first size that
        happens to fit and leaves the band visibly half empty (49 px of text in
        a 66 px band, which is what this replaces).

        Floored, because past a point shrinking stops being a fix — text nobody
        can read from the back of a hall is not better than text that is cut
        off, and ``_translation_px`` clamps at 12 px anyway. If even the floor
        does not fit, it is returned regardless: it is the smallest this can
        make the block, and the alternative is drawing it larger for no gain.
        """
        if self._transparent_static_active():
            return 1.0
        available = self._content_height()
        if available <= 0 or self._measure_at(block, 1.0) <= available:
            return 1.0
        if self._measure_at(block, _FIT_MIN_SCALE) > available:
            return _FIT_MIN_SCALE
        # ``low`` always fits and ``high`` never does; the answer is between.
        low, high = _FIT_MIN_SCALE, 1.0
        for _ in range(_FIT_SEARCH_STEPS):
            middle = (low + high) / 2
            if self._measure_at(block, middle) <= available:
                low = middle
            else:
                high = middle
        return low

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
        # Excluding the honorifics is safe because _honorific_formats has
        # already made sure they FIT the line: nothing is being ignored that
        # could reach outside the box measured here.
        #
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

    @staticmethod
    def _descent_slack(text: str, font: QFont) -> int:
        """Descent box that ``text`` does not draw into.

        The mirror of ``_reclaim`` at the other end of a line, and used at ONE
        join: between a source line and the translation under it. Everywhere
        else the upper line keeps its whole descent zone deliberately — the eye
        measures baselines, so tucking a descender-less line closer makes it an
        outlier (the rule gui/subtitle_window.py states at _stack_overlap).

        That rule costs nothing on Windows, where Segoe UI's Arabic ink reaches
        exactly its descent line. It costs 16 px per pair on Linux, where Noto
        Sans Arabic reserves 35 px of descent at 48 px text and an Arabic line
        draws 19 px into it: the original floated a fifth of an em above its
        own translation there and sat on it here. Measured, so the join is
        ink-to-ink on both — and provably unchanged wherever the slack is zero.
        """
        measured = _HONORIFIC_LIGATURE_RE.sub("", text).strip()
        if not measured:
            return 0
        metrics = QFontMetrics(font)
        ink = metrics.tightBoundingRect(measured)
        if ink.isEmpty():
            return 0
        # Never both this and _ink's overhang: ink past the descent is one, ink
        # short of it the other.
        return max(0, metrics.descent() - ink.bottom())

    @staticmethod
    def _dominant_direction(text: str):
        """Base direction from which script the line is MOSTLY in.

        Unicode decides a paragraph's direction from its first strong
        character, and Qt's LayoutDirectionAuto follows that — correct in
        general, and wrong for a live transcript, because a streaming STT
        prefixes artefact markers like ``<noise>``. Five Latin letters then
        make an entire Arabic sentence an LTR paragraph and its full stop
        lands at the right, which is where the sentence STARTS.

        Deliberately not used for settled subtitles: a translation line
        legitimately opens in one script and quotes the other, and counting
        would flip a German sentence that happens to carry a long Arabic
        quotation. A transcript row is one language plus noise.
        """
        rtl = ltr = 0
        for ch in text:
            bidi = unicodedata.bidirectional(ch)
            if bidi == "L":
                ltr += 1
            elif bidi in ("R", "AL"):
                rtl += 1
        if rtl == ltr:
            return Qt.LayoutDirectionAuto
        return Qt.RightToLeft if rtl > ltr else Qt.LeftToRight

    @staticmethod
    def _honorific_formats(text: str, font: QFont) -> list:
        """Shrink any ﷺ/ﷻ in ``text`` until it fits the line it sits in.

        The ligature is drawn by whichever family happens to have the glyph,
        not the one we asked for, and on Linux that family draws it far taller
        than the line around it. Every way of accounting for that in the
        SPACING is a bad trade: measure it and the honorific pushes its whole
        paragraph away from the one above; ignore it and it climbs into that
        paragraph; clamp it and the paragraph's own rows spread apart to make
        room (all three were seen, in that order).

        So the ligature is made to fit instead. A per-character format is the
        one thing that can, and it costs nothing where the glyph already fits
        — on Windows, Segoe UI's ligature is no taller than the cap height and
        this returns nothing at all.
        """
        matches = list(_HONORIFIC_LIGATURE_RE.finditer(text))
        if not matches:
            return []
        metrics = QFontMetrics(font)
        formats: list = []
        cache: dict[str, QTextCharFormat | None] = {}
        for match in matches:
            glyph = match.group(0)
            if glyph not in cache:
                cache[glyph] = SubtitleWindow._fitted_format(glyph, font, metrics)
            char_format = cache[glyph]
            if char_format is None:
                continue
            span = QTextLayout.FormatRange()
            span.start = match.start()
            span.length = len(glyph)
            span.format = char_format
            formats.append(span)
        return formats

    @staticmethod
    def _fitted_format(glyph: str, font: QFont, metrics: QFontMetrics):
        """A format that scales ``glyph`` into the font's own box, or None.

        None means it already fits — the common case, and the one where this
        whole mechanism must stay invisible.
        """
        ink = metrics.tightBoundingRect(glyph)
        if ink.isEmpty():
            return None
        # ink.top() is negative above the baseline; ink.bottom() positive below.
        above, below = -ink.top(), ink.bottom()
        scale = 1.0
        if above > metrics.ascent():
            scale = min(scale, metrics.ascent() / above)
        if below > metrics.descent():
            scale = min(scale, metrics.descent() / below)
        if scale >= 1.0:
            return None
        smaller = QFont(font)
        smaller.setPixelSize(max(1, round((font.pixelSize() or 1) * scale)))
        char_format = QTextCharFormat()
        char_format.setFont(smaller)
        return char_format

    def _layout_text(
        self,
        text: str,
        font: QFont,
        direction=Qt.LayoutDirectionAuto,
        width: int | None = None,
    ) -> tuple[QTextLayout, int]:
        """``text`` wrapped to ``width``, at a tightened line rhythm.

        ``width`` defaults to the full content width; the side-by-side layout
        passes one column instead. Everything else about the rhythm is
        width-independent, so measuring and drawing stay in step as long as
        both are given the same figure.

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
        # with a quote, a digit or the elision ellipsis. ``direction`` overrides
        # it where first-strong is the wrong rule — see _dominant_direction.
        option.setTextDirection(direction)
        layout.setTextOption(option)
        # Before any measuring: a ligature left at full size changes the line's
        # ascent, and then every figure below is taken from a box the text does
        # not actually occupy.
        formats = self._honorific_formats(text, font)
        if formats:
            layout.setFormats(formats)

        fm = QFontMetrics(font)
        base_ascent = QFontMetricsF(font).ascent()
        reclaim, overhang = self._ink(text, font)
        # ``overhang`` also has to widen the pitch, not just the block's foot:
        # two wrapped lines of the same Arabic sentence stack at this distance
        # and would collide with each other otherwise.
        pitch = max(1, fm.lineSpacing() - reclaim + overhang)
        if width is None:
            width = self._content_width()
        count, y = 0, 0.0
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            # Placed by its BASELINE, not by the top of its box. A line's
            # ascent is the tallest of the font engines that actually drew it,
            # including the fallback family a honorific comes from — and that
            # family's ascent can be far larger than the one every figure here
            # is measured from, even after _honorific_formats has scaled the
            # glyph's INK to fit. Setting the box top on our own rhythm then
            # drops the line by the difference, and the blank band that opens
            # above it is the honorific visibly pushing its paragraph away from
            # the one before it. Correcting for the excess puts the baseline
            # back on the rhythm the block was measured at.
            #
            # Only ever pulls a line UP: an ascent BELOW the asked-for font's
            # is that font's own business (the metrics here are its own), and
            # dropping the line to meet it would move text that is already
            # where it belongs. Measured against the UNROUNDED ascent, so a
            # line drawn entirely in the font we asked for comes out at exactly
            # its old position — QFontMetrics rounds to whole pixels and
            # QTextLine does not, and that difference alone would have shifted
            # every line on every platform.
            line.setPosition(QPointF(0, y - max(0.0, line.ascent() - base_ascent)))
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

    def _measure(self, text: str, font: QFont, width: int | None = None) -> int:
        """Height ``text`` occupies at ``font`` within ``width``."""
        return self._layout_text(text, font, width=width)[1]

    # ── side-by-side columns ─────────────────────────────────────────────
    def _panel_geometry(self) -> tuple[int, int, int]:
        """``(left panel x, right panel x, panel width)``.

        The panels come first and the text columns are measured to what is left
        inside them, rather than the other way round: they are the backdrop in
        this layout, so their distance from the window edge is a backdrop
        margin (small) and not a text margin (SIDE_MARGIN_RATIO, much larger).
        """
        margin = int(self.width() * COLUMN_PANEL_MARGIN_RATIO)
        gap = int(self.width() * COLUMN_GAP_RATIO)
        width = max(1, (self.width() - 2 * margin - gap) // 2)
        return margin, self.width() - margin - width, width

    def _column_width(self) -> int:
        """Width of one text column — a panel less its inset on both sides."""
        return max(1, self._panel_geometry()[2] - 2 * COLUMN_PANEL_PAD_X)

    def _feed_top(self) -> int:
        """Where the feed's first line sits below the overlay's top edge.

        In the side-by-side layout it is measured from the PANEL, by the
        panel's own inset: the panel is the container and now starts a
        clearance below the top edge (_column_panel_rects), so a line held
        FEED_TOP_RATIO down from that edge would leave a band of empty backdrop
        above it three times the inset the same panel keeps at its sides.

        Everywhere else there is no container, and the line is kept off the
        edge by a share of the height instead.
        """
        if self._columns_active():
            return PILL_CLEARANCE + COLUMN_PANEL_PAD_Y
        return int(self.height() * FEED_TOP_RATIO)

    def _columns_active(self) -> bool:
        """Whether the overlay is in the side-by-side layout at all.

        Block-independent, unlike ``_two_column``: the panels are drawn once
        per frame and stay put whether or not the utterance on screen happens
        to carry an original.
        """
        return bool(self._side_by_side and self._bilingual)

    def _column_panel_rects(self) -> tuple[QRect, QRect] | None:
        """The two fixed panels behind the columns, or None outside the layout.

        Both the same size and in the same place every frame. They span the
        content area, ``PILL_CLEARANCE`` below the overlay's top edge down to
        where the footer pill's clearance begins.

        The SAME figure at both ends, which is the point: the panels ARE the
        backdrop here (see _backdrop), so the only thing marking the top of the
        overlay is where they start, and at the feed's own margin they began
        far enough down that at 100% height a band of video stood between them
        and the monitor's upper border while a hairline stood below them. One
        clearance, top and bottom, and the panel sits in an even frame. The
        first line keeps its distance from the panel through the panel's own
        inset instead — see _feed_top.

        None during an announcement: that renders large and centred across the
        whole window, and framing it in two columns it does not use would read
        as a mistake. None with the Transparent toggle on too: it exists to
        take the background away, and here the panels are the background.
        """
        if (
            not self._columns_active()
            or self._announcement
            or self._transparent_static_active()
        ):
            return None
        left_x, right_x, width = self._panel_geometry()
        top = PILL_CLEARANCE
        height = max(1, self._content_height() - top)
        return (
            QRect(left_x, top, width, height),
            QRect(right_x, top, width, height),
        )

    def _draw_column_panels(self, p: QPainter, rects: tuple[QRect, QRect]) -> None:
        colour = self._backdrop_fill()
        for rect in rects:
            path = QPainterPath()
            path.addRoundedRect(rect, COLUMN_PANEL_RADIUS, COLUMN_PANEL_RADIUS)
            p.fillPath(path, colour)

    def _column_source_qcolor(self, newest: bool) -> QColor:
        """The original's colour in the side-by-side layout.

        Stacked, the original is a subordinate line above its translation and
        takes the muted tone. Side by side it is the other half of the row, and
        at the muted tone the newest utterance reads as already-said on one
        side and current on the other. So the newest row's original carries the
        full text colour like its translation, and older rows drop to history
        exactly as the translation does. A configured source colour still wins;
        only the DEFAULT differs from the stacked layout.
        """
        if not newest:
            return self._history_qcolor()
        return QColor(self._source_color or self._colors["text"])

    def _two_column(self, block: Block) -> bool:
        """Whether ``block`` lays out as two columns rather than stacked.

        Needs a separate original to put in the second column. Same-language
        mode, error messages and the verified-verse bypass all emit a
        translation with ``source=None``, and those rows keep the full width —
        half a screen of blank beside an error message helps nobody.
        """
        return bool(self._side_by_side and self._bilingual and block.source)

    def _translation_on_left(self, block: Block) -> bool:
        """Which column the translation takes.

        RTL text goes right, because that is where an RTL reader's eye starts —
        the Arabic → German main path puts the Arabic right and the German
        left. When the RTL side is the *translation* (Turkish → Arabic) the
        columns swap, so this follows the script rather than "source vs
        translation".

        When neither side is RTL there is no directional reason, and the
        tiebreak is that the translation keeps the left column anyway: the
        audience's own language then sits in the same place whatever the
        speaker switches to. ``is_arabic_text`` is the whole RTL test because
        Arabic, Urdu and Persian are the only RTL languages offered and all
        three are Arabic-script; the bidi-counting rule in
        ``_dominant_direction`` is deliberately not reused here (see its
        docstring — a translation quoting the other script would flip).
        """
        return not (
            is_arabic_text(block.translation) and not is_arabic_text(block.source or "")
        )

    def _column_rects(self, block: Block, y: int) -> tuple[QRect, QRect] | None:
        """``(source rect, translation rect)`` for ``block``, or None if stacked.

        Placed inside the panels rather than from the caller's ``x``: the panel
        is the container, so a column sits at the panel's edge plus its inset
        and the row's own left margin does not apply.

        Both rects share ``y``: the columns are a table row, top-aligned, and
        the taller of the two decides the row height. Letting each column flow
        on its own is what would put pair 3 beside pair 5 within a few
        utterances.
        """
        if not self._two_column(block):
            return None
        left_panel_x, right_panel_x, _width = self._panel_geometry()
        left_x = left_panel_x + COLUMN_PANEL_PAD_X
        right_x = right_panel_x + COLUMN_PANEL_PAD_X
        col = self._column_width()
        trans_font, src_font = self._block_fonts(block)
        src_h = self._measure(block.source, src_font, col)
        trans_h = self._measure(block.translation, trans_font, col)
        if self._translation_on_left(block):
            return QRect(right_x, y, col, src_h), QRect(left_x, y, col, trans_h)
        return QRect(left_x, y, col, src_h), QRect(right_x, y, col, trans_h)

    def _block_fonts(self, block: Block) -> tuple[QFont, QFont | None]:
        trans = subtitle_font(self._translation_px(), text=block.translation)
        src = None
        if self._bilingual and block.source:
            src = source_font(
                self._source_px(), block.source, bold=self._side_by_side
            )
        return trans, src

    def _pair_gap(self, block: Block) -> int:
        """Space between a block's source line and its translation.

        Closed at BOTH ends, which no other join is: the translation's blank
        band above its ink (``_reclaim``) and the source's unused descent below
        its own (``_descent_slack``). What is left is PAIR_GAP of real ink
        distance on any font on any platform — the pair is one utterance and
        has to read as one, and a family that reserves a deep descent it does
        not use had it floating a fifth of an em above its own translation.
        """
        trans_font, src_font = self._block_fonts(block)
        # Deliberately allowed to go NEGATIVE: the reclaim is normally larger
        # than PAIR_GAP, and clamping at zero threw the remainder away and left
        # the pair as far apart as before. The boxes overlap; the INK cannot,
        # because _reclaim already holds _STACK_INK_GAP_EM back. Tk stacks the
        # same way (_stack_rows_tight positions at bbox top + overlap).
        gap = PAIR_GAP - self._reclaim(block.translation, trans_font)
        if src_font is not None and block.source:
            gap -= self._descent_slack(block.source, src_font)
        return gap

    def _measure_block(self, block: Block) -> int:
        trans_font, src_font = self._block_fonts(block)
        if self._two_column(block):
            # A row is as tall as its taller cell, not as tall as both — the
            # whole point of the layout is that the translation no longer sits
            # below its own original.
            col = self._column_width()
            return max(
                self._measure(block.source, src_font, col),
                self._measure(block.translation, trans_font, col),
            )
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
        if self._two_column(block):
            # Both columns start at the block's top edge, so only the SMALLER
            # of the two blank bands may be closed up: taking the larger would
            # let the other column's ink reach into the block above.
            reclaim = min(
                self._reclaim(block.source, src_font),
                self._reclaim(block.translation, trans_font),
            )
            return REALTIME_BLOCK_SPACING - reclaim
        if src_font is not None and block.source:
            first_text, first_font = block.source, src_font
        else:
            first_text, first_font = block.translation, trans_font
        # Not clamped at zero, for the reason given in _pair_gap — though a
        # block usually opens on an Arabic source line, whose ink reaches the
        # ascent and reclaims nothing, so this mostly IS the constant.
        return REALTIME_BLOCK_SPACING - self._reclaim(first_text, first_font)

    def _live_gap(self) -> int:
        """Space between the last settled block and the live line.

        The same distance one block keeps from the next, so the feed has a
        single rhythm — the Tk overlay puts REALTIME_BLOCK_SPACING here too.
        """
        return REALTIME_BLOCK_SPACING - self._reclaim(
            self._live_text or "", self._live_font()
        )

    def _ribbon_rects(self, runs: list[_Run], left: int, width: int) -> list[QRect]:
        """One backdrop per RENDERED LINE, tiled into a single ribbon.

        The shape a YouTube subtitle has, and the one asked for: each line gets
        a box as wide as that line's own text, so a short line is a short box
        and the backdrop grows and shrinks with what was said instead of always
        running to the window's edges. Per LINE, not per paragraph — a
        paragraph's width is its longest line, so one box around a wrapped
        sentence is a rectangle with ragged text inside it.

        Tiled, and that is also the fix for the overlap. A block's source line
        and its translation are deliberately pulled together until their metric
        BOXES overlap — ``_pair_gap`` is allowed to go negative and only the
        INK is held apart — so two independent backdrops drew one on top of the
        other and the second hid the last line of the first. Here each box ends
        exactly where the next one begins, so none of them can cover anything.

        Boxes are extended down by their own corner radius so the rounding does
        not notch every join; the caller fills them as one winding path, which
        is what keeps the overlap from painting the alpha twice.
        """
        tops: list[float] = []
        widths: list[float] = []
        bottom = 0.0
        for run in runs:
            for i in range(run.layout.lineCount()):
                line = run.layout.lineAt(i)
                tops.append(run.top + line.position().y())
                widths.append(line.naturalTextWidth())
            bottom = run.top + run.height
        rects: list[QRect] = []
        last = len(tops) - 1
        for i, (top, text_width) in enumerate(zip(tops, widths, strict=True)):
            box_top = top - _CARD_PAD_Y if i == 0 else top
            if i == last:
                box_bottom = bottom + _CARD_PAD_Y
            else:
                box_bottom = tops[i + 1] + _CARD_RADIUS
            box_width = min(width, int(text_width) + 2 * _CARD_PAD_X)
            rects.append(
                QRect(
                    left + (width - box_width) // 2,
                    int(box_top),
                    box_width,
                    max(0, int(box_bottom - box_top)),
                )
            )
        return rects

    def _draw_ribbon(self, p: QPainter, rects: list[QRect]) -> None:
        """Fill ``rects`` as one shape.

        One path and one fill, with the WINDING rule: the boxes overlap by a
        corner radius (see _ribbon_rects) and Qt's default odd-even rule would
        punch that overlap out as a hole, while filling them one at a time
        would composite the translucent black twice and leave a dark band
        across every join.
        """
        if not rects:
            return
        path = QPainterPath()
        path.setFillRule(Qt.WindingFill)
        for rect in rects:
            path.addRoundedRect(rect, _CARD_RADIUS, _CARD_RADIUS)
        p.fillPath(path, self._card_fill())

    def _draw_block(
        self, p: QPainter, block: Block, x: int, y: int, newest: bool = True
    ) -> int:
        """Draw ``block`` with its top edge at ``y``; return the height used.

        ``newest`` carries the full translation colour; everything above it is
        history and drops to the muted tone, so the eye lands on the line
        being spoken now without having to search for it.

        Every backdrop is drawn before any text, never interleaved: the source
        and its translation are stacked close enough that their boxes overlap,
        so a backdrop painted between them covers the line above (see
        _ribbon_rects).
        """
        trans_font, src_font = self._block_fonts(block)
        w = self._content_width()
        cards = self._transparent_static_active()
        rects = self._column_rects(block, y)
        if rects is not None:
            src_rect, trans_rect = rects
            # Cards only when the Transparent toggle has taken the panels away
            # — otherwise the panel already carries the text over video, and
            # drawing both would stack a card inside a panel. One ribbon per
            # column, because the columns are side by side and share no run.
            runs: list[tuple[_Run, QRect, QColor]] = []
            for text, font, rect, colour in (
                (block.source, src_font, src_rect, self._column_source_qcolor(newest)),
                (
                    block.translation,
                    trans_font,
                    trans_rect,
                    self._translation_qcolor() if newest else self._history_qcolor(),
                ),
            ):
                layout, height = self._layout_text(text, font, width=rect.width())
                runs.append((_Run(layout, rect.y(), height), rect, colour))
            if cards:
                for run, rect, _colour in runs:
                    self._draw_ribbon(
                        p, self._ribbon_rects([run], rect.x(), rect.width())
                    )
            for run, rect, colour in runs:
                p.setPen(colour)
                run.layout.draw(p, QPointF(rect.x(), rect.y()))
            return max(src_rect.height(), trans_rect.height())
        used = 0
        stacked: list[tuple[_Run, QColor]] = []
        if src_font is not None and block.source:
            layout, sh = self._layout_text(block.source, src_font)
            stacked.append((_Run(layout, y, sh), self._source_qcolor()))
            used += sh + self._pair_gap(block)
        layout, th = self._layout_text(block.translation, trans_font)
        stacked.append(
            (
                _Run(layout, y + used, th),
                self._translation_qcolor() if newest else self._history_qcolor(),
            )
        )
        if cards:
            self._draw_ribbon(
                p, self._ribbon_rects([run for run, _c in stacked], x, w)
            )
        for run, colour in stacked:
            p.setPen(colour)
            run.layout.draw(p, QPointF(x, run.top))
        return used + th

    # ── painting ─────────────────────────────────────────────────────────
    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), self._backdrop())

        panels = self._column_panel_rects()
        if panels is not None:
            self._draw_column_panels(p, panels)
            # A panel is a container, so its column's text is clipped to it:
            # the realtime feed shifts up as it fills, and without this the
            # rows that have scrolled past keep drawing ABOVE the panel, which
            # reads as text floating loose next to the box it belongs in.
            p.setClipRect(panels[0].united(panels[1]))

        if self._announcement:
            self._paint_announcement(p)
        elif self._mode == SUBTITLE_MODE_REALTIME:
            self._paint_realtime(p)
        elif self._mode == SUBTITLE_MODE_CONTINUOUS:
            self._paint_continuous(p)
        else:
            self._paint_static(p)

        # Pills and announcements are full-width furniture and must never be
        # cut by a column.
        p.setClipping(False)

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
        top = self._feed_top()
        heights = [self._measure_block(b) for b in self._blocks]
        # The advance past each block: its own height plus the gap whatever
        # comes NEXT wants above it. One list, used by all three passes below,
        # so the total, the draw loop and the eviction compensation cannot
        # disagree — if they did, the feed would jump every time a block
        # scrolled off. ``followers`` is empty when there are no blocks —
        # [*[], None] would be a one-element list against zero heights.
        #
        # Nothing follows the last block unless the live line does, and then
        # its advance is its height alone. Charging a trailing gap regardless
        # reserved a block's worth of empty space under the newest subtitle
        # and held the whole feed that far off the footer; the Tk overlay ends
        # at the last block's foot (_feed_natural_layout).
        followers = [*self._blocks[1:], None] if self._blocks else []
        live_gap = self._live_gap() if self._live_text else 0
        advances = [
            h + (self._block_gap(nxt) if nxt is not None else live_gap)
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
        """Only the newest block, sitting just above the footer.

        Anchored to the BOTTOM of the content area, not centred in it. Centring
        was invisible while the overlay was a band at the bottom of the screen
        — the band was barely taller than the block — but static now takes the
        whole monitor (_effective_height_percent), and there it left the
        subtitles floating in the middle of the picture. Where a subtitle
        belongs is where the Tk overlay put it, an ink's distance off the
        bottom edge (_create_outlined_text at canvas_height - 4).

        ``_content_height`` already holds back the footer pill and its
        clearance, so this lands the block just above the disclaimer and grows
        UPWARD as the utterance gets longer.

        ``_static_lift`` then raises the whole arrangement off the bottom edge
        — the pills subtract the same figure, so the two move as one.

        **A block too tall for the space loses its TOP, never its bottom.** The
        anchor used to be clamped to y=0 so the opening lines could not be cut
        off, but a clamp at the top pushes the foot down by the same amount:
        on a 38 px band (5% of a 768 px screen, where a bilingual block bottoms
        out at 50 px because both fonts have hit the 12 px floor) the last line
        was drawn 12 px BELOW the overlay, off the monitor entirely and across
        the disclaimer on its way. Overflowing upward keeps the newest words
        and the pill on screen, and it is what the feed modes already do when
        they run out of room.
        """
        if not self._blocks:
            return
        block = self._blocks[-1]
        x = int(self.width() * SIDE_MARGIN_RATIO)
        # Set for the whole of the measuring AND the drawing, so the two can
        # never disagree about how big the text is; restored afterwards because
        # the pills and every other mode read the same two size helpers.
        self._fit_scale = self._static_fit_scale(block)
        try:
            bottom = self._content_height() - self._static_lift()
            self._draw_block(p, block, x, bottom - self._measure_block(block))
        finally:
            self._fit_scale = 1.0

    def _live_font(self) -> QFont:
        """Font of the in-progress transcript line.

        The FULL translation size, as the Tk overlay draws it (_live_font_for):
        the live line is the sentence being spoken, not a footnote to it — and
        Arabic renders bold and upright there, like a translation line, Latin
        italic and regular. This used to be handed ``_font_size_base``, which
        is a divisor and not a pixel size at all, so the line came out at a
        size unrelated to everything around it — and to the height reserved for
        it, which was already measured at the translation size.
        """
        text = self._live_text or ""
        px = self._translation_px()
        if is_arabic_text(text):
            return subtitle_font(px, text=text)
        return source_font(px, text)

    def _live_line_height(self) -> int:
        fm = QFontMetrics(self._live_font())
        return fm.height() * REALTIME_LIVE_MAX_ROWS

    def _live_rows(self) -> str:
        """The tail of the live text that fits in REALTIME_LIVE_MAX_ROWS rows.

        Wrapped greedily from the START, then only the last rows are kept —
        the Tk overlay's rule (_render_live_line). Row boundaries therefore
        stay put as the interim grows: the visible row fills up to the edge,
        and the next word starts a fresh one that grows from the middle again.

        This deliberately does not elide. ``QFontMetrics.elidedText`` was doing
        the truncation before, which slid the text along one character at a
        time instead of turning the row over — and it re-ordered RTL text on
        the way, which is how the live line ended up with its full stop at the
        wrong end while the settled lines below it were correct.
        """
        text = self._live_text or ""
        if not text:
            return ""
        layout, _height = self._layout_live(text)
        extra = layout.lineCount() - REALTIME_LIVE_MAX_ROWS
        if extra <= 0:
            return text
        # Everything from the first VISIBLE row onward; the wrap point is a
        # word boundary, so the leading space belongs to the row above.
        return text[layout.lineAt(extra).textStart() :].lstrip()

    def _layout_live(self, text: str) -> tuple[QTextLayout, int]:
        """Lay ``text`` out as the live line — one direction rule, one font."""
        return self._layout_text(
            text, self._live_font(), self._dominant_direction(text)
        )

    def _draw_live_line(self, p: QPainter, x: int, y: int) -> None:
        """In-progress transcript: muted while speaking, primary once settled."""
        text = self._live_rows()
        if not text:
            return
        layout, _height = self._layout_live(text)
        p.setPen(self._translation_qcolor() if self._live_settled else self._source_qcolor())
        layout.draw(p, QPointF(x, y))

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
        from gui.subtitle_text import DEFAULT_FOOTER, FOOTER_TRANSLATIONS

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
        # Once, above whichever pill ends up topmost — and only when there is
        # one, so nothing is held back from an overlay that shows neither.
        if not r:
            return 0
        r += PILL_CLEARANCE
        if self._transparent_static_active():
            # A card is drawn _CARD_PAD_Y BELOW the text it wraps
            # (_ribbon_rects), and reserving for the text alone spent the
            # clearance on that pad: the card's bottom border came out flush
            # against the disclaimer with nothing between them. The panel of
            # the side-by-side layout keeps PILL_CLEARANCE of air there, and a
            # card is the same thing — a backdrop with an edge the eye reads.
            r += _CARD_PAD_Y
        # Never more than half a short overlay. The pills are a fixed size —
        # they deliberately do not scale with the subtitle font — so on a band
        # at the low end of the height slider they asked for more room than the
        # whole window had: the content area collapsed to a single pixel, there
        # was nothing left to fit the text into, and the pill itself was laid
        # out from a bottom edge further up than its own height. Capped, the
        # band keeps a usable content area and the text is fitted into THAT.
        return min(r, max(1, self.height() // 2))

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
        """Footer last-but-one, stopped hint stacked directly above it.

        Raised by ``_static_lift``, the same figure the block above them is
        raised by, so the disclaimer travels with the text it belongs to rather
        than staying pinned to the bottom of the screen while the subtitles
        walk away from it. Zero outside transparent static.
        """
        bottom = self.height() - FOOTER_MARGIN - self._static_lift()
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
        from gui.subtitle_text import split_display_chunks

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
        before = self._effective_height_percent()
        self._mode = mode
        self._scroll_offset = self._feed_target = 0.0
        self._feed_timer.stop()
        # Transparent static takes the whole monitor whatever the slider says
        # (_effective_height_percent), so entering or leaving it changes the
        # window's height even though the setting did not.
        if self._effective_height_percent() != before:
            self._apply_geometry()
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

    def set_side_by_side(self, enabled: bool) -> None:
        self._side_by_side = enabled
        if self._mode == SUBTITLE_MODE_CONTINUOUS:
            # Every block's height just changed, and in continuous mode the y
            # values were computed from the old ones (see set_subtitle_mode).
            self._restack_continuous()
        self.update()

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

    def set_static_lift_percent(self, percent: int) -> None:
        """The transparent-static lift. Never a geometry change.

        The overlay is already the whole monitor there (_effective_height_
        percent), so this moves what is PAINTED inside it and nothing else —
        unlike the height, which resizes the window itself.
        """
        self._lift_percent = percent
        self.update()

    def set_always_on_top(self, enabled: bool) -> None:
        """Toggle the stays-on-top flag, and re-place the overlay for it.

        The flag goes through set_window_on_top, which on Windows does not
        recreate the native window — so the overlay neither vanishes nor
        flashes there. The geometry still has to be recomputed: only a topmost
        window paints over the taskbar, so a non-topmost overlay is laid out
        above it instead.

        No early return on a matching flag: on X11 the flag Qt holds is not
        what the window manager is doing (see widgets.set_window_on_top), and
        this window is re-mapped behind Qt's back by its own geometry repair.
        Skipping the call there left the panel obeying the setting while the
        overlay sat under the browser.

        The flag alone does not keep it in front on Windows — see
        _keep_on_top, which this arms and disarms.
        """
        set_window_on_top(self, enabled)
        if enabled and _WINDOWS:
            self._restack_timer.start()
        else:
            self._restack_timer.stop()
        self._apply_geometry()

    def _keep_on_top(self) -> None:
        """Put the overlay back at the top of the topmost band.

        Always-on-top is not a rank, it is a band: Windows keeps its taskbar in
        that same band, and inside it the order is whoever was raised last. So
        clicking the taskbar lifts the shell over the subtitles for good — the
        flag is still set, the overlay is still "always on top", and it is
        still behind the taskbar. Re-raising is the only lever a client has.

        ``raise_`` and not a re-placement: it restacks without activating
        (SWP_NOACTIVATE on Windows), so the overlay still never takes focus off
        the control panel — the property WA_ShowWithoutActivating exists to
        protect. Skipped while hidden, so a stopped session costs nothing but
        the timer tick.

        Windows only, and armed only while the setting is on. macOS puts a
        floating window below the Dock and the menu bar whatever it asks for
        (see _apply_geometry), so there is nothing to win there; on X11 the
        stacking belongs to the window manager and a client re-raising itself
        every second would be fighting it.
        """
        if self.isVisible():
            self.raise_()

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
        # The toggle decides whether the overlay is a band or the whole monitor
        # (_effective_height_percent), so in static mode it re-places the
        # window as well as changing what is painted in it.
        before = self._effective_height_percent()
        self._transparent_static = enabled
        if self._effective_height_percent() != before:
            self._apply_geometry()
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

    def set_font_size_base(self, value: int) -> None:
        try:
            self._font_size_base = max(20, min(80, int(value)))
        except (TypeError, ValueError):
            return
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

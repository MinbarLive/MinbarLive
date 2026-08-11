"""The shared controls: segmented button, dropdown, slider, and the
always-on-top helpers.

Qt ships no segmented button, and the panel uses one for every either/or choice
(theme, window style, and the two 3-way selectors added in PR #22). It is
rebuilt here rather than substituted with dropdowns, which is a decision about
the UI users already know, not a technical one.

Always-on-top goes through ``set_window_on_top``/``is_window_on_top`` and never
through ``setWindowFlag`` — that recreates the native window (a white flash; it
used to make the overlay vanish outright). ``QWidget.windowFlags()`` is
deliberately not trusted as the state, because on X11 the flag is really the
``_NET_WM_STATE_ABOVE`` property and Qt's xcb plugin only writes it while the
window is unmapped.

``place_window_behind`` is the other half of that: Qt can lift a window to the
front of its band but cannot order two top-level windows against each other,
so the overlay is pinned under the control panel natively.

``set_titlebar_dark`` is the third piece of native window chrome that lives
here rather than in the stylesheet, for the same reason as the others: the
caption bar belongs to the window manager, not to Qt.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from utils.logging import log

# Height of a dropdown, and with it of every small button that shares a row
# with one (the −/+ steppers, "?", the language swap, the colour pickers).
# Mirrors the QComboBox metrics in theme.py: 8px padding top and bottom, a
# 20px minimum content height and a 1px border on each side.
CONTROL_H = 38
# A SegmentedControl that sits inline next to a checkbox rather than owning a
# row. Sized to the checkbox beside it, not to the dropdowns above it.
SEGMENT_COMPACT_H = 26


def is_window_on_top(window: QWidget) -> bool:
    """Whether ``window`` currently carries the always-on-top flag.

    Reads the platform window when there is one: set_window_on_top applies the
    flag there, so the widget's cached copy can be a step behind.
    """
    handle = window.windowHandle()
    flags = handle.flags() if handle is not None else window.windowFlags()
    return bool(flags & Qt.WindowStaysOnTopHint)


# Platforms where what a window ASKS FOR only takes effect while it is
# unmapped. X11 carries always-on-top as the _NET_WM_STATE_ABOVE property,
# which Qt's xcb plugin only writes before mapping (updateNetWmStateBeforeMap),
# so setting the flag on a visible window changes nothing at all — the setting
# simply had no effect on Linux. The same rule reaches geometry: a window
# manager is free to refuse a mapped window's move, and honours the position on
# the next map (gui/subtitle_window.py _fit_to_screen, and the Tk overlay's
# withdraw/deiconify in _set_screen_position). Wayland has no always-on-top
# protocol at all; see gui/app.py, which asks for xcb first for that reason.
_REMAP_TO_RESTACK = ("xcb", "wayland")


def needs_remap() -> bool:
    """Whether this platform applies such a request only on the next map."""
    return QGuiApplication.platformName().split(":")[0] in _REMAP_TO_RESTACK


def set_window_on_top(window: QWidget, on_top: bool) -> None:
    """Toggle always-on-top without the window flashing.

    ``QWidget.setWindowFlag`` DESTROYS and recreates the native window, which
    hides the widget and repaints it from an empty surface — a white flash on
    every change, and the reason the old code had to re-show and re-apply the
    geometry afterwards. Setting the flag on the QWindow instead goes straight
    to the platform plugin (a SetWindowPos on Windows), so the surface, the
    geometry and the visibility all survive.

    A widget that has never been shown has no QWindow yet; there the widget
    call is both necessary and free of any flash.

    On X11 the cheap path is not available — see ``_REMAP_TO_RESTACK`` — so
    the window is re-created and shown again there. It is the flash Windows
    was spared, in exchange for the setting working at all.
    """
    if is_window_on_top(window) == on_top and not needs_remap():
        # Skipped only where the cached flag is the truth. On X11 it is not:
        # the state lives in a property the window manager owns, and a window
        # that has been re-mapped since (the overlay's geometry repair does
        # that) can carry the flag in Qt and sit at the bottom of the stack on
        # screen. Re-asserting costs a flash; believing a stale flag cost the
        # overlay entirely — the panel obeyed the setting and the subtitles
        # were left under the browser.
        return
    handle = window.windowHandle()
    if handle is None or needs_remap():
        visible = window.isVisible()
        window.setWindowFlag(Qt.WindowStaysOnTopHint, on_top)
        # setWindowFlag re-parents, which hides the widget; only a window that
        # was on screen gets put back, and through the class's own show() so
        # an overlay re-applies its geometry for the new stacking.
        if visible:
            window.show()
            # A window the toolkit just destroyed and rebuilt is a NEW window to
            # the WM, and it decides where to map it — under everything else,
            # for one that never takes focus. Callers apply this to the overlay
            # first and the panel last, so the panel still ends up in front.
            window.raise_()
        return
    handle.setFlags(_with_on_top(handle.flags(), on_top))


def _with_on_top(flags: Qt.WindowType, on_top: bool) -> Qt.WindowType:
    """``flags`` with the always-on-top bit set or cleared, and nothing else.

    **Clearing it goes through plain integers on purpose.** ``~`` on a PySide6
    flag enum does not invert all 32 bits: it complements within the enum's
    declared range, and ``~Qt.WindowStaysOnTopHint`` is ``0x01fbffff``. Anding
    with that silently drops every window flag above ``0x01ffffff`` —
    ``WindowCloseButtonHint`` (0x08000000) first among them. A window without
    that hint keeps its ✕ and Windows draws it **greyed out and inert**, on a
    focused window, for the rest of the process.

    That is the "sometimes the ✕ is greyed out" report, and it was never
    random: ``always_on_top_mode`` defaults to *When running*, so the first
    Stop of every session cleared the flag and took the close button with it.
    Setting the flag again does not bring it back — ``|`` only adds.
    """
    if on_top:
        return flags | Qt.WindowStaysOnTopHint
    return Qt.WindowType(int(flags) & ~int(Qt.WindowStaysOnTopHint))


# SWP_NOSIZE|SWP_NOMOVE|SWP_NOACTIVATE — restack and nothing else, and never
# take focus doing it.
_SWP_RESTACK_ONLY = 0x0013


def place_window_behind(window: QWidget, above: QWidget) -> bool:
    """Put ``window`` directly behind ``above``. True when it was applied.

    Qt can only lift a top-level window to the front of its band (``raise_``);
    ordering two of them against each other is a native call, which is why
    this is ctypes like the two helpers around it.

    What it is for: the overlay and the control panel are both always-on-top
    while a session runs, and always-on-top is a **band, not a rank** — inside
    it the order is whoever was raised last. Rather than lifting the panel
    back over the overlay again and again, the overlay is given one standing
    position: directly under the panel. Nothing else on the desktop moves, and
    the panel is never touched.

    Three things make it refuse, and each would do damage:

    * ``above`` **minimized or hidden** — Windows parks a minimized window at
      the bottom of the z-order, so inserting behind it would drop the overlay
      under every other application.
    * ``above`` **not itself always-on-top** — ``SetWindowPos`` takes the
      topmost-ness of the window it inserts after, so this would quietly
      demote the overlay out of the band and put the taskbar over it.
    * **No native window yet**, on either side.

    The caller falls back to ``raise_()``, which is the behaviour that was
    there before.
    """
    if sys.platform != "win32":
        return False
    if not above.isVisible() or above.isMinimized() or not is_window_on_top(above):
        return False
    if window.windowHandle() is None or above.windowHandle() is None:
        return False
    try:
        import ctypes

        return bool(
            ctypes.windll.user32.SetWindowPos(
                int(window.winId()),
                int(above.winId()),
                0,
                0,
                0,
                0,
                _SWP_RESTACK_ONLY,
            )
        )
    except Exception as exc:  # noqa: BLE001 - the caller has a fallback
        log(f"Could not place the overlay under the panel: {exc}", level="DEBUG")
        return False


# Windows paints a title bar from the SYSTEM light/dark preference and from
# nothing the application asks for, so a light-themed panel on a dark Windows
# kept a black caption bar joined to a white header. DWMWA_USE_IMMERSIVE_DARK_MODE
# is the only way to say otherwise — Qt exposes no API for it, which is why this
# is ctypes. The Tk tree set the same attribute; the Qt migration lost it.
# 20 since Windows 10 20H1, 19 before that.
_DWMWA_IMMERSIVE_DARK_MODE = 20
_DWMWA_IMMERSIVE_DARK_MODE_LEGACY = 19
# SWP_NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED — redraw the frame where it is,
# without moving, resizing or restacking the window. Windows does not repaint
# an already-painted caption bar just because the attribute changed.
_SWP_FRAME_CHANGED = 0x0027


def set_titlebar_dark(window: QWidget, dark: bool) -> None:
    """Match ``window``'s native title bar to the application theme.

    A no-op off Windows (every other platform already follows the application
    or has no client-settable caption) and a no-op before the native window
    exists — the attribute is set on an HWND, and asking for one early would
    force Qt to create the platform window ahead of time.

    Cosmetic in full: any failure is swallowed. The attribute is unsupported
    before Windows 10 1809, and a caption bar that stays the system colour is
    exactly the behaviour this replaces.
    """
    if sys.platform != "win32" or window.windowHandle() is None:
        return
    try:
        import ctypes

        hwnd = int(window.winId())
        value = ctypes.c_int(1 if dark else 0)
        dwm = ctypes.windll.dwmapi
        if dwm.DwmSetWindowAttribute(
            hwnd,
            _DWMWA_IMMERSIVE_DARK_MODE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        ):
            dwm.DwmSetWindowAttribute(
                hwnd,
                _DWMWA_IMMERSIVE_DARK_MODE_LEGACY,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, _SWP_FRAME_CHANGED)
    except Exception as exc:  # noqa: BLE001 - cosmetic; never break a window
        log(f"Title bar theming unavailable: {exc}", level="DEBUG")


class SegmentedControl(QWidget):
    """A joined row of mutually exclusive buttons — a CTkSegmentedButton.

    Emits ``changed`` with the selected index. Corner rounding is driven by a
    ``seg`` property (first/middle/last/only) that the stylesheet selects on,
    so the row reads as one pill rather than separate buttons.
    """

    changed = Signal(int)

    def __init__(
        self,
        labels: list[str],
        current: int = 0,
        parent=None,
        *,
        compact: bool = False,
    ):
        """``compact`` shrinks it to sit inline beside a checkbox.

        The full size is a row control's — the same height as a dropdown, so a
        selector that owns its own row reads as one. Next to a label it is far
        too heavy, and the padding is what makes it so rather than the text.
        """
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)  # joined, not separate buttons

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: list[QPushButton] = []

        last = len(labels) - 1
        for i, label in enumerate(labels):
            btn = QPushButton(label)
            btn.setObjectName("segment")
            btn.setCheckable(True)
            # A full-width row control like a dropdown, so it keeps the same
            # height — two pixels short read as a slightly different control.
            btn.setFixedHeight(SEGMENT_COMPACT_H if compact else CONTROL_H)
            btn.setProperty("compact", compact)
            btn.setCursor(Qt.PointingHandCursor)
            if len(labels) == 1:
                seg = "only"
            elif i == 0:
                seg = "first"
            elif i == last:
                seg = "last"
            else:
                seg = "middle"
            btn.setProperty("seg", seg)
            self._group.addButton(btn, i)
            layout.addWidget(btn)
            self._buttons.append(btn)

        if 0 <= current < len(self._buttons):
            self._buttons[current].setChecked(True)
        self._group.idClicked.connect(self.changed.emit)

    def set_labels(self, labels: list[str]) -> None:
        """Re-label the segments in place (a GUI-language switch).

        Rebuilding the control instead would drop the selection and every
        connection to it.
        """
        for btn, label in zip(self._buttons, labels, strict=False):
            btn.setText(label)

    def current_index(self) -> int:
        return self._group.checkedId()

    def set_current_index(self, index: int) -> None:
        if 0 <= index < len(self._buttons):
            self._buttons[index].setChecked(True)

    def set_enabled(self, enabled: bool) -> None:
        for btn in self._buttons:
            btn.setEnabled(enabled)


class Stepper(QWidget):
    """A −/+ pair with a value label, matching the Tk stepper rows.

    The Tk panel uses these (not a slider) for font size and scroll speed:
    both are adjusted mid-session by an operator who wants a predictable step,
    not a drag.
    """

    def __init__(self, on_decrease, on_increase, value_text: str = "", parent=None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self.minus = QPushButton("−")
        self.plus = QPushButton("+")
        for btn in (self.minus, self.plus):
            btn.setObjectName("stepper")
            # Square, at the height of the dropdown it shares a row with —
            # taller read as a different class of control.
            btn.setFixedSize(CONTROL_H, CONTROL_H)
            btn.setCursor(Qt.PointingHandCursor)
        self.minus.clicked.connect(on_decrease)
        self.plus.clicked.connect(on_increase)

        self.value = QLabel(value_text)
        self.value.setObjectName("stepper_value")
        self.value.setAlignment(Qt.AlignCenter)
        self.value.setMinimumWidth(64)

        layout.addWidget(self.minus)
        layout.addWidget(self.value)
        layout.addWidget(self.plus)
        # No trailing stretch: every caller puts the stepper at the right-hand
        # end of a row, so an internal stretch would only pad it away from the
        # edge it is meant to sit against.

    def set_value_text(self, text: str) -> None:
        self.value.setText(text)


class Dropdown(QComboBox):
    """A combo box that paints its own chevron.

    The stylesheet flattens ``QComboBox::drop-down`` to kill the platform's
    bevelled drop-down button, and that removes the arrow with it. Restoring
    the arrow in CSS is not possible here: ``image:`` only takes a file or a
    compiled resource, and the zero-box/transparent-border triangle trick
    renders as a filled rectangle once a ``QProxyStyle`` is installed (probed,
    not assumed). Painting it is three lines and always right.

    Also carries the size policy every dropdown in the app wants: without
    ``AdjustToMinimumContentsLengthWithIcon`` a combo demands its longest entry
    and the whole window refuses to be made narrow.
    """

    _ARROW_BOX = 26  # must match the drop-down width in the stylesheet
    # Longest popup before it scrolls. The language, model and device lists run
    # to a dozen-plus entries, and a popup that tall covers the window it
    # belongs to.
    _MAX_VISIBLE_ITEMS = 6

    def __init__(self, items: list[str] | None = None, parent=None):
        super().__init__(parent)
        if items:
            self.addItems(items)
        # An explicit item view, so the popup is a plain list on every
        # platform and the stylesheet's `QComboBox QAbstractItemView` rules
        # are what draws it. Left to itself, a platform style may hand back a
        # menu-like or native view instead (see _ControlStyle.styleHint).
        self.setView(QListView())
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMaxVisibleItems(self._MAX_VISIBLE_ITEMS)
        # Picking an entry ends the interaction; keeping the accent focus ring
        # afterwards reads as "still editing".
        self.activated.connect(lambda _index: self.clearFocus())
        # Long entries — device names above all — are elided both in the
        # closed box and in the popup once the window is squeezed narrow, and
        # two inputs from the same chip then differ only in the tail Qt cut
        # off. Carry the full text as a tooltip so hovering reveals it.
        #
        # Only when it is actually cut off. A tooltip that repeats a label you
        # can already read in full teaches that hovering sometimes says
        # nothing; the rule worth learning is "hover reveals what I cannot
        # read". Measuring costs ~42 us and the panel holds ten dropdowns, so
        # a full re-measure is 0.42 ms against a resize that already costs
        # ~14.7 ms — under 3%, and not worth a cache with a staleness trap.
        self.currentIndexChanged.connect(self._sync_closed_tooltip)
        self._sync_closed_tooltip()

    def _fits(self, text: str, available: int) -> bool:
        return self.fontMetrics().horizontalAdvance(text) <= available

    def _row_text_inset(self) -> int:
        """Left padding the stylesheet puts before a popup row's text.

        A row is clipped by the viewport, but its text does not start at the
        viewport's edge — `QAbstractItemView::item` carries horizontal
        padding. Asked of the style rather than hardcoded, so restyling the
        popup cannot silently make this wrong.
        """
        view = self.view()
        index = self.model().index(0, 0)
        opt = QStyleOptionViewItem()
        opt.initFrom(view)
        opt.rect = view.visualRect(index)
        text = view.style().subElementRect(QStyle.SE_ItemViewItemText, opt, view)
        return max(0, text.left() - opt.rect.left())

    def _closed_text_width(self) -> int:
        """Width the closed box has for text, arrow and padding excluded."""
        opt = QStyleOptionComboBox()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, self
        ).width()

    def _sync_closed_tooltip(self) -> None:
        # The closed box shows the current entry; a tooltip there needs the
        # full text of exactly that one, and only if the box cannot show it.
        text = self.currentText()
        self.setToolTip("" if self._fits(text, self._closed_text_width()) else text)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # The same entry elides or does not depending on the width, and the
        # width is exactly what changes when the window is squeezed.
        super().resizeEvent(event)
        self._sync_closed_tooltip()

    def showPopup(self) -> None:  # noqa: N802 - Qt API
        # Row tooltips are decided here rather than on insert: how much of a
        # row fits depends on the viewport, which does not exist until the
        # popup is up. Hence super() first, then measure.
        super().showPopup()
        available = self.view().viewport().width() - self._row_text_inset()
        for i in range(self.count()):
            text = self.itemText(i)
            self.setItemData(
                i, None if self._fits(text, available) else text, Qt.ToolTipRole
            )

    def setItemText(self, index: int, text: str) -> None:  # noqa: N802 - Qt API
        super().setItemText(index, text)
        if index == self.currentIndex():
            self._sync_closed_tooltip()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        """Never change the selection by scrolling over a closed combo.

        Qt's default silently switches language, model or audio device when the
        wheel passes over one — trivial to do while scrolling the panel, and
        the setting it changes is not obviously connected to the gesture.

        Ignoring the event hands the gesture to the scroll area behind, so the
        page scrolls instead (verified with a real OS wheel). The open popup is
        a separate widget and keeps its own wheel scrolling.
        """
        event.ignore()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        from gui.theme import current_colors

        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pen = QPen(
            QColor(colors["border"] if not self.isEnabled() else colors["muted"]), 1.7
        )
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(pen)
        cx = self.width() - self._ARROW_BOX / 2 - 1
        cy = self.height() / 2
        half = 4.0
        painter.drawPolyline(
            [
                QPointF(cx - half, cy - half / 2),
                QPointF(cx, cy + half / 2),
                QPointF(cx + half, cy - half / 2),
            ]
        )
        painter.end()


class Slider(QSlider):
    """A horizontal slider the wheel does not touch.

    Same rule as Dropdown: the panel scrolls, and a slider that happens to be
    under the pointer would take the gesture instead. Both of ours drive the
    audience overlay live — window height and backdrop opacity — so a stray
    wheel resizes or fades what the room is looking at, mid-session. Dragging
    and the arrow keys still work; the gesture goes to the page behind.
    """

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt API
        event.ignore()


class AudioLevelBar(QWidget):
    """Segmented input-level meter — the Qt twin of ``gui/audio_level_bar.py``.

    A plain progress bar can only use one colour at a time; an audio meter is
    easier to read when its green/amber/red zones stay in place while the fill
    moves through them, so the three zones are painted directly.
    """

    # Zone boundaries on the GUI's -60..0 dBFS scale, as in the Tk widget.
    GREEN_END = 0.70  # -18 dBFS
    RED_START = 5.0 / 6.0  # -10 dBFS

    GREEN = "#37B24D"
    WARNING = "#F08C00"
    DANGER = "#E03131"

    # The zones are also washed faintly across the EMPTY part of the track, so
    # someone sitting in the green can see how much headroom is left before
    # amber and red rather than discovering the boundaries by clipping.
    ZONE_GHOST_ALPHA = 60

    def __init__(self, height: int = 12, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    @staticmethod
    def band_span(left: int, width: int, start: float, end: float) -> tuple[int, int]:
        """Pixel range ``[x0, x1)`` for the fraction ``start``..``end``.

        Both edges are rounded the same way, so consecutive zones share a
        boundary EXACTLY. Rounding the width instead and padding it by a pixel
        — the obvious way to close a rounding gap — makes every band reach one
        pixel into the next. Invisible for the opaque fill, but the translucent
        zone map composited twice there and drew a seam between amber and red.
        """
        return round(left + width * start), round(left + width * end)

    def set_value(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        # Repainting 20x a second for an unchanged reading is pure waste; the
        # Tk meter learned the same lesson (PR #29).
        if abs(value - self._value) < 0.004:
            return
        self._value = value
        self.update()

    def value(self) -> float:
        return self._value

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        from gui.theme import current_colors

        # Read at paint time rather than cached: a theme switch then needs no
        # bookkeeping, and a caller passing a stale theme cannot desync it.
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = self.rect().adjusted(0, 0, -1, -1)
        radius = rect.height() / 2

        painter.setPen(QColor(colors["border"]))
        painter.setBrush(QBrush(QColor(colors["panel_soft"])))
        painter.drawRoundedRect(rect, radius, radius)

        painter.setClipPath(self._rounded_path(rect, radius))
        painter.setPen(Qt.NoPen)
        zones = (
            (0.0, self.GREEN_END, self.GREEN),
            (self.GREEN_END, self.RED_START, self.WARNING),
            (self.RED_START, 1.0, self.DANGER),
        )

        def band(start: float, end: float, colour: QColor) -> None:
            x0, x1 = self.band_span(rect.left(), rect.width(), start, end)
            if x1 <= x0:
                return
            painter.setBrush(QBrush(colour))
            painter.drawRect(x0, rect.top(), x1 - x0, rect.height())

        # The zone map first, at low opacity. Only amber and red are ghosted:
        # washing the green zone too made a silent meter read as an already
        # 70%-full bar, which is worse than showing no map at all.
        for start, end, colour in zones[1:]:
            ghost = QColor(colour)
            ghost.setAlpha(self.ZONE_GHOST_ALPHA)
            band(start, end, ghost)

        # ...then the live level on top, at full strength.
        for start, end, colour in zones:
            filled = min(self._value, end)
            if filled <= start:
                break
            band(start, filled, QColor(colour))

        # The bands are drawn over the rounded outline, so restore it.
        painter.setClipping(False)
        painter.setPen(QColor(colors["border"]))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(rect, radius, radius)

    @staticmethod
    def _rounded_path(rect, radius):
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path


class _ClickableRow(QWidget):
    """A plain row that reports clicks — the header of a collapsible card."""

    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class Card(QFrame):
    """A titled section card — the Qt twin of ``WidgetFactoryMixin._section_card``.

    ``body`` is the layout callers fill; the symbol badge + title row above it
    is built here so every card in the panel shares one look. The symbol sits
    in its own rounded accent-coloured tile rather than being glued in front of
    the title, which is what makes the Tk cards readable at a glance.

    ``collapsible=True`` adds the ▾/▴ arrow and makes the whole header a toggle
    (the Advanced card).
    """

    toggled = Signal(bool)

    # Padding on all four sides, plus the gap the header keeps to the body.
    # Qt drops that gap along with the hidden body, so a collapsed card is
    # symmetric without any margin juggling.
    _PAD = 16
    _BODY_GAP = 10

    def __init__(
        self,
        symbol: str,
        title: str,
        parent=None,
        *,
        collapsible: bool = False,
        expanded: bool = True,
    ):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self._PAD, self._PAD, self._PAD, self._PAD)
        outer.setSpacing(self._BODY_GAP)

        header = _ClickableRow()
        header_row = QHBoxLayout(header)
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(12)

        self.symbol_label = QLabel(symbol)
        self.symbol_label.setObjectName("card_symbol")
        self.symbol_label.setFixedSize(44, 44)
        self.symbol_label.setAlignment(Qt.AlignCenter)
        # setVisible AFTER the widget has a parent, always: a parentless widget
        # shown is a TOP-LEVEL WINDOW, which is what the little boxes flashing
        # across the screen before the panel opened were.
        header_row.addWidget(self.symbol_label)
        self.symbol_label.setVisible(bool(symbol))

        self.title_label = QLabel(title)
        self.title_label.setObjectName("card_title")
        header_row.addWidget(self.title_label)
        header_row.addStretch(1)

        self.arrow_label = QLabel("▾")
        self.arrow_label.setObjectName("card_arrow")
        header_row.addWidget(self.arrow_label)
        self.arrow_label.setVisible(collapsible)

        self.header = header_row
        self._header_widget = header
        # Slack above the header, off while the card is open. A card given more
        # height than it needs puts the difference into its trailing stretch,
        # which is right for an open card (content stays top-aligned) but hangs
        # a collapsed one's title off the top edge with a gap underneath. While
        # collapsed the two stretches share it and the title sits centred.
        outer.addStretch(0)
        outer.addWidget(header)

        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addWidget(self.content)
        self._outer = outer
        self._collapsible = collapsible
        self._expanded = True

        # Connected whatever the current mode: set_expanded coerces a
        # non-collapsible card back open, so the click is inert rather than
        # needing the connection to be made and broken with set_collapsible.
        header.clicked.connect(lambda: self.set_expanded(not self._expanded))
        if collapsible:
            header.setCursor(Qt.PointingHandCursor)
            self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self._expanded

    def is_collapsible(self) -> bool:
        return self._collapsible

    def set_expanded(self, expanded: bool) -> None:
        # A card that cannot be collapsed is always open, so a header click
        # (which asks for the opposite of the current state) is inert without
        # needing a guard of its own.
        expanded = bool(expanded) or not self._collapsible
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self.content.setVisible(expanded)
        self._outer.setStretch(0, 0 if expanded else 1)
        self.arrow_label.setText("▴" if expanded else "▾")
        self.toggled.emit(expanded)

    def set_collapsible(self, collapsible: bool) -> None:
        """Turn the header toggle on or off.

        Not collapsible means always open: when the card is the only thing in
        its column, a collapsed header strip would leave that column empty.
        """
        if collapsible == self._collapsible:
            return
        self._collapsible = collapsible
        self.arrow_label.setVisible(collapsible)
        self._header_widget.setCursor(
            Qt.PointingHandCursor if collapsible else Qt.ArrowCursor
        )
        if not collapsible:
            self.set_expanded(True)

    def add_stretch(self) -> None:
        """Absorb any height the card is given beyond its content.

        Without it, a card stretched to match its neighbours spreads its own
        rows apart; with it the content stays top-aligned and the slack sits at
        the bottom.
        """
        self._outer.addStretch(1)


class Expander(QWidget):
    """A full-width toggle button with a panel that opens under it.

    The Tk panel uses this shape for the subtitle-appearance controls: set-once
    values that would otherwise make the Display card twice as tall.

    ``collapsible=False`` keeps the panel open and renders the title as a plain
    section heading instead — the shape a card's other sections have, so a
    group that cannot be closed does not advertise a button.
    """

    toggled = Signal(bool)

    _PANEL_PAD = 12

    def __init__(
        self,
        title: str,
        parent=None,
        *,
        expanded: bool = False,
        collapsible: bool = True,
    ):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._title = title
        self.heading = QLabel(title)
        self.heading.setObjectName("section")
        # Parented before any setVisible: a parentless widget that is shown is
        # a top-level window (see Card).
        box.addWidget(self.heading)
        self.button = QPushButton()
        self.button.setObjectName("expander")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self.set_expanded(not self._expanded))
        box.addWidget(self.button)

        self.panel = QFrame()
        self.body = QVBoxLayout(self.panel)
        self.body.setSpacing(10)
        box.addWidget(self.panel)

        self._collapsible = collapsible
        self._expanded = True
        self.heading.setVisible(not collapsible)
        self.button.setVisible(collapsible)
        self._apply_panel_style()
        self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self._expanded

    def is_collapsible(self) -> bool:
        return self._collapsible

    def set_expanded(self, expanded: bool) -> None:
        expanded = bool(expanded) or not self._collapsible
        changed = expanded != self._expanded
        self._expanded = expanded
        self.panel.setVisible(expanded)
        self._refresh_button()
        if changed:
            # The card around it changes height, and in the 2-column layout
            # that decides whether the columns can still end level.
            self.toggled.emit(expanded)

    def set_collapsible(self, collapsible: bool) -> None:
        if collapsible == self._collapsible:
            return
        self._collapsible = collapsible
        self.button.setVisible(collapsible)
        self.heading.setVisible(not collapsible)
        self._apply_panel_style()
        self.set_expanded(self._expanded)  # forces it open when not collapsible

    def _apply_panel_style(self) -> None:
        """The soft tile marks the group as collapsible.

        Without the toggle there is nothing to mark, and the controls should
        sit flush in the card like every other section — so the tile and its
        padding go with the button.
        """
        pad = self._PANEL_PAD if self._collapsible else 0
        self.panel.setObjectName("mini" if self._collapsible else "")
        self.body.setContentsMargins(pad, pad, pad, pad)
        self.panel.style().unpolish(self.panel)
        self.panel.style().polish(self.panel)

    def _refresh_button(self) -> None:
        self.button.setText(f"{'▾' if self._expanded else '▸'}  {self._title}")


def field(
    label_text: str, widget: QWidget, spacing: int = 4, symbol: str | None = None
) -> QWidget:
    """A bold caption above its control, as every Tk card field is laid out.

    ``symbol`` prefixes the small glyph the Tk labels carry (▣ ◉ ⌁ → …); it is
    part of the caption text rather than a second widget so the pair wraps and
    aligns as one label.
    """
    holder = QWidget()
    box = QVBoxLayout(holder)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(spacing)
    caption = QLabel(f"{symbol}  {label_text}" if symbol else label_text)
    caption.setObjectName("field")
    box.addWidget(caption)
    box.addWidget(widget)
    holder.caption = caption  # so callers can re-translate it
    return holder


def warning_box(text: str) -> QFrame:
    """A bordered, warning-coloured callout (wizard caveats, disclaimer)."""
    box = QFrame()
    box.setObjectName("warning")
    layout = QVBoxLayout(box)
    layout.setContentsMargins(14, 10, 14, 10)
    label = QLabel(f"⚠  {text}")
    label.setObjectName("warning_text")
    label.setWordWrap(True)
    layout.addWidget(label)
    box.label = label
    return box

"""Shared Qt controls that mirror the CustomTkinter ones.

Qt has no equivalent of ``CTkSegmentedButton``, and the Tk panel uses it for
every either/or choice (themes, window style, and the two 3-way selectors
added in PR #22). Rebuilding it here keeps the Qt tree's arrangement identical
to the UI users already know, rather than substituting dropdowns.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class SegmentedControl(QWidget):
    """A joined row of mutually exclusive buttons — a CTkSegmentedButton.

    Emits ``changed`` with the selected index. Corner rounding is driven by a
    ``seg`` property (first/middle/last/only) that the stylesheet selects on,
    so the row reads as one pill rather than separate buttons.
    """

    changed = Signal(int)

    def __init__(self, labels: list[str], current: int = 0, parent=None):
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
            btn.setFixedSize(46, 46)
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
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMaxVisibleItems(self._MAX_VISIBLE_ITEMS)
        # Picking an entry ends the interaction; keeping the accent focus ring
        # afterwards reads as "still editing".
        self.activated.connect(lambda _index: self.clearFocus())

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
        from gui_qt.theme import current_colors

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
        from gui_qt.theme import current_colors

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
        outer.addWidget(header)

        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(10)
        outer.addWidget(self.content)
        self._outer = outer
        self._collapsible = collapsible
        self._expanded = True

        if collapsible:
            header.setCursor(Qt.PointingHandCursor)
            header.clicked.connect(lambda: self.set_expanded(not self._expanded))
            self.set_expanded(expanded)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        if not self._collapsible:
            return
        self._expanded = expanded
        self.content.setVisible(expanded)
        self.arrow_label.setText("▴" if expanded else "▾")
        self.toggled.emit(expanded)

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
    """

    def __init__(self, title: str, parent=None, *, expanded: bool = False):
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._title = title
        self.button = QPushButton()
        self.button.setObjectName("expander")
        self.button.setCursor(Qt.PointingHandCursor)
        self.button.clicked.connect(lambda: self.set_expanded(not self._expanded))
        box.addWidget(self.button)

        self.panel = QFrame()
        self.panel.setObjectName("mini")
        self.body = QVBoxLayout(self.panel)
        self.body.setContentsMargins(12, 12, 12, 12)
        self.body.setSpacing(10)
        box.addWidget(self.panel)

        self._expanded = not expanded  # so set_expanded always applies
        self.set_expanded(expanded)

    def set_title(self, title: str) -> None:
        self._title = title
        self._refresh_button()

    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self.panel.setVisible(expanded)
        self._refresh_button()

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

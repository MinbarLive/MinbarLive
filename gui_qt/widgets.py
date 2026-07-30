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
        layout.addStretch(1)

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

    def __init__(self, items: list[str] | None = None, parent=None):
        super().__init__(parent)
        if items:
            self.addItems(items)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)

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

    def __init__(self, height: int = 12, parent=None):
        super().__init__(parent)
        self._value = 0.0
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

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

        if self._value <= 0.0:
            return
        painter.setClipPath(self._rounded_path(rect, radius))
        painter.setPen(Qt.NoPen)
        zones = (
            (0.0, self.GREEN_END, self.GREEN),
            (self.GREEN_END, self.RED_START, self.WARNING),
            (self.RED_START, 1.0, self.DANGER),
        )
        for start, end, colour in zones:
            filled = min(self._value, end)
            if filled <= start:
                break
            x0 = rect.left() + rect.width() * start
            x1 = rect.left() + rect.width() * filled
            painter.setBrush(QBrush(QColor(colour)))
            painter.drawRect(round(x0), rect.top(), round(x1 - x0) + 1, rect.height())

    @staticmethod
    def _rounded_path(rect, radius):
        from PySide6.QtGui import QPainterPath

        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        return path


class Card(QFrame):
    """A titled section card — the Qt twin of ``WidgetFactoryMixin._section_card``.

    ``body`` is the layout callers fill; the symbol + title row above it is
    built here so every card in the panel shares one look.
    """

    def __init__(self, symbol: str, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel(f"{symbol}  {title}" if symbol else title)
        self.title_label.setObjectName("card_title")
        header.addWidget(self.title_label)
        header.addStretch(1)
        self.header = header
        outer.addLayout(header)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)
        self._outer = outer

    def add_stretch(self) -> None:
        self._outer.addStretch(1)


def field(label_text: str, widget: QWidget, spacing: int = 4) -> QWidget:
    """A bold caption above its control, as every Tk card field is laid out."""
    holder = QWidget()
    box = QVBoxLayout(holder)
    box.setContentsMargins(0, 0, 0, 0)
    box.setSpacing(spacing)
    caption = QLabel(label_text)
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


class LabelledSlider(QWidget):
    """A slider with its value label ABOVE it, as the Tk height control is."""

    def __init__(self, slider, value_text: str = "", parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.value = QLabel(value_text)
        self.value.setObjectName("slider_value")
        self.value.setAlignment(Qt.AlignLeft)
        layout.addWidget(self.value)
        layout.addWidget(slider)

    def set_value_text(self, text: str) -> None:
        self.value.setText(text)

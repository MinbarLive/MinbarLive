"""Shared Qt controls that mirror the CustomTkinter ones.

Qt has no equivalent of ``CTkSegmentedButton``, and the Tk panel uses it for
every either/or choice (themes, window style, and the two 3-way selectors
added in PR #22). Rebuilding it here keeps the Qt tree's arrangement identical
to the UI users already know, rather than substituting dropdowns.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
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

"""The control panel's responsive card grid (issue #48).

Three column groups — A = Controls + Display, B = Translation flow,
C = Advanced — arranged into 1, 2 or 3 columns by the width available, and
then made to end level. A group is the reflow's atom, so a tall card never
pads a short one out with dead space.

Split out of ``gui/control_panel.py`` because it is the one part of that file
with a real boundary: cards and a width in, a column count and a set of
heights out. Nothing here reads the panel's ~50 widget attributes, which is
what makes it testable without building a control panel (see
``tests/test_card_grid.py``); the card BUILDERS deliberately stayed behind for
the opposite reason.

The spacer discipline is the fiddly part and it lives in ``add_column``: each
column carries a spacer above and below its cards, and which of the two (or
the card itself) absorbs the slack is what decides how a column ends level.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# Width thresholds for the second and third column. Measured against what the
# cards actually need: test_a_column_is_never_narrower_than_its_cards_need
# holds _COL2_MIN_W above two columns' minimum hints plus the grid's chrome.
_COL2_MIN_W = 800
_COL3_MIN_W = 1030

# The largest difference the two-column levelling will fill, so two columns
# end on one line (see level_two_column_bottoms). Comfortably above the tens
# of pixels their content happens to differ by, and well below the ~240 an
# opened subtitle-appearance section adds.
_LEVEL_FILL_MAX_PX = 140

_GRID_SPACING = 18


class CardGrid(QObject):
    """Owns the grid layout, its three column groups and their levelling.

    A ``QObject`` so the queued levelling can name it as the timer's context
    (see ``level_two_column_bottoms``); parent it to the panel and Qt drops
    the pending call when the panel goes.
    """

    def __init__(self, host: QWidget, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.grid = QGridLayout(host)
        self.grid.setContentsMargins(18, 14, 18, 18)
        self.grid.setHorizontalSpacing(_GRID_SPACING)
        self.grid.setVerticalSpacing(_GRID_SPACING)

        self.columns: list[QWidget] = []
        self._boxes: list[QVBoxLayout] = []
        for _ in range(3):
            widget = QWidget()
            box = QVBoxLayout(widget)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(_GRID_SPACING)
            # A spacer above and below every column's cards.
            box.addStretch(0)
            box.addStretch(1)
            self.columns.append(widget)
            self._boxes.append(box)

        # (box, last card) per column — the card that takes up the slack.
        self.tails: list[tuple[QVBoxLayout, object]] = []
        self.count: int | None = None
        self._level_queued = False

    @property
    def col_a(self) -> QWidget:
        return self.columns[0]

    @property
    def col_b(self) -> QWidget:
        return self.columns[1]

    @property
    def col_c(self) -> QWidget:
        return self.columns[2]

    def add_column(self, index: int, *cards) -> None:
        """Fill column ``index``, bottom card last.

        Inserted before the trailing spacer, and the last card gets its own
        trailing stretch so that when it is the one taking the slack its
        content stays top-aligned rather than spreading.
        """
        box = self._boxes[index]
        for card in cards:
            box.insertWidget(box.count() - 1, card)
        tail = cards[-1]
        tail.add_stretch()
        self.tails.append((box, tail))

    # ── the reflow ───────────────────────────────────────────────────────
    def column_count(self, width: int, log_open: bool) -> int:
        """How many columns ``width`` affords."""
        # With the log open the sidebar is pinned narrow, so the cards get one
        # column — the same trade the Tk panel makes.
        if log_open:
            return 1
        if width <= 1:
            return 2  # nothing to measure yet; the default window shows two
        if width >= _COL3_MIN_W:
            return 3
        if width >= _COL2_MIN_W:
            return 2
        return 1

    def relayout(self, width: int, log_open: bool, force: bool = False) -> int | None:
        """Arrange the columns for ``width``.

        Returns the new column count if it changed, else ``None`` — the caller
        moves the Advanced collapse in or out on that signal, and does it after
        this returns so the re-entrant relayout its toggle triggers finds a
        grid that is already consistent.
        """
        cols = self.column_count(width, log_open)
        if cols == self.count and not force:
            # Cheap, and the only thing a plain resize can change: a card's
            # content may have grown since the columns were last arranged.
            self.level_two_column_bottoms()
            return None
        changed = cols != self.count
        self.count = cols
        for widget in self.columns:
            self.grid.removeWidget(widget)
        for i in range(3):
            self.grid.setColumnStretch(i, 0)

        if cols == 1:
            for i, widget in enumerate(self.columns):
                self.grid.addWidget(widget, i, 0)
            self.grid.setColumnStretch(0, 1)
        elif cols == 2:
            # A spans both rows so its height does not push C away from B.
            self.grid.addWidget(self.columns[0], 0, 0, 2, 1)
            self.grid.addWidget(self.columns[1], 0, 1)
            self.grid.addWidget(self.columns[2], 1, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
        else:
            for i, widget in enumerate(self.columns):
                self.grid.addWidget(widget, 0, i)
                self.grid.setColumnStretch(i, 1)
        for widget in self.columns:
            widget.setVisible(True)
        # The scroll area makes the host at least viewport-tall, and without
        # somewhere for that surplus to go the card row swallowed it — the
        # cards grew to the bottom of the window instead of stopping at the
        # tallest one. One stretched row absorbs it.
        #
        # In the 2-column layout that row is row 1 — the one holding Advanced —
        # and it does a second job there: column A spans both rows, so its
        # height (which changes whenever the subtitle-appearance expander
        # opens) has to go somewhere. Stretched, row 1 takes all of it and
        # row 0 stays exactly as tall as the card in it, which is what keeps
        # Advanced sitting still under Translation flow instead of sliding
        # down the window.
        used_rows = 3 if cols == 1 else 1
        for row in range(4):
            self.grid.setRowStretch(row, 1 if row == used_rows else 0)
        # Only side by side in three columns: in the L-shaped 2-column layout
        # levelling this way means padding Advanced away from the card above
        # it, and that gap grows every time column A does. Two columns end
        # level a different way — see level_two_column_bottoms.
        self.set_equal_column_heights(cols >= 3)
        self.level_two_column_bottoms()
        return cols if changed else None

    def set_equal_column_heights(self, equal: bool) -> None:
        """Side by side in three columns, the cards should end level.

        Three ragged bottom edges read as unfinished. In the 1- and 2-column
        layouts each card keeps its natural height instead: stacked, levelling
        has nothing to level, and in the L-shaped 2-column layout it only pads
        Advanced away from the card above it.

        An OPEN card takes the slack itself (its own trailing stretch keeps its
        content top-aligned). A COLLAPSED one must not — stretching a header
        strip into a tall empty box is worse than a ragged edge — so the spacer
        ABOVE it takes the slack instead and the strip sits on the baseline.

        Every stretch factor has to be set explicitly: a spacer from
        ``addStretch`` is expansive whatever its factor, so zeroing one alone
        still let it swallow the height and the card grew by nothing.
        """
        for box, card in self.tails:
            fill = equal and card.is_expanded()
            lead = 1 if (equal and not fill) else 0
            box.setStretch(0, lead)  # leading spacer: bottom-aligns the card
            box.setStretch(box.indexOf(card), 1 if fill else 0)
            box.setStretch(box.count() - 1, 0 if equal else 1)
            card.setSizePolicy(
                QSizePolicy.Preferred,
                QSizePolicy.Expanding if fill else QSizePolicy.Preferred,
            )

    def level_two_column_bottoms(self) -> None:
        """Queue the two-column levelling for once the layout has settled.

        Hiding or showing a widget invalidates size hints through a posted
        event, so anything reading them in the same call gets the PREVIOUS
        layout's numbers — the padding would then be one toggle behind.
        Coalesced, and the handler schedules nothing, so this cannot become
        the idle-requeueing correction loop that froze the Tk panel (PR #43).

        ``self`` as the timer's context, so Qt drops the pending call if the
        grid is destroyed first. Without it the levelling still ran against a
        dead panel — and because it calls ``sendPostedEvents``, it did so from
        inside Qt's event delivery, which came back as a PySide type error on
        the panel's own ``eventFilter`` and, once, an access violation. A GUI
        language switch rebuilds the panel, so this is reachable outside tests.
        """
        if self._level_queued:
            return
        self._level_queued = True
        QTimer.singleShot(0, self, self._apply_two_column_levelling)

    def _apply_two_column_levelling(self) -> None:
        """Make the two columns end on one line.

        Their natural heights are whatever their cards happen to add up to, so
        the bottom borders land a handful of pixels apart — which reads as a
        mistake rather than as a layout. The SHORTER column's last card takes
        the difference; a Card keeps its content top-aligned, so the padding
        lands below it, invisible.

        Unless that card is COLLAPSED, and then it must not grow at all: a
        header strip inflated into a tall empty box is exactly what a collapsed
        card exists to avoid. The spacer ABOVE it takes the slack instead, so
        the strip keeps its own height and the two columns still end on one
        line — the rule set_equal_column_heights already applies at three
        columns.

        Only a small difference, though: when one column is a whole opened
        subtitle-appearance section taller, inflating a card by that much is
        worse than a ragged edge — and pushing Advanced down to meet it is
        exactly the dead gap this layout exists to avoid.

        Each card's own ``sizeHint`` ignores the minimum set here, so the
        measurement never sees the previous correction: running this twice
        gives the same answer.
        """
        self._level_queued = False
        if not self.tails:
            return
        # Deliver the invalidations a hidden/shown widget posted, so the hints
        # below describe the layout as it is now and not as it was one toggle
        # ago. A 0-timer alone does not guarantee they have been processed.
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        (display_box, display), (advanced_box, advanced) = self.tails[0], self.tails[2]
        if self.count != 2:
            for box, card in ((display_box, display), (advanced_box, advanced)):
                card.setMinimumHeight(0)
                self._pad_above(box, 0)
            return
        gap = self.grid.verticalSpacing()
        left = self.natural_height(display_box)
        right = self.natural_height(self.tails[1][0]) + gap + self.natural_height(
            advanced_box
        )
        if left > right:
            (shorter, shorter_box), (taller, taller_box) = (
                (advanced, advanced_box),
                (display, display_box),
            )
        else:
            (shorter, shorter_box), (taller, taller_box) = (
                (display, display_box),
                (advanced, advanced_box),
            )
        extra = abs(left - right)
        if extra > _LEVEL_FILL_MAX_PX:
            extra = 0
        taller.setMinimumHeight(0)
        self._pad_above(taller_box, 0)
        collapsed = extra and not shorter.is_expanded()
        self._pad_above(shorter_box, extra if collapsed else 0)
        shorter.setMinimumHeight(
            0 if collapsed or not extra else shorter.sizeHint().height() + extra
        )

    @staticmethod
    def _pad_above(box, pixels: int) -> None:
        """Set the height of a column's leading spacer.

        Excluded from natural_height (which counts widgets only), so a second
        pass measures the same columns and lands on the same number — no
        feedback loop.
        """
        spacer = box.itemAt(0).spacerItem() if box.count() else None
        if spacer is None:
            return
        spacer.changeSize(0, pixels, QSizePolicy.Minimum, QSizePolicy.Fixed)
        box.invalidate()

    @staticmethod
    def natural_height(box) -> int:
        """A column's height from its cards' own hints — spacers excluded, so
        the stretch that fills a tall window does not count as content."""
        heights = [
            box.itemAt(i).widget().sizeHint().height()
            for i in range(box.count())
            if box.itemAt(i).widget() is not None
        ]
        return sum(heights) + max(0, len(heights) - 1) * box.spacing()

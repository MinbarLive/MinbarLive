"""The responsive card grid, tested without a control panel (issue #48).

The point of the split: this is cards plus a width in, a column count and a
set of heights out, so it can be driven with three throwaway cards instead of
a whole panel. ``tests/test_gui.py`` still covers the grid as the panel wires
it — these cover the algorithm.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from gui.card_grid import (  # noqa: E402
    _COL2_MIN_W,
    _COL3_MIN_W,
    _LEVEL_FILL_MAX_PX,
    CardGrid,
)


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


class FakeCard(QWidget):
    """A Card as the grid uses one: a height hint, an expanded state, a stretch.

    The height is a ``sizeHint``, not ``setFixedHeight``: the grid measures
    hints (``natural_height``) and answers by setting a minimum, so a fixed
    height would both hide the measurement and pre-load the answer.
    """

    def __init__(self, height: int, expanded: bool = True) -> None:
        super().__init__()
        self._expanded = expanded
        self._hint = QSize(200, height)
        self.stretched = False

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self._hint

    def is_expanded(self) -> bool:
        return self._expanded

    def add_stretch(self) -> None:
        self.stretched = True


@pytest.fixture
def grid(qt_app):
    made: list[CardGrid] = []

    def _make(*heights: int, expanded: tuple[bool, ...] = (True, True, True)):
        host = QWidget()
        g = CardGrid(host, parent=host)
        for i, (h, e) in enumerate(zip(heights, expanded, strict=True)):
            g.add_column(i, FakeCard(h, e))
        g._host = host  # keep the host alive for the test's lifetime
        made.append(g)
        return g

    yield _make
    for g in made:
        g._host.deleteLater()


class TestColumnCount:
    """The thresholds, which are the whole reflow decision."""

    @pytest.mark.parametrize(
        ("width", "expected"),
        [
            (400, 1),
            (_COL2_MIN_W - 1, 1),
            (_COL2_MIN_W, 2),
            (_COL3_MIN_W - 1, 2),
            (_COL3_MIN_W, 3),
            (2000, 3),
        ],
    )
    def test_width_decides_the_columns(self, grid, width, expected):
        assert grid(100, 100, 100).column_count(width, log_open=False) == expected

    def test_an_open_log_pins_it_to_one_column(self, grid):
        # The sidebar is narrow while the log shares the window, whatever the
        # window's own width says.
        assert grid(100, 100, 100).column_count(2000, log_open=True) == 1

    def test_an_unmeasured_width_defaults_to_two(self, grid):
        # Before the window is on screen there is nothing to measure, and the
        # default window shows two — starting at one would reflow on first show.
        assert grid(100, 100, 100).column_count(0, log_open=False) == 2


class TestTwoColumnFloor:
    """``_COL2_MIN_W`` is a floor, not the answer.

    The cards' minimums come from the font engine, so the same panel needs
    658 px in Arabic, 758 in German and 869 on the Linux runner — against one
    constant of 800. In the band above the constant and below the need the grid
    used to go two-column into a space that could not hold two columns, and the
    card area scrolls vertically only, so the overflow was cut off with no way
    to reach it.
    """

    @staticmethod
    def _widen(g, pixels: int) -> None:
        """Give every card a real minimum width, as a themed card has."""
        for _box, card in g.tails:
            card.setMinimumWidth(pixels)

    def test_cards_wider_than_the_constant_raise_the_threshold(self, grid, qt_app):
        # 390 + 390 + 36 of margin + 18 of spacing = 834, above the constant and
        # still below _COL3_MIN_W so the three-column branch stays out of it.
        g = grid(100, 100, 100)
        self._widen(g, 390)
        qt_app.processEvents()
        assert g.two_column_min_width() == 834
        assert g.column_count(833, log_open=False) == 1
        assert g.column_count(834, log_open=False) == 2

    def test_narrow_cards_never_lower_it(self, grid, qt_app):
        # The other direction, and the reason this is a max: cards that fit in
        # less than the constant must not drop the threshold to meet them. The
        # constant is also what a pre-show panel falls back to, where the cards
        # are unpolished and hint at ~50 px against a real 449.
        g = grid(100, 100, 100)
        self._widen(g, 120)
        qt_app.processEvents()
        assert g.two_column_min_width() == _COL2_MIN_W

    def test_the_widest_of_the_stacked_columns_decides(self, grid, qt_app):
        # Two columns put B above C, so column 1 has to hold the WIDER of them.
        # Reading B alone lets a wide Advanced card clip. Both candidate answers
        # have to clear the floor or the max() hides the difference: C decides at
        # 954 px, B alone would say 854, and the constant is 800.
        g = grid(100, 100, 100)
        self._widen(g, 400)
        g.tails[2][1].setMinimumWidth(500)
        qt_app.processEvents()
        assert g.two_column_min_width() == 400 + 500 + 36 + 18

    def test_the_threshold_does_not_move_with_the_arrangement(self, grid, qt_app):
        # A threshold measured from the layout it produces oscillates: the grid
        # widens into the next arrangement, re-measures bigger, falls back, and
        # re-measures smaller. Column C is the one that really does move (three
        # columns pin the Advanced card open, worth ~11 px), which is why
        # _COL3_MIN_W stays a plain constant and only this one is measured.
        g = grid(100, 100, 100)
        self._widen(g, 390)
        qt_app.processEvents()
        seen = set()
        for width in (1400, 900, 520):
            g.relayout(width, log_open=False)
            qt_app.processEvents()
            seen.add(g.two_column_min_width())
        assert len(seen) == 1, f"the threshold moved with the arrangement: {seen}"


class TestRelayout:
    def test_it_reports_the_count_only_when_it_changes(self, grid):
        g = grid(100, 100, 100)
        assert g.relayout(2000, log_open=False) == 3, "first arrangement"
        assert g.relayout(2000, log_open=False) is None, "same width, no change"
        assert g.relayout(900, log_open=False) == 2

    def test_force_rearranges_at_an_unchanged_width(self, grid):
        # What a card's expander toggle needs: the width did not move, the
        # content did.
        g = grid(100, 100, 100)
        g.relayout(2000, log_open=False)
        assert g.relayout(2000, log_open=False, force=True) is None
        assert g.count == 3

    def test_every_column_is_placed_exactly_once(self, grid):
        g = grid(100, 100, 100)
        for width in (400, 900, 2000):
            g.relayout(width, log_open=False)
            placed = [
                g.grid.itemAt(i).widget() for i in range(g.grid.count())
            ]
            assert sorted(map(id, placed)) == sorted(map(id, g.columns)), width

    def test_one_stretched_row_absorbs_a_tall_window(self, grid):
        # Without it the cards grew to the bottom of the window instead of
        # stopping at the tallest one.
        g = grid(100, 100, 100)
        for width, row in ((400, 3), (900, 1), (2000, 1)):
            g.relayout(width, log_open=False)
            stretched = [r for r in range(4) if g.grid.rowStretch(r)]
            assert stretched == [row], f"{width}px stretched rows {stretched}"


class TestEqualColumnHeights:
    def test_an_open_tail_card_takes_the_slack_itself(self, grid):
        g = grid(100, 100, 100)
        g.set_equal_column_heights(True)
        for box, card in g.tails:
            assert box.stretch(box.indexOf(card)) == 1, "card does not fill"
            assert box.stretch(0) == 0, "the spacer above took it instead"

    def test_a_collapsed_tail_card_is_bottom_aligned_instead(self, grid):
        # Stretching a header strip into a tall empty box is worse than a
        # ragged edge, so the spacer above it takes the slack.
        g = grid(100, 100, 100, expanded=(False, False, False))
        g.set_equal_column_heights(True)
        for box, card in g.tails:
            assert box.stretch(box.indexOf(card)) == 0, "collapsed card inflated"
            assert box.stretch(0) == 1, "not bottom-aligned"

    def test_unequal_returns_every_column_to_its_natural_height(self, grid):
        g = grid(100, 100, 100)
        g.set_equal_column_heights(True)
        g.set_equal_column_heights(False)
        for box, card in g.tails:
            assert box.stretch(0) == 0
            assert box.stretch(box.indexOf(card)) == 0
            assert box.stretch(box.count() - 1) == 1, "trailing spacer inert"


class TestTwoColumnLevelling:
    """Two columns end on one line, and how depends on the shorter card."""

    @staticmethod
    def _level(g, qt_app):
        g.level_two_column_bottoms()
        qt_app.processEvents()

    @staticmethod
    def _lead_height(box) -> int:
        return box.itemAt(0).spacerItem().sizeHint().height()

    def test_a_short_open_card_grows_to_meet_the_other_column(self, grid, qt_app):
        # Column A is 300 tall; B + C is 100 + 18 + 100 = 218. The shorter
        # side is the right, and its open tail card takes the 82px difference
        # — comfortably inside the _LEVEL_FILL_MAX_PX the grid will absorb.
        g = grid(300, 100, 100)
        g.relayout(900, log_open=False)
        self._level(g, qt_app)
        assert g.tails[2][1].minimumHeight() > 100, "advanced did not grow"
        assert self._lead_height(g.tails[2][0]) == 0, "padded instead of grown"

    def test_a_short_collapsed_card_is_padded_above_not_inflated(
        self, grid, qt_app
    ):
        g = grid(300, 100, 100, expanded=(True, True, False))
        g.relayout(900, log_open=False)
        self._level(g, qt_app)
        assert g.tails[2][1].minimumHeight() == 0, "collapsed card inflated"
        assert self._lead_height(g.tails[2][0]) > 0, "not padded above"

    def test_a_difference_too_large_to_absorb_is_declined(self, grid, qt_app):
        # An opened subtitle-appearance section is ~240px. Inflating a card by
        # that much is worse than a ragged edge.
        g = grid(100 + _LEVEL_FILL_MAX_PX + 200, 100, 100)
        g.relayout(900, log_open=False)
        self._level(g, qt_app)
        assert g.tails[2][1].minimumHeight() == 0
        assert self._lead_height(g.tails[2][0]) == 0

    def test_running_it_twice_lands_on_the_same_answer(self, grid, qt_app):
        # The leading spacer is excluded from natural_height, so the second
        # pass measures what the first one did — no feedback loop.
        g = grid(300, 100, 100)
        g.relayout(900, log_open=False)
        self._level(g, qt_app)
        first = g.tails[2][1].minimumHeight()
        self._level(g, qt_app)
        assert g.tails[2][1].minimumHeight() == first

    def test_outside_two_columns_it_undoes_itself(self, grid, qt_app):
        g = grid(300, 100, 100)
        g.relayout(900, log_open=False)
        self._level(g, qt_app)
        assert g.tails[2][1].minimumHeight() > 100
        g.relayout(2000, log_open=False)
        self._level(g, qt_app)
        assert g.tails[2][1].minimumHeight() == 0, "levelling outlived 2 columns"
        assert self._lead_height(g.tails[2][0]) == 0

    def test_the_queued_pass_is_coalesced(self, grid, qt_app):
        g = grid(300, 100, 100)
        g.relayout(900, log_open=False)
        g.level_two_column_bottoms()
        assert g._level_queued
        g.level_two_column_bottoms()  # a second ask must not queue a second pass
        qt_app.processEvents()
        assert not g._level_queued

    def test_a_destroyed_grid_drops_its_queued_pass(self, grid, qt_app):
        # The timer names the grid as its context, so Qt cancels the call
        # rather than running it against destroyed cards.
        g = grid(300, 100, 100)
        g.relayout(900, log_open=False)
        g.level_two_column_bottoms()
        g._host.deleteLater()
        qt_app.processEvents()  # would raise on a dead card if it still ran


class TestMinimumWidth:
    """The floor a window must not go below, or the cards are clipped and the
    vertical scroll bar draws on top of what is left of them."""

    @staticmethod
    def _with_minimums(g, *widths: int):
        for (_box, card), width in zip(g.tails, widths, strict=True):
            card.setMinimumWidth(width)
        return g

    def _expected(self, g, widest: int) -> int:
        margins = g.grid.contentsMargins()
        return widest + margins.left() + margins.right()

    def test_it_is_the_widest_column_plus_the_grid_margins(self, grid):
        g = self._with_minimums(grid(100, 100, 100), 200, 380, 150)
        assert g.minimum_width() == self._expected(g, 380)

    @pytest.mark.parametrize("width", [400, 900, 2000])
    def test_the_arrangement_does_not_change_it(self, grid, width):
        # The trap this exists to avoid: the host's own minimumSizeHint at
        # three columns is all three minimums added together, so a window
        # floored at it can never be dragged narrow again — it stays three
        # columns wide because it is three columns wide.
        g = self._with_minimums(grid(100, 100, 100), 200, 380, 150)
        g.relayout(width, log_open=False)
        assert g.minimum_width() == self._expected(g, 380)


class TestNaturalHeight:
    def test_it_counts_cards_and_their_spacing_only(self, grid):
        g = grid(100, 100, 100)
        box = g.tails[0][0]
        # One card, plus the two spacers, which must not count.
        assert CardGrid.natural_height(box) == 100

    def test_padding_above_does_not_feed_back_into_the_measurement(self, grid):
        g = grid(100, 100, 100)
        box = g.tails[0][0]
        CardGrid._pad_above(box, 60)
        assert CardGrid.natural_height(box) == 100, "the pad counted as content"

"""The integrated-window sizing rule (gui/modal_host.clamped_panel_size) and
the sizes the individual panels ask it for.

Pure-function tests only — the ModalHost itself needs real windows and is
covered by the scripted AppGUI drive-throughs.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui.announce_view import ANNOUNCE_PANEL_MAX_W, ANNOUNCE_WINDOW_W
from gui.batch_view import BATCH_PANEL_MAX_W, BATCH_WINDOW_W
from gui.history_view import (
    HISTORY_LIST_W_MAX,
    HISTORY_LIST_W_MIN,
    HISTORY_NARROW_W,
    HISTORY_PANEL_MAX_H,
    HISTORY_PANEL_MAX_W,
    HISTORY_WINDOW_H,
    HISTORY_WINDOW_W,
)
from gui.modal_host import (
    MIN_PANEL_H,
    MIN_PANEL_W,
    PANEL_FRACTION,
    clamped_panel_size,
)
from gui.settings_view import SETTINGS_PANEL_MAX_W, SETTINGS_WINDOW_W
from utils.api_key_manager import DIALOG_PANEL_GROWTH, DIALOG_W, KEY_DIALOG_W


class TestClampedPanelSize:
    def test_design_size_when_main_is_large(self):
        # A big main window never stretches the panel past its design size.
        assert clamped_panel_size(500, 620, 1600, 1000) == (500, 620)

    def test_clamps_to_fraction_of_small_main(self):
        w, h = clamped_panel_size(900, 560, 700, 540)
        assert w == int(700 * PANEL_FRACTION)
        assert h == int(540 * PANEL_FRACTION)

    def test_axes_clamp_independently(self):
        # Wide-but-flat main: only the height needs clamping.
        w, h = clamped_panel_size(500, 620, 1600, 600)
        assert w == 500
        assert h == int(600 * PANEL_FRACTION)

    def test_usability_floor(self):
        # A degenerate main window can't shrink a panel into nothing.
        assert clamped_panel_size(500, 620, 100, 80) == (MIN_PANEL_W, MIN_PANEL_H)

    def test_small_design_stays_untouched(self):
        # Dialogs smaller than the clamp keep their exact size.
        assert clamped_panel_size(440, 200, 1200, 800) == (440, 200)


class TestPanelSizes:
    """What each window asks for: view windows grow with the main window up to
    a cap that suits their content, notifications only take a fixed step up."""

    # A maximized window on the monitor the design was drawn for.
    MAIN = (2560, 1400)

    def test_history_grows_far_past_its_windowed_size(self):
        w, h = clamped_panel_size(
            HISTORY_PANEL_MAX_W, HISTORY_PANEL_MAX_H, *self.MAIN
        )
        assert w > HISTORY_WINDOW_W * 1.5
        assert h > HISTORY_WINDOW_H * 1.5
        # ...but not past the cap, and never past the main window.
        assert (w, h) == (HISTORY_PANEL_MAX_W, HISTORY_PANEL_MAX_H)
        assert w < self.MAIN[0] and h < self.MAIN[1]

    def test_history_still_shrinks_into_a_small_window(self):
        w, h = clamped_panel_size(HISTORY_PANEL_MAX_W, HISTORY_PANEL_MAX_H, 600, 500)
        assert (w, h) == (int(600 * PANEL_FRACTION), int(500 * PANEL_FRACTION))

    def test_form_shaped_panels_stay_column_shaped(self):
        # Settings/batch/announcement grow, but nowhere near the full width —
        # their content is a single column of dropdowns.
        for cap, windowed in (
            (SETTINGS_PANEL_MAX_W, SETTINGS_WINDOW_W),
            (BATCH_PANEL_MAX_W, BATCH_WINDOW_W),
            (ANNOUNCE_PANEL_MAX_W, ANNOUNCE_WINDOW_W),
        ):
            w, _h = clamped_panel_size(cap, 600, *self.MAIN)
            assert windowed < w <= cap
            assert w < self.MAIN[0] * PANEL_FRACTION / 2

    def test_notification_dialogs_do_not_scale_with_the_main_window(self):
        for design in (DIALOG_W, KEY_DIALOG_W):
            asked = int(design * DIALOG_PANEL_GROWTH)
            small, _h = clamped_panel_size(asked, 200, 900, 700)
            large, _h = clamped_panel_size(asked, 200, *self.MAIN)
            assert small == large == asked
            assert 1.05 < asked / design < 1.15


class TestHistoryResponsiveBounds:
    """The narrow-layout constants have to be consistent with each other, or
    the viewer switches to single-pane at a width where it wasn't needed."""

    def test_list_bounds_ordered(self):
        assert HISTORY_LIST_W_MIN < HISTORY_LIST_W_MAX

    def test_both_panes_fit_at_the_narrow_threshold(self):
        # At the breakpoint the list is at most its minimum share and still
        # leaves the transcript more room than the list itself takes.
        assert HISTORY_NARROW_W - HISTORY_LIST_W_MIN > HISTORY_LIST_W_MIN


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

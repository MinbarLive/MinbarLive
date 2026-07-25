"""The integrated-window sizing rule (gui/modal_host.clamped_panel_size).

Pure-function tests only — the ModalHost itself needs real windows and is
covered by the scripted AppGUI drive-throughs.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui.modal_host import (
    MIN_PANEL_H,
    MIN_PANEL_W,
    PANEL_FRACTION,
    clamped_panel_size,
)


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

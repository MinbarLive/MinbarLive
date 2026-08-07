"""The dBFS scale behind the input-level meter.

Toolkit-free on purpose, like ``gui/palette.py`` and ``gui/i18n.py``: the panel
and the wizard both draw the meter, and the mapping is arithmetic that should
be testable without building either of them.
"""

from __future__ import annotations

# The scale the GUI maps dBFS onto, shared so the control panel and the setup
# wizard show the same meter. The zone colours live with the widget that
# paints them (``gui.widgets.AudioLevelBar``), not here.
LEVEL_FLOOR_DBFS = -60.0


def level_fill(rms_dbfs: float) -> float:
    """Map a dBFS reading onto the bar's 0..1 fill."""

    span = -LEVEL_FLOOR_DBFS
    return max(0.0, min(1.0, (rms_dbfs - LEVEL_FLOOR_DBFS) / span))

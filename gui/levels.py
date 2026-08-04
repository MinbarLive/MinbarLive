"""Input-level scale shared by both GUI trees.

Toolkit-free on purpose, like ``gui/palette.py`` and ``gui/i18n.py``:
the Qt panel and wizard need the dBFS mapping, and reaching into
``gui/audio_level_bar.py`` for it would pull CustomTkinter — and with it all
of tkinter — into a process that has no Tk in it.
"""

from __future__ import annotations

# Conventional audio-meter zone colours (readable in both themes) and the
# scale the GUI maps dBFS onto. Shared so the control panel and the setup
# wizard show the same meter.
LEVEL_GREEN = "#37B24D"
LEVEL_WARNING = "#F08C00"
LEVEL_DANGER = "#E03131"
LEVEL_FLOOR_DBFS = -60.0


def level_fill(rms_dbfs: float) -> float:
    """Map a dBFS reading onto the bar's 0..1 fill."""

    span = -LEVEL_FLOOR_DBFS
    return max(0.0, min(1.0, (rms_dbfs - LEVEL_FLOOR_DBFS) / span))

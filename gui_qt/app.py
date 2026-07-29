"""Qt application bootstrap.

Entered from ``main.py --qt``. Tk and Qt cannot share a process, so exactly
one GUI tree runs per launch and the flag chooses which.

Note what is absent: no ``enable_windows_dpi_awareness`` call, no manual
scaling setup, no reveal-delay dance. Qt is per-monitor DPI aware and paints
before showing, so the ``_reveal_when_drawn`` machinery (and the Windows-only
``<Map>`` event that made the whole panel invisible on macOS) has no analogue.
"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from gui_qt.control_panel import ControlPanel
from gui_qt.theme import apply_theme
from utils.settings import load_settings


def run(controller) -> int:
    """Run the Qt GUI against ``controller``; returns the process exit code."""
    app = QApplication(sys.argv)
    app.setApplicationName("MinbarLive")

    settings = load_settings()
    apply_theme(app, settings.theme_mode)

    panel = ControlPanel(controller)
    panel.show()
    return app.exec()

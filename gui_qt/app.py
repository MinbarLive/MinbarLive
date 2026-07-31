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
from gui_qt.icons import app_icon
from gui_qt.onboarding import run_onboarding
from gui_qt.theme import apply_theme
from utils.settings import load_settings


def run(controller) -> int:
    """Run the Qt GUI against ``controller``; returns the process exit code."""
    # Reused when one exists: the "already running" dialog runs before this and
    # creates it, and Qt allows only one QApplication per process.
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MinbarLive")
    # Application-wide and before the first window, so every window — including
    # the overlay, which nobody else sets an icon on — inherits it the moment
    # its native window is created. Without this Qt shows its own default.
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    apply_theme(app, load_settings().theme_mode)

    # First-run setup, on the same QApplication and before the control panel,
    # so the chosen language and theme apply from the start.
    if not run_onboarding(app, controller):
        return 0

    # Re-read: the wizard writes language, theme, provider and device.
    settings = load_settings()
    apply_theme(app, settings.theme_mode)

    panel = ControlPanel(controller)
    panel.show()
    return app.exec()

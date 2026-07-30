"""Qt application bootstrap.

Entered from ``main.py --qt``. Tk and Qt cannot share a process, so exactly
one GUI tree runs per launch and the flag chooses which.

Note what is absent: no ``enable_windows_dpi_awareness`` call, no manual
scaling setup, no reveal-delay dance. Qt is per-monitor DPI aware and paints
before showing, so the ``_reveal_when_drawn`` machinery (and the Windows-only
``<Map>`` event that made the whole panel invisible on macOS) has no analogue.
"""

from __future__ import annotations

import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from config import ICON_PATH, ICON_PATH_PNG
from gui_qt.control_panel import ControlPanel
from gui_qt.onboarding import run_onboarding
from gui_qt.theme import apply_theme
from utils.settings import load_settings


def _application_icon() -> QIcon | None:
    """The shipped app icon, preferring the .ico (it carries several sizes, so
    Windows picks a crisp one for the taskbar)."""
    for path in (ICON_PATH, ICON_PATH_PNG):
        if path and os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return None


def run(controller) -> int:
    """Run the Qt GUI against ``controller``; returns the process exit code."""
    app = QApplication(sys.argv)
    app.setApplicationName("MinbarLive")
    # Application-wide, so every window and the taskbar button inherit it —
    # without this Qt shows its own default icon everywhere.
    icon = _application_icon()
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

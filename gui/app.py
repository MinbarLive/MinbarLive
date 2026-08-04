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

from gui.control_panel import ControlPanel
from gui.icons import app_icon
from gui.onboarding import run_onboarding
from gui.theme import apply_theme
from utils.logging import log
from utils.settings import load_settings


def run(controller) -> int:
    """Run the Qt GUI against ``controller``; returns the process exit code."""
    # Reused when one exists: the "already running" dialog runs before this and
    # creates it, and Qt allows only one QApplication per process.
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("MinbarLive")
    # Which platform plugin actually loaded decides what the overlay can do:
    # under Wayland a window cannot be positioned or kept on top at all
    # (gui/platform_setup.py asks for xcb first because of it), so a bug
    # report about either has to start here.
    platform = app.platformName()
    log(f"Qt platform plugin: {platform}", level="INFO")
    if platform.startswith("wayland"):
        # Said out loud, because the symptoms look like application bugs: the
        # overlay appears in the middle of the screen, lowering its height
        # walks the footer UP instead of bringing the top edge down, and the
        # always-on-top setting does nothing. All three are the compositor's
        # call under Wayland, and none of them is reachable from here.
        warning = (
            "Wayland session: the subtitle overlay cannot be placed on a chosen "
            "screen or kept on top. Install libxcb-cursor0 so the X11 (xcb) "
            "plugin can load."
        )
        log(warning, level="WARNING")
        # Also on stderr, right under Qt's own "Could not load the Qt platform
        # plugin xcb" line. utils.logging writes to a file and to the panel's
        # log view; whoever is running from source is reading the terminal, and
        # this warning is worth nothing if it is not where the symptom is.
        print(f"MinbarLive: {warning}", file=sys.stderr)
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

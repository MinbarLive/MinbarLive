"""Process-level Qt platform choices, made before the QApplication exists.

Deliberately importable on its own: it must run before the first Qt window,
which under ``--qt`` is the already-running dialog, and it has no business
pulling the control panel in that early. Nothing here imports PySide6 either —
these are environment variables the platform plugins read when they load.
"""

from __future__ import annotations

import os
import sys

# Qt reads this as an ordered list and takes the first plugin that loads.
#
# Why xcb first on Linux: a Wayland client cannot place its own windows and
# cannot ask to stay on top — there is no protocol for either. The subtitle
# overlay is nothing but those two things, so under Wayland the compositor
# centres it on the screen and the always-on-top setting does nothing at all,
# which is exactly what the overlay does there today. Through XWayland (xcb)
# the window manager honours both, as it does on a real X11 session.
#
# ``wayland`` stays in the list as the fallback: a session without XWayland
# should still start, with those two limitations, rather than fail to launch.
# Setting QT_QPA_PLATFORM yourself overrides this entirely.
#
# The xcb plugin needs libxcb-cursor0 since Qt 6.5. Without it the plugin is
# found but refuses to load, and this list quietly lands on Wayland — with the
# two limitations above. gui_qt/app.py says so at startup rather than leaving
# a centred, never-on-top overlay to be puzzled over.
_LINUX_PLATFORM = "xcb;wayland"

# Every family we ask for is one this machine has (gui_qt/fonts.py filters
# them), so what is left of this warning is Qt reporting that none of them
# covers Devanagari, Bengali, Gurmukhi or Tamil — the scripts of the language
# names in our own dropdowns (हिन्दी, বাংলা, ਪੰਜਾਬੀ, தமிழ்). Falling through to
# a system font that does have them is exactly right, and there is nothing to
# act on: it is four lines per family per script on every launch, on stderr.
# Only the warnings from that one category, and only when the operator has not
# set rules of their own.
_LINUX_LOG_RULES = "qt.text.font.db.warning=false"


def prepare_qt_platform() -> None:
    """Pick the platform plugin order. Call before creating a QApplication."""
    if not sys.platform.startswith("linux"):
        return
    if not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = _LINUX_PLATFORM
    if not os.environ.get("QT_LOGGING_RULES"):
        os.environ["QT_LOGGING_RULES"] = _LINUX_LOG_RULES

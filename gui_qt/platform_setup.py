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
_LINUX_PLATFORM = "xcb;wayland"


def prepare_qt_platform() -> None:
    """Pick the platform plugin order. Call before creating a QApplication."""
    if sys.platform.startswith("linux") and not os.environ.get("QT_QPA_PLATFORM"):
        os.environ["QT_QPA_PLATFORM"] = _LINUX_PLATFORM

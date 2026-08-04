"""Process-level Qt platform choices, made before the QApplication exists.

Deliberately importable on its own: it must run before the first Qt window,
which under ``--qt`` is the already-running dialog, and it has no business
pulling the control panel in that early. Nothing here imports PySide6 either —
these are environment variables the platform plugins read when they load.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

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
# two limitations above. gui/app.py says so at startup rather than leaving
# a centred, never-on-top overlay to be puzzled over.
_LINUX_PLATFORM = "xcb;wayland"

# Every family we ask for is one this machine has (gui/fonts.py filters
# them), so what is left of this warning is Qt reporting that none of them
# covers Devanagari, Bengali, Gurmukhi or Tamil — the scripts of the language
# names in our own dropdowns (हिन्दी, বাংলা, ਪੰਜਾਬੀ, தமிழ்). Falling through to
# a system font that does have them is exactly right, and there is nothing to
# act on: it is four lines per family per script on every launch, on stderr.
# Only that one category, and only when the operator has not set rules of
# their own.
#
# The rule names the CATEGORY and no severity. ``qt.text.font.db.warning=false``
# was tried first and the lines still came out on Ubuntu: Qt does not document
# which severity carries "OpenType support missing", and a rule for the wrong
# one silences nothing. The whole-category form covers every severity it could
# be, which is what this needs — nothing in that category is actionable here.
_LINUX_LOG_RULES = "qt.text.font.db=false"


def linux_environment(env: Mapping[str, str]) -> dict[str, str]:
    """What a Linux launch adds to ``env``, leaving the operator's own alone.

    Split out from ``prepare_qt_platform`` so the decision can be checked
    anywhere: the alternative is faking ``sys.platform`` inside a test process,
    which this project has already been bitten by.
    """
    chosen: dict[str, str] = {}
    if not env.get("QT_QPA_PLATFORM"):
        chosen["QT_QPA_PLATFORM"] = _LINUX_PLATFORM
    if not env.get("QT_LOGGING_RULES"):
        chosen["QT_LOGGING_RULES"] = _LINUX_LOG_RULES
    return chosen


def prepare_qt_platform() -> None:
    """Pick the platform plugin order. Call before creating a QApplication."""
    if not sys.platform.startswith("linux"):
        return
    os.environ.update(linux_environment(os.environ))

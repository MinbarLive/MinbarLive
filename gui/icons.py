"""The window icon every Qt window carries.

The logo artwork is a vertical lockup — mark above "MinbarLive" above the
tagline. Windows draws the taskbar button at 24-32px, where the wordmark and
the tagline are a few dozen unreadable grey pixels: the pale smudge that reads
as "the logo is broken" next to neighbouring icons that are all compact marks.

So the runtime icon is built from the mark alone, which ``utils.icons`` already
extracts for the panel header, at the sizes Windows actually asks for. The
shipped ``MinbarLive.ico`` — the fallback here, and what the EXE and the
desktop shortcut use — is now the same mark at the same sizes, written by
``packaging/make_windows_icon.py``. It used to be the lockup, on the grounds
that a wordmark belongs on a file icon; the desktop shortcut sitting next to a
taskbar button showing something else settled that the other way.

The white-outlined artwork is the source: the taskbar and title bar follow the
system theme, and the plain navy mark disappears against a dark one while the
outlined variant reads on both.
"""

from __future__ import annotations

import io
import os

from PySide6.QtGui import QIcon, QPixmap

from config import ICON_PATH, ICON_PATH_PNG, ICON_PATH_PNG_ON_DARK
from utils.logging import log

# What Windows asks for: 16 and 20 in menus and the title bar, 24-32 on the
# taskbar (32 and 48 at 150-200% scaling), the rest for alt-tab and Explorer.
ICON_SIZES = (16, 20, 24, 32, 48, 64, 128, 256)

_cached: QIcon | None = None


def app_icon() -> QIcon | None:
    """The application icon, built once and shared by every window.

    Set on the QApplication before any window exists, so a window inherits it
    at creation and Windows never sees a button without one.
    """
    global _cached
    if _cached is None:
        _cached = _mark_icon() or _file_icon()
    return _cached


def _mark_icon() -> QIcon | None:
    """The logo's mark at every icon size, or None if it cannot be built."""
    path = ICON_PATH_PNG_ON_DARK
    if not os.path.exists(path):
        path = ICON_PATH_PNG
    if not os.path.exists(path):
        return None
    try:
        from utils.icons import square_marks

        icon = QIcon()
        for square in square_marks(path, ICON_SIZES):
            buffer = io.BytesIO()
            square.save(buffer, format="PNG")
            pixmap = QPixmap()
            if pixmap.loadFromData(buffer.getvalue(), "PNG"):
                icon.addPixmap(pixmap)
    except Exception as exc:  # noqa: BLE001 - fall back to the shipped .ico
        log(f"Window icon from the logo mark unavailable: {exc}", level="WARNING")
        return None
    return icon if not icon.isNull() else None


def _file_icon() -> QIcon | None:
    """The shipped icon file, preferring the .ico for its several sizes."""
    for path in (ICON_PATH, ICON_PATH_PNG):
        if path and os.path.exists(path):
            icon = QIcon(path)
            if not icon.isNull():
                return icon
    return None

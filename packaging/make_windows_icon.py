"""Regenerate ``public/MinbarLive.ico`` — the EXE's icon, and so the desktop
shortcut's, Explorer's and the taskbar's.

Run it when the logo artwork changes; the result is committed, because
``MinbarLive.spec`` reads the file at build time and a local
``pyinstaller MinbarLive.spec`` must work without a generation step.

    python packaging/make_windows_icon.py

**The mark alone, never the lockup.** The artwork is a vertical lockup — mark
above "MinbarLive" above the tagline — and Windows draws this icon at 16-48px,
where the wordmark and the tagline are a few dozen grey pixels: the pale smudge
the taskbar button used to show. ``gui/icons.py`` already builds the window
icon from the mark for that reason, and ``packaging/build-appimage.sh`` already
does the same for the Linux launcher. This is the third of the three, so the
desktop icon, the taskbar button and the header logo are finally one image.

The white-outlined variant is the source, as in ``gui/icons.py``: an icon lands
on whatever wallpaper the user has, and the plain navy mark disappears against
a dark one.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import ICON_PATH, ICON_PATH_PNG_ON_DARK  # noqa: E402
from gui.icons import ICON_SIZES  # noqa: E402
from utils.icons import square_marks  # noqa: E402


def main() -> None:
    squares = square_marks(ICON_PATH_PNG_ON_DARK, ICON_SIZES)
    # Pillow writes every size into one .ico from the largest image plus a
    # sizes list, but it RESAMPLES them itself — which is not the same as the
    # squares above, each scaled from the mark by its longer side. So the
    # largest is saved with the rest appended as prepared frames.
    largest, *rest = sorted(squares, key=lambda image: -image.width)
    largest.save(
        ICON_PATH, format="ICO", sizes=[(s, s) for s in ICON_SIZES], append_images=rest
    )
    print(f"{ICON_PATH}: {', '.join(str(size) for size in ICON_SIZES)}")


if __name__ == "__main__":
    main()

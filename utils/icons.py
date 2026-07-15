"""Window-icon helpers shared by every window.

Two cross-platform pitfalls (found via PR #1, dodosack):

- ``iconbitmap(.ico)`` is Windows-only — Linux Tk expects an XBM bitmap
  there and raises. Worse, several call sites defer it via ``after()``, so
  the exception fires inside a Tk callback instead of the guarding try.
- ``wm iconphoto`` with the raw 3200x3200 PNG asset exceeds the X11 maximum
  request size and aborts the whole process with a fatal BadLength error —
  the Linux startup crash. The PNG must be downscaled first.
"""

from __future__ import annotations

import sys
import tkinter as tk

# iconbitmap(.ico) works only on Windows; everywhere else use the PNG.
ICO_SUPPORTED = sys.platform.startswith("win")


def scaled_icon_photo(png_path: str, max_px: int = 64) -> tk.PhotoImage:
    """The PNG icon as a PhotoImage downscaled to at most ``max_px``."""
    img = tk.PhotoImage(file=png_path)
    factor = max(1, img.width() // max_px, img.height() // max_px)
    if factor > 1:
        img = img.subsample(factor, factor)
    return img

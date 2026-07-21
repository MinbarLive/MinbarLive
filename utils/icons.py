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


def logo_mark(png_path: str, height: int):
    """The logo's mark (the dome, without the lettering) at ``height`` px.

    Two things have to be trimmed off the shipped artwork. It sits in a lot
    of transparent padding (MinbarLive1.png fills ~40% of its 3200x3200), so
    drawing the file at a widget size would shrink the logo into the middle
    of an empty box. And it is a vertical lockup — mark above "MinbarLive"
    above the tagline — whose lettering is an illegible smudge at header
    size, right next to the real wordmark label.

    The cut is the emptiest pixel row between 55% and 80% of the artwork
    height (the gap under the mark's base line) rather than a fixed
    fraction: the two shipped variants put it at 0.69 and 0.71.
    """
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415 — only GUI callers need Pillow

    img = Image.open(png_path).convert("RGBA")
    box = img.getbbox()
    if box is not None:
        img = img.crop(box)

    ink = (np.array(img)[:, :, 3] > 8).sum(axis=1)
    low, high = int(len(ink) * 0.55), int(len(ink) * 0.80)
    if high > low:
        img = img.crop((0, 0, img.width, low + int(ink[low:high].argmin())))
        box = img.getbbox()
        if box is not None:
            img = img.crop(box)

    width = max(1, round(img.width * height / img.height))
    return img.resize((width, height), Image.LANCZOS)


def logo_photo(png_path: str, height: int, master) -> tk.Image:
    """:func:`logo_mark` as a PhotoImage bound to ``master``'s interpreter.

    The master is explicit on purpose. PhotoImage otherwise attaches itself
    to tkinter's *default* root, which is not necessarily the window drawing
    it — the onboarding wizard is created as the first root, and the GUI
    tests build one root per test. The image then lives in a different Tcl
    interpreter and Tk fails with ``image "pyimageN" doesn't exist``.
    (CTkImage has exactly that problem, which is why it is not used here.)
    """
    from PIL import ImageTk  # noqa: PLC0415 — only GUI callers need Pillow

    return ImageTk.PhotoImage(logo_mark(png_path, height), master=master)

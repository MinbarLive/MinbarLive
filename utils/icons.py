"""Logo artwork helpers, shared by the windows that draw it.

Toolkit-free by design: these return PIL images, and ``gui/icons.py`` turns
them into the ``QIcon`` every window carries. Nothing here may import a GUI
toolkit — the Tk helpers that used to live beside them went with
``utils/api_key_manager.py``, their only caller.

Pillow and numpy are imported inside the functions rather than at module level.
Only GUI callers need them, and importing them at module scope makes every
headless import of ``utils`` pay for it.
"""

from __future__ import annotations

from collections.abc import Sequence


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


def square_marks(png_path: str, sizes: Sequence[int]) -> list:
    """:func:`logo_mark` centred on a transparent square canvas per size.

    Window icons are square, and the mark is wider than tall (402x256 for the
    shipped artwork), so it is scaled by its longer side and centred rather
    than stretched. The mark is extracted once at the largest size and each
    smaller square resampled from that — extracting per size means reopening
    and rescanning the 3200x3200 source, which costs ~170 ms a time.
    """
    from PIL import Image  # noqa: PLC0415 — only GUI callers need Pillow

    mark = logo_mark(png_path, max(sizes))
    squares = []
    for size in sizes:
        scale = size / max(mark.width, mark.height)
        scaled = mark.resize(
            (max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
            Image.LANCZOS,
        )
        canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        canvas.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
        squares.append(canvas)
    return squares

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

    ``png_path`` is a mark-only asset — ``config.ICON_PATH_PNG`` or its
    on-dark twin — so all this does is trim the transparent padding and scale.

    **It used to carve the mark out of the full lockup here, and that was the
    bug behind the sliced circle** in the taskbar button, the .ico and the site
    header. The cut was the emptiest pixel row in the lower middle of the
    artwork, on the assumption that it lands in the gap under the mark's base
    line. It lands inside the mark instead: the base line is not the mark's
    lowest ink, because a node hangs below it, and the emptiest row is the one
    where only that node's traces are left. No horizontal cut works — the
    letter ascenders start above the node's bottom — so the split is done by
    connected component, once, in ``packaging/make_logo_assets.py``.
    """
    from PIL import Image  # noqa: PLC0415 — only GUI callers need Pillow

    img = Image.open(png_path).convert("RGBA")
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

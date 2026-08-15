"""Regenerate every derived logo asset — the app's marks and the website's.

Run from the repo root: ``python packaging/make_logo_assets.py``, then
``python packaging/make_windows_icon.py`` to rebuild ``public/MinbarLive.ico``
from the new mark.

**Why the mark is split off by connected component.** The lockup cannot be cut
with a horizontal line: the letter ascenders start *above* the mark's lowest node
(y=826 against y=840 in the plain artwork), so every row either slices the node
or keeps letter tops. Cutting at "the emptiest row" is what sliced the node in
the taskbar icon, the .ico and the site header — the emptiest row lands inside
the mark, where only the hanging traces carry ink. The mark is instead the
component holding the topmost ink, plus any component nested inside its box (the
inner arches).

**The artwork is not clipped.** Both lockups hold the right-hand node complete;
it only reads as cut once the image is trimmed to its bounding box, because the
circle is then tangent to the edge. Check a suspect crop by counting ink in its
last row: a tangent leaves ~20 px, a real cut leaves a chord of ~120.

**Why the white-stroke file is the source for the site.** It is the same lockup,
and its outline is removable — the stroke is white, the artwork is navy. The app
keeps both variants: plain for light backgrounds, outlined for dark ones.
"""

import os
import sys

import numpy as np
from PIL import Image
from scipy import ndimage

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PUBLIC = os.path.join(ROOT, "public")
ASSETS = os.path.join(ROOT, "docs", "assets")
PLAIN = os.path.join(PUBLIC, "MinbarLive1.png")
OUTLINED = os.path.join(PUBLIC, "MinbarLive_white_stroke.png")
FAVICON_SIZES = (16, 32, 48, 64, 128, 256)


def without_white_stroke(path: str) -> Image.Image:
    """The lockup with its white outline removed, on transparency.

    Alpha becomes "how navy is this pixel", and the colour is un-matted back off
    white so anti-aliased edges keep the artwork's navy instead of a pale blend.
    """
    src = Image.open(path).convert("RGBA")
    a = np.array(src).astype(np.float64)
    alpha = a[:, :, 3:4] / 255.0

    # Outside the shape the colour is undefined, so read everything as if it sat
    # on white paper.
    on_white = a[:, :, :3] * alpha + 255.0 * (1 - alpha)
    lum = on_white.mean(axis=2)
    navy_lum = np.percentile(lum[lum < 200], 2)
    navyness = np.clip((255.0 - lum) / (255.0 - navy_lum), 0, 1)

    n = np.maximum(navyness, 1e-6)[..., None]
    out = np.zeros_like(a)
    out[:, :, :3] = np.clip((on_white - 255.0 * (1 - n)) / n, 0, 255)
    out[:, :, 3] = navyness * 255.0
    return Image.fromarray(out.astype(np.uint8))


def mark_only(lockup: Image.Image) -> Image.Image:
    """``lockup`` with the wordmark and tagline removed."""
    trimmed = lockup.crop(lockup.getbbox())
    labels, count = ndimage.label(np.array(trimmed)[:, :, 3] > 8)
    boxes = ndimage.find_objects(labels)
    top = min(range(count), key=lambda i: boxes[i][0].start)
    my, mx = boxes[top]

    keep = np.zeros(labels.shape, dtype=bool)
    for i, (ys, xs) in enumerate(boxes):
        nested = (
            ys.start >= my.start
            and ys.stop <= my.stop
            and xs.start >= mx.start
            and xs.stop <= mx.stop
        )
        if i == top or nested:
            keep |= labels == i + 1

    out = np.array(trimmed)
    out[~keep] = 0
    mark = Image.fromarray(out)
    return mark.crop(mark.getbbox())


def framed_like(image: Image.Image, reference_path: str) -> Image.Image:
    """``image`` placed on the reference's canvas at the reference's ink box.

    The hero and footer size the logo by its file height, so a tighter crop would
    silently enlarge it. Matching the old file's geometry keeps removing the
    stroke a change of appearance only.
    """
    ref = Image.open(reference_path).convert("RGBA")
    box = ref.getbbox()
    ink = image.crop(image.getbbox()).resize(
        (box[2] - box[0], box[3] - box[1]), Image.LANCZOS
    )
    canvas = Image.new("RGBA", ref.size, (0, 0, 0, 0))
    canvas.paste(ink, (box[0], box[1]))
    return canvas


def square(image: Image.Image, size: int, margin: float = 0.06) -> Image.Image:
    """``image`` centred on a transparent square, scaled by its longer side."""
    inner = max(1, round(size * (1 - 2 * margin)))
    scale = inner / max(image.width, image.height)
    scaled = image.resize(
        (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
        Image.LANCZOS,
    )
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2))
    return canvas


def write(image: Image.Image, path: str, label: str) -> None:
    image.save(path, optimize=True)
    print(f"{label:22s} {str(image.size):12s} -> {os.path.relpath(path, ROOT)}")


def main() -> int:
    # The app draws the mark alone; both variants keep their own artwork, so the
    # outlined one is never the plain one with a stroke bolted on.
    write(
        mark_only(Image.open(PLAIN).convert("RGBA")),
        os.path.join(PUBLIC, "MinbarLive_mark.png"),
        "app mark",
    )
    outlined_mark = mark_only(Image.open(OUTLINED).convert("RGBA"))
    write(
        outlined_mark,
        os.path.join(PUBLIC, "MinbarLive_mark_on_dark.png"),
        "app mark (on dark)",
    )

    # The site wants the lockup without the outline, at the geometry the old
    # outlined file had so the hero and footer do not resize.
    lockup = without_white_stroke(OUTLINED)
    write(
        framed_like(lockup, OUTLINED),
        os.path.join(ASSETS, "MinbarLive_lockup.png"),
        "site lockup",
    )
    mark = mark_only(lockup)
    write(mark, os.path.join(ASSETS, "MinbarLive_mark.png"), "site mark")

    # The favicon is the OUTLINED mark, unlike the header beside it: a browser
    # tab bar follows the browser's theme, not the page's, and the navy mark
    # disappears into a dark one. The white outline is what keeps it readable
    # there, and it costs nothing on a light tab bar.
    icon_path = os.path.join(ROOT, "docs", "favicon.ico")
    frames = [square(outlined_mark, size) for size in FAVICON_SIZES]
    frames[-1].save(
        icon_path,
        format="ICO",
        sizes=[(s, s) for s in FAVICON_SIZES],
        append_images=frames[:-1],
    )
    print(f"{'site favicon':22s} {str(FAVICON_SIZES):12s} -> {os.path.relpath(icon_path, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

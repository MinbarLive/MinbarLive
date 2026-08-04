"""Font selection for the Qt GUI.

Deliberately small. The Tk tree needs a parallel module of shaping helpers
(``_reshape_rtl``, ``_ARABIC_BLOCK_RE``, ``_TK_HANDLES_ARABIC``,
``_TK_SHAPES_ARABIC``) because Tk cannot shape Arabic consistently across
platforms. Qt lays text out with HarfBuzz on every platform, so none of that
exists here: pass logical text straight to Qt and it shapes and bidi-orders it.

What remains are two real differences we still care about, both keyed off
``is_arabic_text``: Latin source/live lines render italic while Arabic does not
(Arabic has no italic tradition and Qt would synthesise an oblique), and each
script gets a family stack that actually covers it, per platform.
"""

from __future__ import annotations

import re
import sys
from functools import lru_cache

from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication

# Arabic block, plus the presentation-form ranges, so text that has been through
# a legacy reshaper (e.g. replayed history) classifies the same as fresh text.
_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")

# The Allah honorifics the translator inserts into otherwise-Latin lines. They
# are Arabic-block codepoints but render within plain Latin bounds, so counting
# them as "Arabic" would push German lines into the Arabic styling class.
_HONORIFIC_RE = re.compile(r"[ﷺﷻ]")


def is_arabic_text(text: str) -> bool:
    """True when ``text`` is genuinely Arabic-script, ignoring ﷺ/ﷻ honorifics."""
    return bool(_ARABIC_RE.search(_HONORIFIC_RE.sub("", text or "")))


# Interface stack per platform, best first. Naming a family this machine does
# not have costs real time and says so out loud — macOS printed "Populating
# font family aliases took 41 ms. Replace uses of missing font family
# "Segoe UI"" on every launch, because the Windows family was hardcoded for
# all three platforms.
_UI_FAMILIES = {
    "win32": ("Segoe UI", "Segoe UI Symbol"),
    "darwin": ("SF Pro Text", "Helvetica Neue", "Apple Symbols"),
}
_UI_FAMILIES_DEFAULT = ("Noto Sans", "DejaVu Sans")

# Arabic stack per platform. Not a nicety: Qt reports the metrics of the
# family it was ASKED for, and paints missing glyphs from a fallback family it
# was not — so an Arabic line drawn in "Noto Sans" (which has no Arabic at
# all) is measured against Latin ascent/descent and stacked as if its
# descenders did not exist. On Windows this list is the interface stack
# unchanged: Segoe UI covers Arabic, and the overlay's spacing was tuned on it.
_ARABIC_FAMILIES = {
    "win32": ("Segoe UI", "Segoe UI Symbol"),
    "darwin": ("SF Arabic", "Geeza Pro", "Al Bayan", "Helvetica Neue"),
}
_ARABIC_FAMILIES_DEFAULT = (
    "Noto Sans Arabic",
    "Noto Naskh Arabic",
    "DejaVu Sans",
)


@lru_cache(maxsize=8)
def _installed(families: tuple[str, ...]) -> list[str]:
    """``families`` minus the ones this machine does not have.

    Cached because it cannot change while the process runs, and because it is
    reached from paint code.
    """
    present = [name for name in families if QFontDatabase.hasFamily(name)]
    # Nothing matched: hand Qt the preferred name anyway rather than an empty
    # list, so it substitutes as it always did instead of losing the request.
    return present or [families[0]]


def _resolve(families: tuple[str, ...]) -> list[str]:
    """Installed subset of ``families``, or all of them before there is an app.

    The font database needs a QGuiApplication. Every real caller has one, but
    ``gui_qt.theme.stylesheet`` is also read by tests as a plain string — the
    unfiltered list is the right answer there, and deliberately not cached so
    the filtered one is still computed once the app exists.
    """
    if QGuiApplication.instance() is None:
        return list(families)
    return _installed(families)


def ui_families() -> list[str]:
    """Interface font stack for this platform, missing families removed."""
    return _resolve(_UI_FAMILIES.get(sys.platform, _UI_FAMILIES_DEFAULT))


def arabic_families() -> list[str]:
    """Arabic-first font stack for this platform."""
    return _resolve(_ARABIC_FAMILIES.get(sys.platform, _ARABIC_FAMILIES_DEFAULT))


def families_for(text: str) -> list[str]:
    """The stack to lay ``text`` out with, chosen by script."""
    return arabic_families() if is_arabic_text(text) else ui_families()


def ui_family() -> str:
    """The interface font family for this platform."""
    return ui_families()[0]


def subtitle_font(size_px: int, *, bold: bool = True, text: str = "") -> QFont:
    """Font for settled translation lines.

    ``text`` only selects the family stack — pass the line being drawn so an
    Arabic translation is measured with Arabic metrics.
    """
    f = QFont()
    f.setFamilies(families_for(text))
    f.setPixelSize(max(1, int(size_px)))
    f.setBold(bold)
    return f


def source_font(size_px: int, text: str) -> QFont:
    """Font for original-language / live lines.

    Latin renders italic and regular weight to separate it from the bold
    upright translation; Arabic stays upright.
    """
    f = QFont()
    f.setFamilies(families_for(text))
    f.setPixelSize(max(1, int(size_px)))
    f.setBold(False)
    f.setItalic(not is_arabic_text(text))
    return f

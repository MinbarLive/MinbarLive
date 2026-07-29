"""Font selection for the Qt GUI.

Deliberately small. The Tk tree needs a parallel module of shaping helpers
(``_reshape_rtl``, ``_ARABIC_BLOCK_RE``, ``_TK_HANDLES_ARABIC``,
``_TK_SHAPES_ARABIC``) because Tk cannot shape Arabic consistently across
platforms. Qt lays text out with HarfBuzz on every platform, so none of that
exists here: pass logical text straight to Qt and it shapes and bidi-orders it.

What remains is a real difference we still care about: Latin source/live lines
render italic while Arabic does not (Arabic has no italic tradition and Qt would
synthesise an oblique). See ``is_arabic_text``.
"""

from __future__ import annotations

import re
import sys

from PySide6.QtGui import QFont

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


def ui_family() -> str:
    """The interface font family for this platform."""
    if sys.platform == "win32":
        return "Segoe UI"
    if sys.platform == "darwin":
        return "SF Pro Text"
    return "Noto Sans"


def subtitle_font(size_px: int, *, bold: bool = True) -> QFont:
    """Font for settled translation lines."""
    f = QFont(ui_family())
    f.setPixelSize(max(1, int(size_px)))
    f.setBold(bold)
    return f


def source_font(size_px: int, text: str) -> QFont:
    """Font for original-language / live lines.

    Latin renders italic and regular weight to separate it from the bold
    upright translation; Arabic stays upright.
    """
    f = QFont(ui_family())
    f.setPixelSize(max(1, int(size_px)))
    f.setBold(False)
    f.setItalic(not is_arabic_text(text))
    return f

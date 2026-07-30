"""Tests for the RTL pre-shaping in gui.subtitle_window.

Windows Tk runs its own Arabic bidi+shaping pass over any string that still
contains Arabic-block codepoints. ؟ ، ؛ and Arabic-Indic digits have no
presentation forms and survive arabic_reshaper — feeding Tk our
already-reordered visual string then double-reorders it (reversed,
disconnected letters: the "breaks only with ؟/!" bug). In exactly those
cases _reshape_rtl must return the logical text and let Tk do the whole job.

macOS Tk goes further: it lays every string out with CoreText, which always
applies bidi AND shaping — even to the presentation forms the reshaper emits —
so there pre-shaping is wrong for *every* string, not just the ones with
surviving Arabic-block punctuation.
"""

import pytest

from gui import subtitle_window
from gui.subtitle_window import _ARABIC_BLOCK_RE, _ARABIC_SUPPORT, _reshape_rtl

pytestmark = pytest.mark.skipif(
    not _ARABIC_SUPPORT, reason="arabic_reshaper/python-bidi not installed"
)

PURE = "هل من خالق غير الله"
QMARK = "هل من خالق غير الله؟"
COMMA = "قال، ثم ذهب غير الله"
GERMAN = "O ihr Menschen, gedenkt der Gunst Allahs."


class TestTkNativeFallback:
    """On platforms where Tk itself handles Arabic (Windows)."""

    @pytest.fixture(autouse=True)
    def _tk_native(self, monkeypatch):
        # Pinned so the suite tests the Windows rule on a macOS machine too.
        monkeypatch.setattr(subtitle_window, "_TK_SHAPES_ARABIC", False)
        monkeypatch.setattr(subtitle_window, "_TK_HANDLES_ARABIC", True)

    def test_surviving_arabic_qmark_returns_logical_text(self):
        assert _reshape_rtl(QMARK) == QMARK

    def test_surviving_arabic_comma_returns_logical_text(self):
        assert _reshape_rtl(COMMA) == COMMA

    def test_pure_arabic_is_fully_shaped(self):
        # No U+06xx survivors => our visual string is safe for Tk and used.
        out = _reshape_rtl(PURE)
        assert out != PURE
        assert not _ARABIC_BLOCK_RE.search(out)

    def test_ascii_punctuation_does_not_force_fallback(self):
        # "!" / "?" / "." are not Arabic-block chars: shaped path stays.
        out = _reshape_rtl(PURE + "!")
        assert out != PURE + "!"
        assert not _ARABIC_BLOCK_RE.search(out)

    def test_non_arabic_text_unchanged(self):
        assert _reshape_rtl(GERMAN) == GERMAN


class TestPreShapedPath:
    """On platforms without native Tk Arabic (e.g. Linux/X11) the shaped
    visual string must always be used, survivors or not."""

    @pytest.fixture(autouse=True)
    def _no_tk_native(self, monkeypatch):
        monkeypatch.setattr(subtitle_window, "_TK_SHAPES_ARABIC", False)
        monkeypatch.setattr(subtitle_window, "_TK_HANDLES_ARABIC", False)

    def test_surviving_qmark_still_shaped(self):
        out = _reshape_rtl(QMARK)
        assert out != QMARK
        # the ؟ itself survives, but the letters are presentation forms
        assert "؟" in out

    def test_pure_arabic_shaped(self):
        assert _reshape_rtl(PURE) != PURE


class TestCoreTextPath:
    """macOS: CoreText shapes and reorders everything itself, so the logical
    text must be handed over untouched in every case."""

    @pytest.fixture(autouse=True)
    def _coretext(self, monkeypatch):
        monkeypatch.setattr(subtitle_window, "_TK_SHAPES_ARABIC", True)
        # False on purpose: the Windows rule must not be what saves us here.
        monkeypatch.setattr(subtitle_window, "_TK_HANDLES_ARABIC", False)

    @pytest.mark.parametrize("text", [PURE, QMARK, COMMA, GERMAN])
    def test_text_is_never_pre_shaped(self, text):
        assert _reshape_rtl(text) == text

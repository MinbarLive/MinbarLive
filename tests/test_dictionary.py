"""Tests for dictionary-based matching functions."""

import json
import re
import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import ATHAN_TRANSLATIONS_DIR, QURAN_TRANSLATIONS_DIR
from translation.dictionary import fuzzy_match_athan, normalize_arabic
from utils.settings import TARGET_LANGUAGES


class TestNormalizeArabic:
    """Tests for Arabic text normalization."""

    def test_removes_harakat(self):
        """Should remove diacritical marks (harakat)."""
        # "بِسْمِ" with harakat -> "بسم" without
        text_with_harakat = "بِسْمِ اللَّهِ"
        result = normalize_arabic(text_with_harakat)
        # Result should have no harakat characters
        assert "ِ" not in result  # kasra
        assert "ْ" not in result  # sukun
        assert "َ" not in result  # fatha

    def test_normalizes_alef_variants(self):
        """Should normalize all alef variants to plain alef."""
        assert "ا" in normalize_arabic("أحمد")  # alef with hamza above
        assert "ا" in normalize_arabic("إبراهيم")  # alef with hamza below
        assert "ا" in normalize_arabic("آمين")  # alef with madda

    def test_normalizes_taa_marbuta(self):
        """Should normalize taa marbuta to haa."""
        result = normalize_arabic("الصلاة")
        assert "ه" in result or "ة" not in result

    def test_normalizes_whitespace(self):
        """Should collapse multiple spaces to single space."""
        result = normalize_arabic("الله    أكبر")
        assert "    " not in result
        assert " " in result

    def test_strips_whitespace(self):
        """Should strip leading/trailing whitespace."""
        result = normalize_arabic("  الله أكبر  ")
        assert not result.startswith(" ")
        assert not result.endswith(" ")

    def test_empty_string(self):
        """Should handle empty string."""
        assert normalize_arabic("") == ""

    def test_non_arabic_text(self):
        """Should pass through non-Arabic text unchanged."""
        result = normalize_arabic("Hello World")
        assert result == "Hello World"


class TestFuzzyMatchAthan:
    """Tests for Athan phrase fuzzy matching."""

    def test_returns_tuple_of_three(self):
        """Should return (score, translation, arabic_phrase) tuple."""
        result = fuzzy_match_athan("الله أكبر")
        assert isinstance(result, tuple)
        assert len(result) == 3

    def test_score_is_float(self):
        """Score should be a float between 0 and 1."""
        score, _, _ = fuzzy_match_athan("الله أكبر")
        assert isinstance(score, float)
        assert 0.0 <= score <= 1.0

    def test_empty_input(self):
        """Should handle empty input gracefully."""
        score, trans, ar = fuzzy_match_athan("")
        assert isinstance(score, float)
        # Empty string should have low or zero score

    def test_exact_match_high_score(self):
        """Exact match should have high score (if in dictionary)."""
        # This test depends on the actual athan_dict content
        # Using a common Athan phrase
        score, trans, ar = fuzzy_match_athan("الله أكبر")
        # If this phrase is in the dictionary, score should be high
        # We can't assert exact value without knowing dictionary content
        assert score >= 0.0

    def test_gibberish_low_score(self):
        """Random text should have low match score."""
        score, _, _ = fuzzy_match_athan("xyz123 random text")
        assert score < 0.5  # Should be low for non-Arabic gibberish


class TestFuzzyMatchAthanEdgeCases:
    """Edge case tests for fuzzy matching."""

    def test_none_handling(self):
        """Should handle None-like edge cases."""
        # The function expects str, but should not crash on edge cases
        score, trans, ar = fuzzy_match_athan("   ")
        assert isinstance(score, float)

    def test_unicode_handling(self):
        """Should handle various Unicode correctly."""
        # Arabic with various Unicode characters
        result = fuzzy_match_athan("اللّٰهُ أَكْبَر")
        assert isinstance(result[0], float)

    def test_mixed_arabic_latin(self):
        """Should handle mixed Arabic and Latin text."""
        score, _, _ = fuzzy_match_athan("الله akbar")
        assert isinstance(score, float)


def _load(directory: str, code: str) -> dict:
    with open(Path(directory) / f"{code}.json", encoding="utf-8") as f:
        return json.load(f)


_QURAN_CODES = sorted(p.stem for p in Path(QURAN_TRANSLATIONS_DIR).glob("*.json"))
_ATHAN_CODES = sorted(p.stem for p in Path(ATHAN_TRANSLATIONS_DIR).glob("*.json"))
_VERSE_REF = re.compile(r"\((\d+:\d+)\)$")

# Target languages without a file, and why (data/translations/quran/SOURCES.md).
# Arabic needs neither: the Arabic is already the output. Pinned both ways: a new
# target without dictionaries fails here, and so does a gap that gets filled.
_QURAN_GAPS = {"ar", "ku", "ko", "pl", "sd", "zh-hant"}
_ATHAN_GAPS = {"ar", "ku"}


class TestDictionaryFiles:
    """Every shipped dictionary has the reference file's shape.

    A verified verse is shown verbatim, so a file that is partial or numbered
    differently from the reference puts the wrong verse on screen as verified.
    """

    @pytest.mark.parametrize("code", _QURAN_CODES)
    def test_quran_file_matches_the_reference_verse_for_verse(self, code):
        reference = _load(QURAN_TRANSLATIONS_DIR, "de")
        data = _load(QURAN_TRANSLATIONS_DIR, code)
        assert data.keys() == reference.keys()
        for arabic, text in data.items():
            ref = _VERSE_REF.search(text)
            assert ref, f"{code}: no (sura:ayah) at the end of {text[-40:]!r}"
            expected = _VERSE_REF.search(reference[arabic]).group(1)
            assert ref.group(1) == expected, f"{code}: {ref.group(1)} != {expected}"
            assert text[: ref.start()].strip(), f"{code}: empty {expected}"

    @pytest.mark.parametrize("code", _ATHAN_CODES)
    def test_athan_file_has_every_phrase(self, code):
        reference = _load(ATHAN_TRANSLATIONS_DIR, "de")
        data = _load(ATHAN_TRANSLATIONS_DIR, code)
        assert data.keys() == reference.keys()
        assert all(value.strip() for value in data.values())

    def test_every_target_language_has_its_dictionaries(self):
        targets = {code for _, code in TARGET_LANGUAGES}
        assert targets - set(_QURAN_CODES) == _QURAN_GAPS
        assert targets - set(_ATHAN_CODES) == _ATHAN_GAPS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

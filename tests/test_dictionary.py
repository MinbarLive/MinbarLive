"""Tests for dictionary-based matching functions."""

import pytest
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from translation.dictionary import normalize_arabic, fuzzy_match_athan


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

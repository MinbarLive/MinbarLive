"""Tests for the shared STT helpers (translation/stt.py).

These are the single copy of the model-fallback chain and the secondary
Arabic re-transcription that the live segmented pipeline and batch mode both
use (consolidated from their former near-verbatim duplicates).
"""

import sys
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import translation.stt as stt
from translation.stt import (
    has_min_letters,
    maybe_arabic_retranscription,
    strip_overlap_prefix,
    transcribe_with_fallback,
)


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Run retries without sleeping."""
    monkeypatch.setattr(stt, "retry_with_backoff", lambda fn, **kw: fn())


class RecordingProvider:
    def __init__(self, fail_models=(), text="نص"):
        self.calls = []
        self.fail_models = set(fail_models)
        self.text = text

    def transcribe(self, audio, *, model, language=None, prompt=None):
        self.calls.append((model, language, prompt))
        if model in self.fail_models:
            raise RuntimeError(f"{model} down")
        return self.text


class TestTranscribeWithFallback:
    def test_first_model_wins(self):
        p = RecordingProvider()
        assert transcribe_with_fallback(p, ["m1", "m2"], b"wav", "ar") == "نص"
        assert [c[0] for c in p.calls] == ["m1"]

    def test_falls_back_in_chain_order(self):
        p = RecordingProvider(fail_models={"m1"})
        assert transcribe_with_fallback(p, ["m1", "m2"], b"wav", "ar") == "نص"
        assert [c[0] for c in p.calls] == ["m1", "m2"]

    def test_all_models_failed_returns_none(self):
        p = RecordingProvider(fail_models={"m1", "m2"})
        assert transcribe_with_fallback(p, ["m1", "m2"], b"wav", "ar") is None

    def test_language_and_prompt_are_forwarded(self):
        p = RecordingProvider()
        transcribe_with_fallback(p, ["m1"], b"wav", "tr", prompt="tail")
        assert p.calls == [("m1", "tr", "tail")]


class TestMaybeArabicRetranscription:
    def _run(self, provider, **overrides):
        kwargs = {
            "transcription": "metin",  # Latin script
            "source_lang_code": "tr",
            "source_language": "Turkish",
            "target_language": "German",
            "islamic_mode": True,
        }
        kwargs.update(overrides)
        return maybe_arabic_retranscription(provider, "m1", b"wav", **kwargs)

    def test_runs_for_non_arabic_source_in_islamic_mode(self):
        p = RecordingProvider(text="نص عربي")
        assert self._run(p) == "نص عربي"
        assert p.calls == [("m1", "ar", None)]

    def test_skipped_for_arabic_source(self):
        p = RecordingProvider()
        assert self._run(p, source_lang_code="ar") == ""
        assert p.calls == []

    def test_skipped_in_same_language_mode(self):
        p = RecordingProvider()
        assert (
            self._run(p, source_language="German", target_language="German") == ""
        )
        assert p.calls == []

    def test_skipped_when_islamic_mode_off(self):
        p = RecordingProvider()
        assert self._run(p, islamic_mode=False) == ""
        assert p.calls == []

    def test_skipped_when_transcription_already_arabic_script(self):
        p = RecordingProvider()
        assert self._run(p, transcription="الحمد لله رب العالمين") == ""
        assert p.calls == []

    def test_failure_degrades_to_empty_string(self):
        p = RecordingProvider(fail_models={"m1"})
        assert self._run(p) == ""


class TestStripOverlapPrefix:
    """The OVERLAP-second repeat of the previous segment's tail is stripped
    from the head of the next segment (live segmented dedup)."""

    def test_strips_repeated_prefix(self):
        assert strip_overlap_prefix("a b c d", "c d e f") == "e f"

    def test_no_overlap_returns_current_unchanged(self):
        assert strip_overlap_prefix("a b c", "x y z") == "x y z"

    def test_empty_previous_returns_current(self):
        assert strip_overlap_prefix("", "a b c") == "a b c"

    def test_never_empties_the_segment(self):
        # cur is entirely prev's tail — the never-empty guard keeps it, so a
        # genuine full repetition is never mistaken for boundary overlap.
        assert strip_overlap_prefix("w1 w2 w3", "w2 w3") == "w2 w3"

    def test_identical_single_word_segments_kept(self):
        # The exact case the FakeTranscription fixture produces ("metin"
        # every call): a 1-word segment can never be stripped (len-1 == 0).
        assert strip_overlap_prefix("metin", "metin") == "metin"

    def test_longest_overlap_wins(self):
        assert strip_overlap_prefix("x a b c", "a b c d e") == "d e"

    def test_arabic_normalization_ignores_diacritics(self):
        # prev tail has harakat, cur prefix does not — still recognized as
        # the same overlapping word and stripped.
        assert strip_overlap_prefix("قَالَ اللَّه", "الله رحيم") == "رحيم"

    def test_overlap_within_cap_stripped(self):
        assert strip_overlap_prefix("x w1 w2", "w1 w2 y z", max_words=2) == "y z"

    def test_overlap_longer_than_cap_is_ignored(self):
        # A 3-word overlap with max_words=2: the shorter partial prefixes do
        # not align, so nothing is stripped (safe under-strip — the cap never
        # produces a wrong partial strip).
        assert (
            strip_overlap_prefix("x w1 w2 w3", "w1 w2 w3 y z", max_words=2)
            == "w1 w2 w3 y z"
        )

    def test_raw_text_preserved_after_strip(self):
        # Stripping is by word index on the raw text: kept words keep their
        # original diacritics/case.
        assert strip_overlap_prefix("one TWO", "two THREE four") == "THREE four"

    def test_trailing_punctuation_on_overlap_still_stripped(self):
        # The STT ends the overlapped word with a period on one pass but not
        # the other ("الخالق." vs "الخالق"); edge punctuation must be ignored.
        assert (
            strip_overlap_prefix("وهو الخالق.", "وهو الخالق ويقول") == "ويقول"
        )

    def test_near_identical_word_fuzzy_matched(self):
        # A one-letter STT difference in the overlap ("testing" vs "testng")
        # is still recognised as the same overlapped word.
        assert strip_overlap_prefix("keep testing", "testng again") == "again"

    def test_unrelated_short_words_not_fuzzy_matched(self):
        # Short function words must match exactly, or unrelated segments would
        # look overlapped.
        assert strip_overlap_prefix("في", "من هنا") == "من هنا"


class TestHasMinLetters:
    def test_full_word_passes(self):
        assert has_min_letters("الله") is True

    def test_single_arabic_letter_fails(self):
        assert has_min_letters("م") is False

    def test_two_letters_fail(self):
        assert has_min_letters("ال") is False
        assert has_min_letters("Um") is False

    def test_three_latin_letters_pass(self):
        assert has_min_letters("abc") is True

    def test_empty_and_symbols_fail(self):
        assert has_min_letters("") is False
        assert has_min_letters("... 123 !!") is False

    def test_letters_counted_across_words(self):
        # Two 1-letter words still total 2 letters -> fragment.
        assert has_min_letters("a b") is False
        assert has_min_letters("a b c") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

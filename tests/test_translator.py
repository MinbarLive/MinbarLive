"""Tests for translation pipeline logic: verified-verse bypass and prompts."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import QURAN_VERIFIED_MARKER
from translation import translator

# 4 words after normalization
VERSE = "بسم الله الرحمن الرحيم"
VERSE_TRANSLATION = "Im Namen Allahs, des Allerbarmers, des Barmherzigen. (1:1)"


class TestSelectVerifiedVerse:
    """Tests for the hard-verified bypass guard logic."""

    @pytest.fixture(autouse=True)
    def target_dict(self, monkeypatch):
        monkeypatch.setattr(
            translator,
            "get_quran_dict",
            lambda code: {VERSE: VERSE_TRANSLATION},
        )

    def test_exact_verse_qualifies(self):
        result = translator._select_verified_verse(
            [(0.90, VERSE, "hint")], VERSE, "de"
        )
        assert result == (0.90, VERSE, VERSE_TRANSLATION)

    def test_score_below_threshold_rejected(self):
        result = translator._select_verified_verse(
            [(0.80, VERSE, "hint")], VERSE, "de"
        )
        assert result is None

    def test_segment_with_surrounding_speech_rejected(self):
        """The critical guard: verse plus sermon speech must NOT bypass GPT,
        otherwise the non-verse speech would be silently dropped."""
        segment = VERSE + " ثم تحدث الخطيب عن موضوع آخر في الخطبة اليوم"
        result = translator._select_verified_verse(
            [(0.90, VERSE, "hint")], segment, "de"
        )
        assert result is None

    def test_long_verse_with_extra_sentence_rejected(self, monkeypatch):
        """Regression: for long verses the ratio band alone admits whole
        dropped sentences — the absolute word-diff cap must catch them."""
        long_verse = " ".join(f"كلمة{i}" for i in range(40))
        monkeypatch.setattr(
            translator, "get_quran_dict", lambda code: {long_verse: "trans"}
        )
        # 8 extra words: ratio 48/40 = 1.2 (inside band) but diff 8 > cap
        segment = long_verse + " ثم تحدث الخطيب عن أهمية هذه الآية العظيمة"
        result = translator._select_verified_verse(
            [(0.90, long_verse, "hint")], segment, "de"
        )
        assert result is None

    def test_long_verse_with_minor_word_noise_qualifies(self, monkeypatch):
        """Small transcription noise (±few words) must not block the bypass."""
        long_verse = " ".join(f"كلمة{i}" for i in range(40))
        monkeypatch.setattr(
            translator, "get_quran_dict", lambda code: {long_verse: "trans"}
        )
        segment = long_verse + " كلمتان إضافيتان"  # 2 extra words
        result = translator._select_verified_verse(
            [(0.90, long_verse, "hint")], segment, "de"
        )
        assert result == (0.90, long_verse, "trans")

    def test_partial_verse_rejected(self):
        """A half-recited verse must not be over-completed by the bypass."""
        result = translator._select_verified_verse(
            [(0.90, VERSE, "hint")], "بسم الله", "de"
        )
        assert result is None

    def test_missing_target_translation_rejected(self, monkeypatch):
        """German reference fallback is a GPT hint, never verified output."""
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: {})
        result = translator._select_verified_verse(
            [(0.90, VERSE, "german fallback")], VERSE, "fr"
        )
        assert result is None

    def test_arabic_target_uses_match_directly(self, monkeypatch):
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: {})
        result = translator._select_verified_verse(
            [(0.90, VERSE, VERSE)], VERSE, "ar"
        )
        assert result == (0.90, VERSE, VERSE)

    def test_empty_matches(self):
        assert translator._select_verified_verse([], VERSE, "de") is None

    def test_only_top_match_considered(self):
        """A high-scoring second match must not qualify over a weak top match."""
        matches = [(0.80, "other verse", "x"), (0.95, VERSE, "hint")]
        assert translator._select_verified_verse(matches, VERSE, "de") is None


class TestTranslateTextBypass:
    """End-to-end translate_text behavior around the bypass."""

    @pytest.fixture(autouse=True)
    def pipeline(self, monkeypatch):
        monkeypatch.setattr(
            translator,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Arabic",
                target_language="German",
                translation_model="test-model",
                islamic_mode=True,
            ),
        )
        monkeypatch.setattr(
            translator, "fuzzy_match_athan", lambda text, code: (0.0, None, None)
        )
        monkeypatch.setattr(
            translator,
            "get_quran_dict",
            lambda code: {VERSE: VERSE_TRANSLATION},
        )

    def test_verified_verse_skips_gpt(self, monkeypatch):
        monkeypatch.setattr(
            translator,
            "match_quran_rag_multi",
            lambda text, target_lang_code: [(0.92, VERSE, "hint")],
        )

        class _NoGPTProvider:
            def complete(self, **kwargs):
                raise AssertionError("GPT must not be called for a verified verse")

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _NoGPTProvider()
        )

        result = translator.translate_text(VERSE)
        assert result == f"{QURAN_VERIFIED_MARKER} {VERSE_TRANSLATION}"

    def test_multi_verse_run_skips_gpt(self, monkeypatch):
        """Two complete consecutive ayat in one segment (both below the
        single-verse threshold) must be certified as a run without GPT."""
        ref_dict = {RUN_VERSE_A: RUN_TRANS_A, RUN_VERSE_B: RUN_TRANS_B}
        monkeypatch.setattr(translator, "quran_dict", ref_dict)
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: dict(ref_dict))
        monkeypatch.setattr(
            translator,
            "match_quran_rag_multi",
            lambda text, target_lang_code: [
                (0.77, RUN_VERSE_A, "hint"),
                (0.69, RUN_VERSE_B, "hint"),
            ],
        )

        class _NoGPTProvider:
            def complete(self, **kwargs):
                raise AssertionError("GPT must not be called for a verified run")

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _NoGPTProvider()
        )

        result = translator.translate_text(f"{RUN_VERSE_A} {RUN_VERSE_B}")
        assert result == f"{QURAN_VERIFIED_MARKER} {RUN_TRANS_A} {RUN_TRANS_B}"

    def test_below_threshold_goes_through_gpt(self, monkeypatch):
        monkeypatch.setattr(
            translator,
            "match_quran_rag_multi",
            lambda text, target_lang_code: [(0.70, VERSE, "hint")],
        )
        calls = {}

        class _FakeProvider:
            def complete(self, **kwargs):
                calls.update(kwargs)
                return "GPT out"

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _FakeProvider()
        )

        result = translator.translate_text(VERSE)
        assert result == "GPT out"
        assert not result.startswith(QURAN_VERIFIED_MARKER)
        # The 0.60-0.85 range must still be passed to GPT as a hint
        assert "hint" in calls["user_prompt"]


# Consecutive ayat (At-Takwir 81:7 / 81:8) — the multi-verse-run case
RUN_VERSE_A = "واذا النفوس زوجت"
RUN_VERSE_B = "واذا الموءودة سئلت"
RUN_TRANS_A = "und wenn die Seelen gepaart werden, (81:7)"
RUN_TRANS_B = "und wenn das lebendig begrabene Mädchen gefragt wird, (81:8)"


class TestSelectVerifiedVerseRun:
    """Multi-verse run bypass: consecutive ayat certified by exact text."""

    @pytest.fixture(autouse=True)
    def dicts(self, monkeypatch):
        ref_dict = {RUN_VERSE_A: RUN_TRANS_A, RUN_VERSE_B: RUN_TRANS_B}
        monkeypatch.setattr(translator, "quran_dict", ref_dict)
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: dict(ref_dict))

    def test_two_consecutive_verses_verified(self):
        segment = f"{RUN_VERSE_A} {RUN_VERSE_B}"
        result = translator._select_verified_verse_run(
            [(0.77, RUN_VERSE_A, "hint"), (0.69, RUN_VERSE_B, "hint")], segment, "de"
        )
        assert result == f"{RUN_TRANS_A} {RUN_TRANS_B}"

    def test_output_in_ayah_order_not_score_order(self):
        segment = f"{RUN_VERSE_A} {RUN_VERSE_B}"
        result = translator._select_verified_verse_run(
            [(0.90, RUN_VERSE_B, "hint"), (0.65, RUN_VERSE_A, "hint")], segment, "de"
        )
        assert result == f"{RUN_TRANS_A} {RUN_TRANS_B}"

    def test_non_consecutive_ayat_rejected(self, monkeypatch):
        """81:7 + 81:10 are not consecutive — never certified as a run."""
        verse_c = "واذا الصحف نشرت"
        trans_c = "und wenn die Blätter aufgeschlagen werden, (81:10)"
        ref_dict = {RUN_VERSE_A: RUN_TRANS_A, verse_c: trans_c}
        monkeypatch.setattr(translator, "quran_dict", ref_dict)
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: dict(ref_dict))
        segment = f"{RUN_VERSE_A} {verse_c}"
        result = translator._select_verified_verse_run(
            [(0.80, RUN_VERSE_A, "h"), (0.70, verse_c, "h")], segment, "de"
        )
        assert result is None

    def test_different_surahs_rejected(self, monkeypatch):
        verse_c = "اذا السماء انفطرت"
        trans_c = "Wenn der Himmel zerbricht (82:1)"
        ref_dict = {RUN_VERSE_B: RUN_TRANS_B, verse_c: trans_c}
        monkeypatch.setattr(translator, "quran_dict", ref_dict)
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: dict(ref_dict))
        segment = f"{RUN_VERSE_B} {verse_c}"
        result = translator._select_verified_verse_run(
            [(0.80, RUN_VERSE_B, "h"), (0.70, verse_c, "h")], segment, "de"
        )
        assert result is None

    def test_sermon_speech_around_run_rejected(self):
        """The critical guard carries over: verses + sermon must NOT bypass GPT."""
        segment = (
            f"{RUN_VERSE_A} {RUN_VERSE_B} "
            "ثم تحدث الخطيب عن معاني هذه الآيات الكريمة"
        )
        result = translator._select_verified_verse_run(
            [(0.77, RUN_VERSE_A, "h"), (0.69, RUN_VERSE_B, "h")], segment, "de"
        )
        assert result is None

    def test_partial_second_verse_rejected(self):
        segment = f"{RUN_VERSE_A} واذا"
        result = translator._select_verified_verse_run(
            [(0.77, RUN_VERSE_A, "h"), (0.69, RUN_VERSE_B, "h")], segment, "de"
        )
        assert result is None

    def test_same_length_but_different_text_rejected(self):
        """Length guards alone are not certification — the text must match."""
        segment = f"{RUN_VERSE_A} ثم قال الخطيب"
        result = translator._select_verified_verse_run(
            [(0.77, RUN_VERSE_A, "h"), (0.69, RUN_VERSE_B, "h")], segment, "de"
        )
        assert result is None

    def test_missing_target_translation_rejected(self, monkeypatch):
        """German reference fallback is a GPT hint, never verified output."""
        monkeypatch.setattr(
            translator, "get_quran_dict", lambda code: {RUN_VERSE_A: RUN_TRANS_A}
        )
        segment = f"{RUN_VERSE_A} {RUN_VERSE_B}"
        result = translator._select_verified_verse_run(
            [(0.77, RUN_VERSE_A, "h"), (0.69, RUN_VERSE_B, "h")], segment, "fr"
        )
        assert result is None

    def test_single_candidate_rejected(self):
        result = translator._select_verified_verse_run(
            [(0.90, RUN_VERSE_A, "h")], RUN_VERSE_A, "de"
        )
        assert result is None

    def test_arabic_target_uses_verses_directly(self, monkeypatch):
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: {})
        segment = f"{RUN_VERSE_A} {RUN_VERSE_B}"
        result = translator._select_verified_verse_run(
            [(0.77, RUN_VERSE_A, "h"), (0.69, RUN_VERSE_B, "h")], segment, "ar"
        )
        assert result == f"{RUN_VERSE_A} {RUN_VERSE_B}"


ARABIC_TEXT = "الحمد لله رب العالمين"


class TestSameLanguageHelpers:
    """Unit tests for the script check and the same-language decision."""

    def test_is_arabic_script_arabic(self):
        assert translator.is_arabic_script(ARABIC_TEXT)

    def test_is_arabic_script_latin(self):
        assert not translator.is_arabic_script("Guten Abend zusammen")

    def test_is_arabic_script_mixed_majority_arabic(self):
        text = "قال الخطيب subhanallah ثم تابع الخطبة بكلام طويل جدا"
        assert translator.is_arabic_script(text)

    def test_is_arabic_script_empty_or_symbols(self):
        assert not translator.is_arabic_script("")
        assert not translator.is_arabic_script("123 ... !؟")

    def test_same_language_by_name(self):
        assert translator._is_same_language("Arabic", "Arabic", "ar", "x")
        assert translator._is_same_language("German", "German", "de", "x")
        assert not translator._is_same_language("Arabic", "German", "de", "x")

    def test_automatic_only_bypasses_for_arabic_script_and_target(self):
        assert translator._is_same_language("Automatic", "Arabic", "ar", ARABIC_TEXT)
        assert not translator._is_same_language("Automatic", "Arabic", "ar", "hello")
        # Latin-script languages cannot be told apart — never bypassed
        assert not translator._is_same_language("Automatic", "German", "de", "hallo")


class TestSameLanguageMode:
    """Same-language runs must skip GPT entirely (translator step 0)."""

    @pytest.fixture(autouse=True)
    def pipeline(self, monkeypatch):
        class _NoGPTProvider:
            def complete(self, **kwargs):
                raise AssertionError("GPT must not be called in same-language mode")

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _NoGPTProvider()
        )
        monkeypatch.setattr(
            translator, "fuzzy_match_athan", lambda text, code: (0.0, None, None)
        )
        monkeypatch.setattr(
            translator,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Arabic",
                target_language="Arabic",
                translation_model="test-model",
                islamic_mode=True,
            ),
        )

    def test_explicit_same_language_returns_transcription(self):
        result = translator.translate_text(
            ARABIC_TEXT, source_language="Arabic", target_language="Arabic"
        )
        assert result == ARABIC_TEXT

    def test_automatic_source_with_arabic_text_and_target_bypasses(self):
        result = translator.translate_text(
            ARABIC_TEXT, source_language="Automatic", target_language="Arabic"
        )
        assert result == ARABIC_TEXT

    def test_automatic_source_with_latin_text_translates(self, monkeypatch):
        class _FakeProvider:
            def complete(self, **kwargs):
                return "ترجمة"

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _FakeProvider()
        )
        monkeypatch.setattr(
            translator, "match_quran_rag_multi", lambda text, target_lang_code: []
        )
        result = translator.translate_text(
            "hello everyone", source_language="Automatic", target_language="Arabic"
        )
        assert result == "ترجمة"


class TestPromptCodeSwitching:
    """The code-switching instructions must be present in the prompts."""

    def test_system_prompt_has_code_switching_rules(self):
        prompt = translator._build_system_prompt("Arabic", "German")
        assert "Code-switching rules" in prompt
        assert "ALREADY in German" in prompt
        assert "return it as-is" in prompt

    def test_user_prompt_mentions_already_translated_parts(self):
        prompt = translator._build_user_prompt("text", "", "", "Arabic", "German")
        assert "already in German" in prompt

    def test_general_prompts_keep_code_switching_but_not_islamic(self):
        system = translator._build_general_system_prompt("Arabic", "German")
        assert "Code-switching rules" in system
        assert "professional translator" in system
        assert "Islamic" not in system
        user = translator._build_general_user_prompt("text", "", "Arabic", "German")
        assert "already in German" in user
        assert "mosque" not in user
        assert "Quran" not in user

    def test_prompts_forbid_meta_commentary_on_unintelligible_input(self):
        """The 'Das Wort ist unverständlich' class: on garbled input the model
        must output an empty string, not describe the audio."""
        for build in (
            translator._build_system_prompt,
            translator._build_general_system_prompt,
        ):
            prompt = build("Arabic", "German")
            assert "empty string" in prompt
            assert "never describe" in prompt.lower()

    def test_prompts_forbid_raw_arabic_script_in_output(self):
        """عبادة-style raw script must be transliterated or translated."""
        for build in (
            translator._build_system_prompt,
            translator._build_general_system_prompt,
        ):
            prompt = build("Arabic", "German")
            assert "raw Arabic-script" in prompt
            assert "transliterate" in prompt


class TestIslamicModeOff:
    """islamic_mode=False: pipeline becomes a general translator — no
    Athan/RAG/verse-bypass, neutral prompts."""

    @pytest.fixture(autouse=True)
    def pipeline(self, monkeypatch):
        monkeypatch.setattr(
            translator,
            "load_settings",
            lambda: SimpleNamespace(
                source_language="Arabic",
                target_language="German",
                translation_model="test-model",
                islamic_mode=False,
            ),
        )

    def test_matchers_skipped_and_neutral_prompt_used(self, monkeypatch):
        def _must_not_run(*args, **kwargs):
            raise AssertionError("matcher must not run with Islamic mode off")

        monkeypatch.setattr(translator, "fuzzy_match_athan", _must_not_run)
        monkeypatch.setattr(translator, "match_quran_rag_multi", _must_not_run)

        calls = {}

        class _FakeProvider:
            def complete(self, **kwargs):
                calls.update(kwargs)
                return "general out"

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _FakeProvider()
        )

        assert translator.translate_text("نص عادي للترجمة") == "general out"
        assert "Islamic" not in calls["system_prompt"]
        assert "professional translator" in calls["system_prompt"]
        assert "Quran" not in calls["user_prompt"]
        assert "mosque" not in calls["user_prompt"]


class TestStripArabicLeak:
    """The guard that removes stray untranslated Arabic words from a
    non-Arabic GPT translation."""

    def test_minority_arabic_token_stripped(self):
        out = translator._strip_arabic_leak("die Mutter aller نعم", "de")
        assert out == "die Mutter aller"

    def test_clean_german_unchanged(self):
        text = "Alles Lob gebührt Allah."
        assert translator._strip_arabic_leak(text, "de") == text

    def test_arabic_target_never_touched(self):
        text = "بسم الله الرحمن الرحيم"
        assert translator._strip_arabic_leak(text, "ar") == text

    def test_majority_arabic_left_as_is(self):
        # A mostly-Arabic result is a failed translation, not a leak — leave it
        # for the caller to notice rather than silently gutting it.
        text = "الحمد لله رب العالمين ok"
        assert translator._strip_arabic_leak(text, "de") == text

    def test_latin_transliteration_kept(self):
        text = "Er lehrte den Tawhid der Rububiyya."
        assert translator._strip_arabic_leak(text, "de") == text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

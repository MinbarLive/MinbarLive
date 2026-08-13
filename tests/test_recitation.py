"""Recitation follow-through: predicting the next ayah without ever asserting it.

The safety property under test is one-directional. The tracker may add
candidates the embedding missed; it may never cause a verse to be output. If
a prediction is wrong, the exact-text certification in
``_select_verified_verse_run`` must throw it away — because the alternative is
an ayah on a mosque screen that nobody recited.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from config import RECITATION_LOOKAHEAD_AYAT
from translation import recitation, translator

# At-Takwir 81:7-81:10, four consecutive ayat.
V7 = "واذا النفوس زوجت"
V8 = "واذا الموءودة سئلت"
V9 = "باي ذنب قتلت"
V10 = "واذا الصحف نشرت"
REF = {
    V7: "und wenn die Seelen gepaart werden, (81:7)",
    V8: "und wenn das lebendig begrabene Mädchen gefragt wird, (81:8)",
    V9: "wegen welcher Sünde sie getötet wurde, (81:9)",
    V10: "und wenn die Blätter aufgeschlagen werden, (81:10)",
}


@pytest.fixture(autouse=True)
def clean_tracker(monkeypatch):
    monkeypatch.setattr(recitation, "quran_dict", REF)
    recitation.reset()
    yield
    recitation.reset()


class TestTrackerActivation:
    def test_one_verse_predicts_nothing(self):
        """A single quoted ayah mid-sermon says nothing about what follows —
        the speaker is about to explain it, not continue reciting."""
        recitation.note_certified([(81, 7)])
        assert recitation.expected_next() == []

    def test_two_consecutive_ayat_start_a_recitation(self):
        recitation.note_certified([(81, 7), (81, 8)])
        assert recitation.expected_next() == [
            (81, 8 + step) for step in range(1, RECITATION_LOOKAHEAD_AYAT + 1)
        ]

    def test_two_ayat_with_a_gap_also_start_one(self):
        """The normal shape of a real recitation, not an edge case: 81:8 was
        recited but only half of it landed in the segment, so the text check
        refused to certify it and GPT translated it instead. Requiring a clean
        consecutive run here withheld the prediction in exactly the case it
        exists for."""
        recitation.note_certified([(81, 7)])
        recitation.note_certified([(81, 9)])
        assert recitation.expected_next()[0] == (81, 10)

    def test_prediction_continues_from_the_highest_ayah(self):
        recitation.note_certified([(81, 7), (81, 8)])
        recitation.note_certified([(81, 9)])
        assert recitation.expected_next()[0] == (81, 10)

    def test_a_single_verse_keeps_an_established_recitation_alive(self, monkeypatch):
        """Starting needs two; continuing needs one. Otherwise a patchy
        stretch — the only kind that needs help — drops out of recitation
        mode."""
        monkeypatch.setattr(recitation, "RECITATION_WINDOW_SECONDS", 0.4)
        recitation.note_certified([(81, 7), (81, 8)])
        for ayah in (9, 10, 11):
            time.sleep(0.25)  # longer than half the window: only the refresh saves it
            recitation.note_certified([(81, ayah)])
            assert recitation.expected_next()[0] == (81, ayah + 1)

    def test_a_repeated_verse_does_not_walk_the_prediction_backwards(self):
        """A reciter repeating an ayah, or an out-of-order certification, must
        not un-advance the anchor over ground already covered."""
        recitation.note_certified([(81, 7), (81, 8), (81, 9)])
        recitation.note_certified([(81, 8)])
        assert recitation.expected_next()[0] == (81, 10)

    def test_two_ayat_of_different_surahs_are_not_a_recitation(self):
        recitation.note_certified([(81, 7), (2, 255)])
        assert recitation.expected_next() == []

    def test_a_new_surah_needs_its_own_two_verses_to_take_over(self):
        recitation.note_certified([(81, 7), (81, 8)])
        recitation.note_certified([(2, 255)])  # one ayah quoted elsewhere
        assert recitation.expected_next()[0] == (81, 9)  # still At-Takwir
        recitation.note_certified([(2, 256)])
        assert recitation.expected_next()[0] == (2, 257)  # now Al-Baqara

    def test_a_stale_recitation_stops_predicting(self, monkeypatch):
        monkeypatch.setattr(recitation, "RECITATION_WINDOW_SECONDS", 0.05)
        recitation.note_certified([(81, 7), (81, 8)])
        time.sleep(0.1)
        assert recitation.expected_next() == []

    def test_a_lapsed_recitation_needs_two_verses_again(self, monkeypatch):
        """Once the window closes, a single verse must not silently resume it —
        that is the "one quotation predicts nothing" rule again."""
        monkeypatch.setattr(recitation, "RECITATION_WINDOW_SECONDS", 0.05)
        recitation.note_certified([(81, 7), (81, 8)])
        time.sleep(0.1)
        recitation.note_certified([(81, 20)])
        assert recitation.expected_next() == []

    def test_reset_forgets_everything(self):
        recitation.note_certified([(81, 7), (81, 8)])
        recitation.reset()
        assert recitation.expected_next() == []


class TestCandidateInjection:
    def test_no_recitation_leaves_the_list_untouched(self):
        existing = [(0.71, V7, "hint")]
        assert recitation.expected_candidates(existing) is existing

    def test_expected_ayat_are_appended(self):
        recitation.note_certified([(81, 7), (81, 8)])
        augmented = recitation.expected_candidates([(0.71, V8, "hint")])
        assert [verse for _s, verse, _h in augmented] == [V8, V9, V10]

    def test_injected_candidates_score_zero(self):
        """A prediction is not evidence. Zero keeps it out of the top slot the
        score-based single-verse bypass reads, so it can only ever reach the
        verifier that checks the actual text."""
        recitation.note_certified([(81, 7), (81, 8)])
        augmented = recitation.expected_candidates([(0.71, V8, "hint")])
        assert [score for score, _v, _h in augmented if _v != V8] == [0.0, 0.0]

    def test_a_verse_the_embedding_already_found_is_not_duplicated(self):
        recitation.note_certified([(81, 7), (81, 8)])
        augmented = recitation.expected_candidates([(0.71, V9, "hint")])
        assert [verse for _s, verse, _h in augmented].count(V9) == 1

    def test_running_off_the_end_of_a_surah_is_not_an_error(self):
        recitation.note_certified([(81, 28), (81, 29)])  # 81 has 29 ayat
        assert recitation.expected_candidates([]) == []


class TestPredictionsStillHaveToBeEarned:
    """The whole safety argument: injection widens nomination, never
    certification."""

    @pytest.fixture(autouse=True)
    def dicts(self, monkeypatch):
        monkeypatch.setattr(translator, "quran_dict", REF)
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: dict(REF))

    def test_a_wrong_prediction_is_thrown_away(self):
        """81:9 is predicted, but the speaker moved on to sermon prose. The
        text comparison must reject it — this is the fabrication case."""
        recitation.note_certified([(81, 7), (81, 8)])
        segment = "ثم تحدث الخطيب عن معاني هذه الايات الكريمة وذكر"
        result = translator._select_verified_verse_run(
            recitation.expected_candidates([]), segment, "de"
        )
        assert result is None

    def test_a_right_prediction_rescues_a_verse_the_embedding_missed(self):
        """The win. Only 81:9 was nominated by similarity; 81:10 fell out of
        the top-k, so the pair could never be certified as a run before."""
        recitation.note_certified([(81, 7), (81, 8)])
        segment = f"{V9} {V10}"
        result = translator._select_verified_verse_run(
            recitation.expected_candidates([(0.71, V9, "hint")]), segment, "de"
        )
        assert result == f"{REF[V9]} {REF[V10]}"

    def test_prediction_alone_certifies_nothing_without_the_text(self):
        """Predicted verses with an empty segment: no text to match, no
        output, however confident the tracker is."""
        recitation.note_certified([(81, 7), (81, 8)])
        result = translator._select_verified_verse_run(
            recitation.expected_candidates([]), "", "de"
        )
        assert result is None


class TestTranslateTextWiring:
    @pytest.fixture(autouse=True)
    def dicts(self, monkeypatch):
        monkeypatch.setattr(translator, "quran_dict", REF)
        monkeypatch.setattr(translator, "get_quran_dict", lambda code: dict(REF))
        monkeypatch.setattr(
            translator, "fuzzy_match_athan", lambda text, code: (0.0, "", "")
        )
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

    def test_a_certified_run_is_remembered(self, monkeypatch):
        """Certification, not nomination, is what convinces the tracker."""
        monkeypatch.setattr(
            translator,
            "match_quran_rag_multi",
            lambda text, target_lang_code: [(0.77, V7, "h"), (0.69, V8, "h")],
        )
        translator.translate_text(f"{V7} {V8}")
        assert recitation.expected_next()[0] == (81, 9)

    def test_a_gpt_translation_is_not_remembered(self, monkeypatch):
        """Nothing was verified, so nothing may prime the next segment."""
        monkeypatch.setattr(
            translator,
            "match_quran_rag_multi",
            lambda text, target_lang_code: [(0.65, V7, "h"), (0.63, V8, "h")],
        )

        class _FakeProvider:
            def complete(self, **kwargs):
                return "GPT out"

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _FakeProvider()
        )
        translator.translate_text("خطبة عن الصبر والشكر في حياة المسلم اليومية")
        assert recitation.expected_next() == []

    def test_predicted_verses_never_reach_the_gpt_prompt(self, monkeypatch):
        """They carry score 0.0 and no text check has passed, so telling GPT
        they are "candidates" would invite exactly the invention this design
        rules out."""
        recitation.note_certified([(81, 7), (81, 8)])
        monkeypatch.setattr(
            translator, "match_quran_rag_multi", lambda text, target_lang_code: []
        )
        calls = {}

        class _FakeProvider:
            def complete(self, **kwargs):
                calls.update(kwargs)
                return "GPT out"

        monkeypatch.setattr(
            translator, "get_translation_provider", lambda: _FakeProvider()
        )
        translator.translate_text("وقال الخطيب في خطبته امس عن اهمية الصلاة")
        assert V9 not in calls["user_prompt"]
        assert V10 not in calls["user_prompt"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

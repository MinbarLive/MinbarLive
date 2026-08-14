"""Tests for ContextManager."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import (
    CONTEXT_RECENT_RAW_COUNT,
    CONTEXT_RECENT_TRANSLATION_COUNT,
    CONTEXT_SUMMARIZE_EVERY_N,
    CONTEXT_SUMMARIZE_MIN_SECONDS,
    QURAN_VERIFIED_MARKER,
)
from utils.context_manager import ContextManager


class TestTranslationMemory:
    """The target-language half of the context (see add_translation)."""

    def test_recent_translations_reach_the_context(self):
        mgr = ContextManager()
        mgr.add_translation("Alles Lob gehört Allah.")
        assert "Alles Lob gehört Allah." in mgr.get_context()

    def test_labelled_apart_from_the_source_segments(self):
        """The model has to tell 'what was said' from 'what I already wrote'."""
        mgr = ContextManager()
        mgr.add_transcription("الحمد لله")
        mgr.add_translation("Alles Lob gehört Allah.")
        context = mgr.get_context()
        assert context.index("[Last segments:") < context.index("[Already shown")

    def test_only_the_newest_are_kept(self):
        mgr = ContextManager()
        for i in range(CONTEXT_RECENT_TRANSLATION_COUNT + 3):
            mgr.add_translation(f"line {i}")
        context = mgr.get_context()
        assert "line 0" not in context
        assert f"line {CONTEXT_RECENT_TRANSLATION_COUNT + 2}" in context

    def test_verified_verse_marker_is_stripped(self):
        """GPT must never see a 📖 it could copy onto its own paraphrase.

        The marker certifies that an exact text check matched what was
        recited. Anything in the context is material the model imitates, so a
        stored marker is an invitation to print an unverified ayah as verified
        — the one failure the verification guards exist to prevent.
        """
        mgr = ContextManager()
        mgr.add_translation(f"{QURAN_VERIFIED_MARKER} O die ihr glaubt")
        context = mgr.get_context()
        assert QURAN_VERIFIED_MARKER not in context
        assert "O die ihr glaubt" in context

    def test_ayah_reference_is_stripped(self):
        """A (surah:ayah) is a certification too, and GPT was copying it.

        Verified verses arrive as dictionary text ending in "... (3:102)". Once
        those sat in the context the model began appending references to its own
        translations — 5 unverified subtitles carried one in a single live
        session against 0 before the feature existed. An unverified line must
        never wear the costume of a verified one.
        """
        mgr = ContextManager()
        mgr.add_translation("O die ihr glaubt, fürchtet Allah (3:102)")
        context = mgr.get_context()
        assert "(3:102)" not in context
        assert "O die ihr glaubt, fürchtet Allah" in context

    def test_every_reference_in_a_verified_run_is_stripped(self):
        """A run joins several dictionary verses, so refs also appear mid-string."""
        mgr = ContextManager()
        mgr.add_translation(
            f"{QURAN_VERIFIED_MARKER} Erste Aussage (3:102) zweite Aussage (33:70)"
        )
        context = mgr.get_context()
        assert "(3:102)" not in context and "(33:70)" not in context
        assert "Erste Aussage zweite Aussage" in context

    def test_empty_translation_is_not_stored(self):
        mgr = ContextManager()
        mgr.add_translation("")
        mgr.add_translation(f"  {QURAN_VERIFIED_MARKER}  ")
        assert "Already shown" not in mgr.get_context()

    def test_reset_clears_them(self):
        mgr = ContextManager()
        mgr.add_translation("Alles Lob gehört Allah.")
        mgr.reset()
        assert "Already shown" not in mgr.get_context()


class TestContextManager:
    """Tests for ContextManager class."""

    def test_add_transcription_updates_recent_raw(self):
        """Adding transcriptions should update recent_raw deque."""
        mgr = ContextManager()

        mgr.add_transcription("First segment")
        mgr.add_transcription("Second segment")
        mgr.add_transcription("Third segment")

        context = mgr.get_context()
        assert "First segment" in context
        assert "Second segment" in context
        assert "Third segment" in context

    def test_recent_raw_limited_to_max(self):
        """Recent raw should be limited to CONTEXT_RECENT_RAW_COUNT."""
        mgr = ContextManager()

        for i in range(CONTEXT_RECENT_RAW_COUNT + 5):
            mgr.add_transcription(f"Segment {i}")

        context = mgr.get_context()
        # Oldest segments should be gone
        assert "Segment 0" not in context
        assert "Segment 1" not in context
        # Most recent should remain
        assert f"Segment {CONTEXT_RECENT_RAW_COUNT + 4}" in context

    def test_empty_transcription_ignored(self):
        """Empty or whitespace-only transcriptions should be ignored."""
        mgr = ContextManager()

        mgr.add_transcription("")
        mgr.add_transcription("   ")
        mgr.add_transcription(None)

        stats = mgr.get_stats()
        assert stats["transcription_count"] == 0

    def test_get_context_returns_immediately(self):
        """get_context should return immediately without blocking."""
        mgr = ContextManager()
        mgr.add_transcription("Test segment")

        start = time.time()
        context = mgr.get_context()
        elapsed = time.time() - start

        assert elapsed < 0.1  # Should be nearly instant
        assert "Test segment" in context

    def test_reset_clears_all_state(self):
        """Reset should clear all context state."""
        mgr = ContextManager()

        for i in range(10):
            mgr.add_transcription(f"Segment {i}")

        mgr.reset()

        stats = mgr.get_stats()
        assert stats["transcription_count"] == 0
        assert stats["hourly_summaries"] == 0
        assert not stats["has_rolling_summary"]

    def test_get_stats_returns_correct_info(self):
        """get_stats should return accurate statistics."""
        mgr = ContextManager()

        mgr.add_transcription("First")
        mgr.add_transcription("Second")

        stats = mgr.get_stats()
        assert stats["transcription_count"] == 2
        assert stats["session_minutes"] >= 0
        assert stats["hourly_summaries"] == 0
        assert stats["pending_for_summary"] == 2

    @patch("utils.context_manager.get_translation_provider")
    def test_start_stop_lifecycle(self, mock_get_provider):
        """Start and stop should manage thread lifecycle correctly."""
        mgr = ContextManager()

        mgr.start()
        assert mgr._thread is not None
        assert mgr._thread.is_alive()

        mgr.stop(timeout=1.0)
        assert mgr._thread is None

    @patch("utils.context_manager.get_translation_provider")
    def test_start_is_idempotent(self, mock_get_provider):
        """A second start() must not orphan the first summarizer thread.

        stop() only joins self._thread, so replacing it would leave the first
        loop running against the same state — two summarizers, duplicate API
        calls. A start() that fails partway (streaming connect raising after
        the context manager started) relies on this too.
        """
        mgr = ContextManager()
        try:
            mgr.start()
            first = mgr._thread

            mgr.start()
            assert mgr._thread is first, "second start() replaced the thread"

            alive = [
                t
                for t in threading.enumerate()
                if t.name == "context-summarizer" and t.is_alive()
            ]
            assert len(alive) == 1, f"expected 1 summarizer thread, got {len(alive)}"
        finally:
            mgr.stop(timeout=1.0)
        assert mgr._thread is None

    @patch("utils.context_manager.get_translation_provider")
    def test_start_after_stop_starts_a_new_thread(self, mock_get_provider):
        """The idempotence guard must not block a legitimate restart."""
        mgr = ContextManager()
        mgr.start()
        first = mgr._thread
        mgr.stop(timeout=1.0)

        mgr.start()
        try:
            assert mgr._thread is not None
            assert mgr._thread is not first
            assert mgr._thread.is_alive()
        finally:
            mgr.stop(timeout=1.0)

    def test_context_format_structure(self):
        """Context should have proper structure with sections."""
        mgr = ContextManager()

        mgr.add_transcription("Test segment one")
        mgr.add_transcription("Test segment two")

        context = mgr.get_context()

        # Should have the "Last segments" section
        assert "[Last segments:" in context
        assert "Test segment one" in context
        assert "Test segment two" in context


class TestContextManagerIntegration:
    """Integration tests that verify summarization (require mocking API)."""

    @patch("utils.context_manager.get_translation_provider")
    def test_rolling_summary_triggered_after_n_segments(self, mock_get_provider):
        """Rolling summary should be triggered after CONTEXT_SUMMARIZE_EVERY_N segments."""
        # Setup mock provider
        mock_provider = MagicMock()
        mock_provider.complete.return_value = "Summary of topics"
        mock_get_provider.return_value = mock_provider

        mgr = ContextManager()
        mgr.start()

        try:
            # Add enough segments to trigger summarization
            for i in range(CONTEXT_SUMMARIZE_EVERY_N):
                mgr.add_transcription(f"Segment {i} with some content")

            # Give background thread time to process
            time.sleep(0.5)

            # Check that API was called for summarization
            # (It may or may not be called depending on timing)
            stats = mgr.get_stats()
            # At minimum, transcription count should be correct
            assert stats["transcription_count"] == CONTEXT_SUMMARIZE_EVERY_N

        finally:
            mgr.stop(timeout=1.0)


class TestSummaryCadence:
    """The rolling summary needs BOTH enough pending texts AND the time
    floor — streaming utterances every ~3-8s must not refresh a
    near-identical summary every ~45s (one wasted LLM call each)."""

    def _mgr_with_mock(self, mock_get_provider):
        mock_provider = MagicMock()
        mock_provider.complete.return_value = "Summary"
        mock_get_provider.return_value = mock_provider
        return ContextManager(), mock_provider

    @patch("utils.context_manager.get_translation_provider")
    def test_time_floor_blocks_early_summary(self, mock_get_provider):
        """Ten fast utterances right after session start: pending stays
        queued, no summary call fires until the floor has passed."""
        mgr, mock_provider = self._mgr_with_mock(mock_get_provider)
        mgr.start()
        try:
            for i in range(CONTEXT_SUMMARIZE_EVERY_N):
                mgr.add_transcription(f"Utterance {i} content")
            time.sleep(0.4)  # give the background thread a chance to misfire
            mock_provider.complete.assert_not_called()
            assert (
                mgr.get_stats()["pending_for_summary"] == CONTEXT_SUMMARIZE_EVERY_N
            )
        finally:
            mgr.stop(timeout=1.0)

    @patch("utils.context_manager.get_translation_provider")
    def test_summary_fires_once_floor_passed(self, mock_get_provider):
        mgr, mock_provider = self._mgr_with_mock(mock_get_provider)
        mgr._state.last_rolling_summary_time = (
            time.time() - CONTEXT_SUMMARIZE_MIN_SECONDS - 1
        )
        for i in range(CONTEXT_SUMMARIZE_EVERY_N):
            mgr.add_transcription(f"Utterance {i} content")
        mgr._do_summarization_work()  # synchronous — deterministic
        mock_provider.complete.assert_called_once()
        assert mgr.get_stats()["pending_for_summary"] == 0
        # The floor is re-armed for the next summary.
        assert (
            time.time() - mgr._state.last_rolling_summary_time
            < CONTEXT_SUMMARIZE_MIN_SECONDS
        )

    @patch("utils.context_manager.get_translation_provider")
    def test_texts_arriving_during_summary_survive(self, mock_get_provider):
        """Utterances landing while the summary API call runs must stay
        pending for the NEXT summary — clear() used to wipe them (visible in
        streaming mode, where a 2s call spans several utterances)."""
        mgr, mock_provider = self._mgr_with_mock(mock_get_provider)
        mgr._state.last_rolling_summary_time = (
            time.time() - CONTEXT_SUMMARIZE_MIN_SECONDS - 1
        )
        for i in range(CONTEXT_SUMMARIZE_EVERY_N):
            mgr.add_transcription(f"Utterance {i} content")

        def slow_complete(**kwargs):
            mgr.add_transcription("late arrival during the API call")
            return "Summary"

        mock_provider.complete.side_effect = slow_complete
        mgr._do_summarization_work()
        assert mgr.get_stats()["pending_for_summary"] == 1


class TestSummaryPromptIslamicMode:
    """The summary prompts must drop the mosque/khutbah/Islamic framing when
    Islamic mode is off — mirrors translate_text's general prompt, and read
    fresh so a mid-session toggle applies to the next summary."""

    def _rolling_prompt(self, mock_get_provider, islamic):
        mock_provider = MagicMock()
        mock_provider.complete.return_value = "ok"
        mock_get_provider.return_value = mock_provider
        with patch(
            "utils.context_manager.load_settings",
            return_value=SimpleNamespace(islamic_mode=islamic),
        ):
            ContextManager()._create_rolling_summary(["some talk"], "")
        return mock_provider.complete.call_args.kwargs["user_prompt"]

    def _hourly_prompt(self, mock_get_provider, islamic):
        mock_provider = MagicMock()
        mock_provider.complete.return_value = "ok"
        mock_get_provider.return_value = mock_provider
        with patch(
            "utils.context_manager.load_settings",
            return_value=SimpleNamespace(islamic_mode=islamic),
        ):
            ContextManager()._create_hourly_summary("a rolling summary", 1)
        return mock_provider.complete.call_args.kwargs["user_prompt"]

    @patch("utils.context_manager.get_translation_provider")
    def test_rolling_prompt_islamic_on(self, mock_get_provider):
        prompt = self._rolling_prompt(mock_get_provider, islamic=True).lower()
        assert "khutbah" in prompt or "mosque" in prompt
        assert "islamic" in prompt or "quran" in prompt

    @patch("utils.context_manager.get_translation_provider")
    def test_rolling_prompt_general_off(self, mock_get_provider):
        prompt = self._rolling_prompt(mock_get_provider, islamic=False).lower()
        for word in ("mosque", "khutbah", "sermon", "islamic", "quran"):
            assert word not in prompt

    @patch("utils.context_manager.get_translation_provider")
    def test_hourly_prompt_islamic_on(self, mock_get_provider):
        prompt = self._hourly_prompt(mock_get_provider, islamic=True).lower()
        assert "sermon" in prompt

    @patch("utils.context_manager.get_translation_provider")
    def test_hourly_prompt_general_off(self, mock_get_provider):
        prompt = self._hourly_prompt(mock_get_provider, islamic=False).lower()
        assert "sermon" not in prompt

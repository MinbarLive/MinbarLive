"""Tests for audio segment buffering strategies."""

import sys
import time
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from translation.buffering import (
    AudioSegment,
    ChunkBasedStrategy,
    SemanticBufferingStrategy,
    _split_for_display,
)


def _segment(text: str, is_silent: bool = False, timestamp: float | None = None):
    return AudioSegment(
        file_path="dummy.wav",
        transcription=text,
        is_silent=is_silent,
        timestamp=timestamp if timestamp is not None else time.time(),
    )


class TestChunkBasedStrategy:
    """Tests for the pass-through chunk strategy."""

    def test_passes_text_through(self):
        strategy = ChunkBasedStrategy()
        assert strategy.add_segment(_segment("hello world")) == ["hello world"]

    def test_skips_empty_transcription(self):
        strategy = ChunkBasedStrategy()
        assert strategy.add_segment(_segment("   ")) == []

    def test_skips_silent_segment(self):
        strategy = ChunkBasedStrategy()
        assert strategy.add_segment(_segment("text", is_silent=True)) == []

    def test_flush_returns_empty(self):
        strategy = ChunkBasedStrategy()
        assert strategy.flush() == []


class TestSemanticBufferingStrategy:
    """Tests for the semantic buffering strategy."""

    def test_buffers_incomplete_text(self):
        strategy = SemanticBufferingStrategy(max_chunks=3, max_seconds=100)
        assert strategy.add_segment(_segment("short text")) == []
        assert len(strategy.buffer) == 1

    def test_flushes_on_sentence_end(self):
        strategy = SemanticBufferingStrategy(max_chunks=3, max_seconds=100)
        strategy.add_segment(_segment("first part"))
        result = strategy.add_segment(_segment("second part."))
        assert result == ["first part second part."]
        assert len(strategy.buffer) == 0

    def test_flushes_on_max_chunks(self):
        strategy = SemanticBufferingStrategy(max_chunks=3, max_seconds=100)
        strategy.add_segment(_segment("one two"))
        strategy.add_segment(_segment("three four"))
        result = strategy.add_segment(_segment("five six"))
        assert result == ["one two three four five six"]

    def test_timeout_with_whitespace_only_segments_resets_cleanly(self):
        """Regression: a timed-out buffer containing only whitespace
        transcriptions used to call the nonexistent _clean_buffer() and raise
        AttributeError (surfacing as a spurious connection-error subtitle)."""
        strategy = SemanticBufferingStrategy(max_chunks=3, max_seconds=10)
        stale = time.time() - 100  # first segment timestamp far in the past
        result = strategy.add_segment(_segment("   ", timestamp=stale))
        assert result == []
        assert len(strategy.buffer) == 0
        assert strategy.start_time is None

    def test_explicit_flush_with_only_whitespace(self):
        strategy = SemanticBufferingStrategy()
        strategy.buffer.append(_segment("  "))
        assert strategy.flush() == []
        assert len(strategy.buffer) == 0

    def test_explicit_flush_returns_buffered_text(self):
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=100)
        strategy.add_segment(_segment("pending words"))
        assert strategy.flush() == ["pending words"]
        assert len(strategy.buffer) == 0

    def test_reset_clears_state(self):
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=100)
        strategy.add_segment(_segment("pending"))
        strategy.reset()
        assert len(strategy.buffer) == 0
        assert strategy.start_time is None

    def test_flush_if_stale_before_timeout_keeps_buffer(self):
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=100)
        strategy.add_segment(_segment("pending words"))
        assert strategy.flush_if_stale() == []
        assert len(strategy.buffer) == 1

    def test_flush_if_stale_after_timeout_flushes(self):
        # During silence no segments arrive, so the timeout must also fire
        # from this controller-driven check, not only inside add_segment.
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=10)
        strategy.add_segment(_segment("pending words"))
        strategy.start_time -= 20  # simulate 20s of pure silence since then
        assert strategy.flush_if_stale() == ["pending words"]
        assert len(strategy.buffer) == 0
        assert strategy.start_time is None

    def test_flush_if_stale_empty_buffer(self):
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=10)
        assert strategy.flush_if_stale() == []

    def test_chunk_strategy_flush_if_stale_is_noop(self):
        assert ChunkBasedStrategy().flush_if_stale() == []

    def test_flush_collapses_internal_whitespace(self):
        # A newline / repeated spaces the STT left inside a segment (or the
        # join between two buffered pieces) must not fuse or break the words.
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=100)
        strategy.add_segment(_segment("first\nline"))
        result = strategy.add_segment(_segment("second   part."))
        assert result == ["first line second part."]

    def test_long_multisentence_flush_is_split(self):
        # A dense flush carrying several sentences is broken into readable
        # subtitles at sentence ends.
        sent1 = " ".join(f"a{i}" for i in range(20)) + "."
        sent2 = " ".join(f"b{i}" for i in range(20)) + "."
        strategy = SemanticBufferingStrategy(max_chunks=5, max_seconds=100)
        strategy.buffer.append(_segment(f"{sent1} {sent2}"))
        result = strategy.flush()
        assert result == [sent1, sent2]


class TestSplitForDisplay:
    """The sentence-boundary splitter for over-long flushes."""

    def test_short_text_unchanged(self):
        assert _split_for_display("short and sweet.") == ["short and sweet."]

    def test_single_long_runon_sentence_kept_whole(self):
        # No internal sentence end → never fragmented, returned as one piece.
        text = " ".join(f"w{i}" for i in range(40))
        assert _split_for_display(text, max_words=28) == [text]

    def test_packs_whole_sentences_up_to_cap(self):
        sent1 = " ".join(f"a{i}" for i in range(20)) + "."
        sent2 = " ".join(f"b{i}" for i in range(20)) + "."
        assert _split_for_display(f"{sent1} {sent2}", max_words=28) == [sent1, sent2]

    def test_no_content_lost_on_split(self):
        sents = [" ".join(f"s{g}w{i}" for i in range(15)) + "." for g in range(4)]
        text = " ".join(sents)
        pieces = _split_for_display(text, max_words=28)
        assert len(pieces) > 1
        assert " ".join(pieces) == text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

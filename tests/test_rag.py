"""Tests for RAG (Retrieval-Augmented Generation) functions."""

import sys
from pathlib import Path

import numpy as np
import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from translation.rag import is_rag_available


class TestRagAvailability:
    """Tests for RAG availability checking."""

    def test_is_rag_available_returns_bool(self):
        """is_rag_available should return a boolean."""
        result = is_rag_available()
        assert isinstance(result, bool)


class TestMatchQuranRagMulti:
    """Tests for vectorized verse matching against a small fake store."""

    @pytest.fixture
    def rag_with_fake_store(self, monkeypatch):
        from translation import rag

        verses = ["verse_a", "verse_b", "verse_c"]
        # Identity rows: score against query == corresponding query component
        matrix = np.eye(3, dtype=np.float32)
        monkeypatch.setattr(rag, "_verses", verses)
        monkeypatch.setattr(rag, "_verse_matrix", matrix)
        monkeypatch.setattr(
            rag, "_verse_index", {v: i for i, v in enumerate(verses)}
        )
        monkeypatch.setattr(
            rag,
            "quran_dict",
            {
                "verse_a": "trans_a (1:1)",
                "verse_b": "trans_b (1:2)",
                "verse_c": "trans_c (1:3)",
            },
        )
        return rag

    def _set_query(self, monkeypatch, rag, vector):
        monkeypatch.setattr(
            rag,
            "get_text_embedding",
            lambda text: np.array(vector, dtype=np.float32),
        )

    def test_matches_sorted_by_score(self, rag_with_fake_store, monkeypatch):
        rag = rag_with_fake_store
        self._set_query(monkeypatch, rag, [0.9, 0.5, 0.0])
        matches = rag.match_quran_rag_multi(
            "text", min_similarity=0.3, top_k=5, target_lang_code="xx"
        )
        assert [m[1] for m in matches] == ["verse_a", "verse_b"]
        assert matches[0][0] > matches[1][0]
        assert matches[0][2] == "trans_a (1:1)"

    def test_top_k_limits_results(self, rag_with_fake_store, monkeypatch):
        rag = rag_with_fake_store
        self._set_query(monkeypatch, rag, [0.9, 0.8, 0.7])
        matches = rag.match_quran_rag_multi(
            "text", min_similarity=0.1, top_k=1, target_lang_code="xx"
        )
        assert len(matches) == 1
        assert matches[0][1] == "verse_a"

    def test_threshold_filters_all(self, rag_with_fake_store, monkeypatch):
        rag = rag_with_fake_store
        self._set_query(monkeypatch, rag, [0.5, 0.4, 0.3])
        matches = rag.match_quran_rag_multi(
            "text", min_similarity=0.95, top_k=5, target_lang_code="xx"
        )
        assert matches == []

    def test_empty_text_returns_empty(self, rag_with_fake_store):
        assert rag_with_fake_store.match_quran_rag_multi("  ") == []

    def test_unavailable_store_returns_empty(self, rag_with_fake_store, monkeypatch):
        rag = rag_with_fake_store
        monkeypatch.setattr(rag, "_verse_matrix", None)
        assert rag.match_quran_rag_multi("text", target_lang_code="xx") == []

    def test_failed_query_embedding_returns_empty(
        self, rag_with_fake_store, monkeypatch
    ):
        rag = rag_with_fake_store
        # get_text_embedding returns size-1 zero vector on API failure
        self._set_query(monkeypatch, rag, [0.0])
        assert rag.match_quran_rag_multi("text", target_lang_code="xx") == []

    def test_arabic_target_returns_verse_itself(
        self, rag_with_fake_store, monkeypatch
    ):
        rag = rag_with_fake_store
        self._set_query(monkeypatch, rag, [1.0, 0.0, 0.0])
        matches = rag.match_quran_rag_multi(
            "text", min_similarity=0.5, top_k=5, target_lang_code="ar"
        )
        assert matches[0][2] == "verse_a"

    def test_verse_missing_from_dict_is_skipped(
        self, rag_with_fake_store, monkeypatch
    ):
        rag = rag_with_fake_store
        monkeypatch.setattr(rag, "quran_dict", {"verse_b": "trans_b (1:2)"})
        self._set_query(monkeypatch, rag, [0.9, 0.8, 0.0])
        matches = rag.match_quran_rag_multi(
            "text", min_similarity=0.3, top_k=5, target_lang_code="xx"
        )
        assert [m[1] for m in matches] == ["verse_b"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

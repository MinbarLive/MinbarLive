"""RAG (Retrieval-Augmented Generation) for Quran verse matching using embeddings.

The embeddings are language-agnostic (based on Arabic text).
Translations are loaded dynamically based on target language.

Verse embeddings are stored as an L2-normalized float32 matrix in
quran_embeddings_openai.npz (built by notebooks/build_embeddings_npz.py), so
each query is a single matrix-vector product instead of a per-verse Python
loop.
The raw quran_embeddings.json (notebook output) is supported as a slow
fallback when the .npz is missing.

Per-provider embedding spaces: the query embedding and the verse matrix must
come from the same model, so the store follows providers.get_embedding_space()
— the optional Gemini matrix (quran_embeddings_gemini.npz) is used when
ai_provider is gemini and that file exists; everything else uses the shipped
OpenAI matrix. A live ai_provider switch reloads the store on the next query.
"""

from __future__ import annotations

import os
import re

import numpy as np

from config import (
    QURAN_EMBEDDINGS_GEMINI_NPZ_PATH,
    QURAN_EMBEDDINGS_OPENAI_NPZ_PATH,
    QURAN_EMBEDDINGS_PATH,
    RAG_MIN_SIMILARITY,
    RAG_TOP_K,
)
from providers import get_embedding_model, get_embedding_provider, get_embedding_space
from translation.dictionary import get_quran_dict, has_quran_translation, quran_dict
from utils.json_helpers import load_json
from utils.logging import log
from utils.settings import get_target_language_code, load_settings

_NPZ_PATHS = {
    "openai": QURAN_EMBEDDINGS_OPENAI_NPZ_PATH,
    "gemini": QURAN_EMBEDDINGS_GEMINI_NPZ_PATH,
}


def _extract_ayah_reference(translation: str) -> str:
    """
    Extract the (surah:ayah) reference from the translation text.

    Args:
        translation: Translation text that may contain (X:Y) pattern.

    Returns:
        The reference string like "(2:255)" or empty string if not found.
    """
    match = re.search(r"\((\d+:\d+)\)\s*$", translation)
    if match:
        return f"({match.group(1)})"
    return ""


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize matrix rows in place (zero rows are left untouched)."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms
    return matrix


def _load_npz(npz_path: str) -> tuple[list[str], np.ndarray | None]:
    """Load verses + normalized embedding matrix from an .npz archive."""
    try:
        data = np.load(npz_path, allow_pickle=False)
        verses = [str(v) for v in data["verses"]]
        matrix = np.asarray(data["embeddings"], dtype=np.float32)
    except Exception as e:
        log(f"Failed to load {npz_path}: {e}", level="ERROR")
        return [], None

    if matrix.ndim != 2 or matrix.shape[0] != len(verses) or matrix.shape[1] < 100:
        log(
            f"Invalid embedding matrix in {npz_path} "
            f"(shape {matrix.shape}, {len(verses)} verses)",
            level="ERROR",
        )
        return [], None

    return verses, matrix


def _load_json_legacy() -> tuple[list[str], np.ndarray | None]:
    """Load verses + embedding matrix from the legacy JSON file (slow)."""
    embeddings = load_json(QURAN_EMBEDDINGS_PATH)
    if not embeddings:
        log("Quran embeddings file is empty.", level="WARNING")
        return [], None

    first_val = next(iter(embeddings.values()))
    if not isinstance(first_val, list) or len(first_val) < 100:
        log(f"Invalid embedding format in {QURAN_EMBEDDINGS_PATH}", level="ERROR")
        return [], None

    verses = list(embeddings.keys())
    matrix = np.array([embeddings[v] for v in verses], dtype=np.float32)
    return verses, _normalize_rows(matrix)


def _load_verse_store(space: str) -> tuple[list[str], np.ndarray | None]:
    """Load the verse embedding store for one embedding space."""
    npz_path = _NPZ_PATHS.get(space, QURAN_EMBEDDINGS_OPENAI_NPZ_PATH)
    if os.path.exists(npz_path):
        verses, matrix = _load_npz(npz_path)
        if matrix is not None:
            log(f"Loaded {len(verses)} Quran embeddings (npz, {space}).", level="INFO")
            return verses, matrix

    # The legacy raw-JSON fallback only exists for the OpenAI space.
    if space == "openai" and os.path.exists(QURAN_EMBEDDINGS_PATH):
        log(
            "quran_embeddings_openai.npz not found — loading legacy JSON (slow). "
            "Run notebooks/build_embeddings_npz.py to speed up startup.",
            level="WARNING",
        )
        verses, matrix = _load_json_legacy()
        if matrix is not None:
            log(f"Loaded {len(verses)} Quran embeddings (legacy json).", level="INFO")
            return verses, matrix

    log(
        f"No Quran embeddings found for the {space} space ({npz_path}). "
        "RAG-based Quran matching will be disabled.",
        level="WARNING",
    )
    return [], None


# Precomputed verse store (read-only between loads).
# _verse_matrix rows are L2-normalized, so cosine similarity against a
# normalized query vector is a single matrix-vector product. The store is
# keyed by embedding space and reloads if the space changes at runtime
# (ai_provider switch) — see _ensure_store().
_loaded_space: str | None = None
_verses: list[str] = []
_verse_matrix: np.ndarray | None = None
_verse_index: dict[str, int] = {}


def _ensure_store() -> None:
    """(Re)load the verse store when the active embedding space changed."""
    global _loaded_space, _verses, _verse_matrix, _verse_index
    space = get_embedding_space()
    if space == _loaded_space:
        return
    _verses, _verse_matrix = _load_verse_store(space)
    _verse_index = {verse: i for i, verse in enumerate(_verses)}
    _loaded_space = space

    if _verse_matrix is not None and quran_dict:
        missing = sum(1 for ar in quran_dict if ar not in _verse_index)
        if missing > 0:
            log(
                f"{missing}/{len(quran_dict)} Quran verses missing embeddings.",
                level="WARNING",
            )


_ensure_store()


def is_rag_available() -> bool:
    """Check if RAG-based Quran matching is available."""
    _ensure_store()
    return _verse_matrix is not None


def get_text_embedding(text: str) -> np.ndarray:
    """
    Get embedding for arbitrary text from the embedding provider.

    Args:
        text: Text to embed.

    Returns:
        Embedding as numpy float32 array.
    """
    text = (text or "").strip()
    if not text:
        return np.zeros(1, dtype=np.float32)

    try:
        vector = get_embedding_provider().embed(text, model=get_embedding_model())
        return np.array(vector, dtype=np.float32)
    except Exception as e:
        log(f"ERROR get_text_embedding: {e}", level="ERROR")
        return np.zeros(1, dtype=np.float32)


def match_quran_rag_multi(
    text: str,
    min_similarity: float | None = None,
    top_k: int | None = None,
    target_lang_code: str | None = None,
) -> list:
    """
    RAG matching for Quran verses:
    - Embed the input text
    - Score against all verse embeddings with one matrix-vector product
    - Return top matches above threshold with translations in target language

    Args:
        text: Arabic text to match.
        min_similarity: Minimum similarity score (default: RAG_MIN_SIMILARITY).
        top_k: Maximum number of matches to return (default: RAG_TOP_K).
        target_lang_code: Target language code for translations.
                         If None, uses current settings.
                         For 'ar', returns Arabic verse as translation.

    Returns:
        List of (score, arabic_verse, translation) tuples, sorted by score.
    """
    if min_similarity is None:
        min_similarity = RAG_MIN_SIMILARITY
    if top_k is None:
        top_k = RAG_TOP_K
    if target_lang_code is None:
        target_lang_code = (
            get_target_language_code(load_settings().target_language) or "de"
        )

    _ensure_store()
    txt = (text or "").strip()
    if not txt or not quran_dict or _verse_matrix is None:
        return []

    query_emb = get_text_embedding(txt)
    if query_emb.size <= 1:
        return []

    query_norm = query_emb / (np.linalg.norm(query_emb) + 1e-8)
    scores = _verse_matrix @ query_norm

    candidate_idx = np.flatnonzero(scores >= min_similarity)
    if candidate_idx.size == 0:
        return []
    ranked_idx = candidate_idx[np.argsort(scores[candidate_idx])[::-1]]

    # Get translation dict for target language (fallback to German for reference)
    target_dict = (
        get_quran_dict(target_lang_code)
        if has_quran_translation(target_lang_code)
        else None
    )

    matches = []
    for i in ranked_idx:
        ar = _verses[i]
        # Only verses present in the reference dictionary are usable as hints
        if ar not in quran_dict:
            continue

        # For Arabic target, return the Arabic verse itself
        if target_lang_code == "ar":
            trans = ar
        # For other languages, try target language, fallback to German
        elif target_dict and ar in target_dict:
            trans = target_dict[ar]
        else:
            trans = quran_dict.get(ar, ar)  # German or Arabic fallback

        matches.append((float(scores[i]), ar, trans))
        if len(matches) >= top_k:
            break

    for score, ar, trans in matches:
        ayah_ref = _extract_ayah_reference(trans)
        log(
            f"Quran-RAG match: Score={score:.3f} {ayah_ref} | AR='{ar[:40]}...'",
            level="INFO",
        )

    return matches

"""Build the compact quran_embeddings .npz verse matrices the app loads.

Switch the PROVIDER variable below and rerun:

  PROVIDER = "openai"  (default)
      Converts the raw quran_embeddings.json (output of
      Build_Quran_EmbeddingSpace.ipynb, text-embedding-3-large) into
      data/embeddings/quran_embeddings_openai.npz. No API calls.

  PROVIDER = "gemini"
      Re-embeds all verse texts with Gemini (gemini-embedding-001) and
      writes data/embeddings/quran_embeddings_gemini.npz. Needs a Gemini
      API key (OS keychain entry from the app, or GEMINI_API_KEY /
      GOOGLE_API_KEY env). Verse texts are taken from the existing OpenAI
      .npz, so build that one first. Costs one embedding call per verse
      (6,054 verses, batched).

The app picks the matrix automatically: when ai_provider is gemini AND the
Gemini .npz exists, RAG runs fully in the Gemini embedding space (no OpenAI
key needed); otherwise it uses the OpenAI space (see
providers.get_embedding_space()).

Each .npz contains:
  - "verses":     unicode array of Arabic verse texts (row order = matrix rows)
  - "embeddings": float32 matrix (N x D), rows L2-normalized
  - "model":      the embedding model id the matrix was built with

Run:
    python notebooks/build_embeddings_npz.py
"""

from __future__ import annotations

import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import (  # noqa: E402
    GEMINI_EMBEDDING_MODEL,
    QURAN_EMBEDDINGS_GEMINI_NPZ_PATH,
    QURAN_EMBEDDINGS_OPENAI_NPZ_PATH,
    QURAN_EMBEDDINGS_PATH,
)

# ── Switch me: "openai" or "gemini" ──────────────────────────────────────
PROVIDER = "gemini"

_GEMINI_BATCH_SIZE = 100
_GEMINI_MAX_RETRIES = 3


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms
    return matrix


def _write_npz(npz_path: str, verses: list[str], matrix: np.ndarray, model: str):
    print(f"  matrix shape: {matrix.shape}, dtype: {matrix.dtype}")
    np.savez(
        npz_path.removesuffix(".npz"),
        verses=np.array(verses),
        embeddings=matrix,
        model=np.array(model),
    )
    size_mb = os.path.getsize(npz_path) / 1024 / 1024
    print(f"Written {npz_path} ({size_mb:.1f} MB)")

    t0 = time.time()
    loaded = np.load(npz_path, allow_pickle=False)
    assert loaded["embeddings"].shape == matrix.shape
    assert len(loaded["verses"]) == len(verses)
    print(f"Verified: reloads in {time.time() - t0:.2f}s")


def build_openai() -> None:
    """Convert the raw notebook JSON to the OpenAI-space .npz."""
    if not os.path.exists(QURAN_EMBEDDINGS_PATH):
        sys.exit(f"Source JSON not found: {QURAN_EMBEDDINGS_PATH}")

    print(f"Loading {QURAN_EMBEDDINGS_PATH} ...")
    t0 = time.time()
    with open(QURAN_EMBEDDINGS_PATH, encoding="utf-8") as f:
        data: dict[str, list[float]] = json.load(f)
    print(f"  {len(data)} verses loaded in {time.time() - t0:.1f}s")

    verses = list(data.keys())
    matrix = _normalize_rows(np.array([data[v] for v in verses], dtype=np.float32))
    _write_npz(QURAN_EMBEDDINGS_OPENAI_NPZ_PATH, verses, matrix, "text-embedding-3-large")


def build_gemini() -> None:
    """Re-embed all verses with Gemini into the Gemini-space .npz."""
    if not os.path.exists(QURAN_EMBEDDINGS_OPENAI_NPZ_PATH):
        sys.exit(
            f"OpenAI .npz not found ({QURAN_EMBEDDINGS_OPENAI_NPZ_PATH}) — the verse "
            'texts come from it. Build with PROVIDER = "openai" first.'
        )
    data = np.load(QURAN_EMBEDDINGS_OPENAI_NPZ_PATH, allow_pickle=False)
    verses = [str(v) for v in data["verses"]]
    print(f"Embedding {len(verses)} verses with {GEMINI_EMBEDDING_MODEL} ...")

    from providers.gemini.client import get_client  # uses keychain/env key

    client = get_client()
    vectors: list[list[float]] = []
    t0 = time.time()
    for start in range(0, len(verses), _GEMINI_BATCH_SIZE):
        batch = verses[start : start + _GEMINI_BATCH_SIZE]
        for attempt in range(1, _GEMINI_MAX_RETRIES + 1):
            try:
                resp = client.models.embed_content(
                    model=GEMINI_EMBEDDING_MODEL, contents=batch
                )
                vectors.extend(list(e.values) for e in resp.embeddings)
                break
            except Exception as exc:
                if attempt == _GEMINI_MAX_RETRIES:
                    raise
                wait = 5 * attempt
                print(f"  batch at {start} failed ({exc}); retrying in {wait}s")
                time.sleep(wait)
        done = min(start + _GEMINI_BATCH_SIZE, len(verses))
        print(f"  {done}/{len(verses)}  ({time.time() - t0:.0f}s)")

    if len(vectors) != len(verses):
        sys.exit(f"Got {len(vectors)} embeddings for {len(verses)} verses — aborting.")

    matrix = _normalize_rows(np.array(vectors, dtype=np.float32))
    _write_npz(
        QURAN_EMBEDDINGS_GEMINI_NPZ_PATH, verses, matrix, GEMINI_EMBEDDING_MODEL
    )
    print(
        "\nDone. The app uses this matrix automatically whenever the AI "
        "provider is set to Gemini."
    )


def main() -> None:
    if PROVIDER == "openai":
        build_openai()
    elif PROVIDER == "gemini":
        build_gemini()
    else:
        sys.exit(f'Unknown PROVIDER "{PROVIDER}" — use "openai" or "gemini".')


if __name__ == "__main__":
    main()

---
name: rebuild-embeddings
description: Regenerate the Quran verse embedding matrices (.npz) that RAG searches. Use when EMBEDDING_MODEL changes, when adding or repairing an embedding space for a provider, or when RAG returns nonsense matches. Costs real API money — read before running.
---

# Rebuilding the Quran embedding space

RAG works by one matrix-vector product against precomputed verse embeddings. **The matrix
must have been built with the same model the app embeds queries with.** Mismatch it and
RAG doesn't error — it silently returns meaningless verses, which is far worse.

## The files

| File | Role |
| --- | --- |
| `data/embeddings/quran_embeddings_openai.npz` | The shipped default space. `text-embedding-3-large`, 6,054 verses × 3072 dims |
| `data/embeddings/quran_embeddings_gemini.npz` | Optional. Used **only** when `ai_provider=gemini` **and** the file exists |
| `data/embeddings/quran_embeddings.json` | Raw notebook output, ~418 MB, git LFS. **Not** bundled into the EXE |

Each `.npz` holds `verses` (unicode array, row order = matrix rows), `embeddings`
(float32 N×D, rows L2-normalized) and `model` (the model id it was built with).
`providers.get_embedding_space()` picks between them.

## When you actually need this

- `EMBEDDING_MODEL` in `config.py` changed → **required**, or RAG breaks.
- Building the Gemini space for the first time.
- The `.npz` is corrupt or the row count is wrong.

Not needed when adding a translation language — verse *text* is Arabic and unchanged.

## Rebuilding the OpenAI space (the shipped default)

Two steps, and only the first costs money.

1. Run `notebooks/Build_Quran_EmbeddingSpace.ipynb`. Needs an OpenAI API key. Embeds all
   6,054 verses and writes the ~418 MB `quran_embeddings.json`. **~$0.20.**
2. Convert to the matrix the app loads — no API calls:
   ```bash
   python notebooks/build_embeddings_npz.py
   ```

If `quran_embeddings.json` is already current, step 2 alone is enough.

## Rebuilding the Gemini space

Set `PROVIDER = "gemini"` at the top of `notebooks/build_embeddings_npz.py`, then run it.
It re-embeds every verse with `gemini-embedding-001` (one call per verse, batched — this
one *does* cost) and writes `quran_embeddings_gemini.npz`.

**Verse texts are read from the existing OpenAI `.npz`, so that file must exist first.**

Needs a Gemini key: the app's keychain entry, or `GEMINI_API_KEY` / `GOOGLE_API_KEY` in
the environment.

## Verify before committing

```bash
python -c "
import numpy as np
d = np.load('data/embeddings/quran_embeddings_openai.npz', allow_pickle=True)
e = d['embeddings']
print('model :', d['model'])
print('shape :', e.shape)
print('verses:', len(d['verses']))
print('dtype :', e.dtype)
print('norms :', np.linalg.norm(e[:100], axis=1).round(4).min(), '-', np.linalg.norm(e[:100], axis=1).round(4).max())
"
```

Expected: 6,054 verses (the source merges some ayahs — it is *not* the canonical 6,236),
rows == verse count, `float32`, norms all 1.0, and `model` matching `EMBEDDING_MODEL`.

Then a real retrieval check:

```bash
python -m pytest tests/test_rag.py -q
```

And confirm a known verse still matches above `RAG_MIN_SIMILARITY` — a matrix that loads
cleanly can still be in the wrong space.

## Watch for

- **The `.json` is git LFS.** Don't commit it un-LFS'd; don't let it into the EXE — the
  spec filters it out of `a.datas` deliberately.
- **CI verifies the matrices are real** before building (`Verify the matrices are real` in
  `release.yml`) — an LFS pointer file instead of content fails the release.
- Both `.npz` files together are ~182 MB and *are* bundled. Keep it that way.

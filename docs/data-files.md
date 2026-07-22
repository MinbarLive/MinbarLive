# Data Files

## Quran Translation Source

The translation database is built programmatically via public APIs:

- **Arabic text** (without diacritics) from `quranapi.pages.dev`
- **Translations** from `quranenc.com`

The dataset contains **6,054 verses**; the source merges some ayahs, so this differs from the canonical count of 6,236; the embeddings cover all of them 1:1.

### Available Quran Translations

| Language | File      | Translation Key       | Source            |
| -------- | --------- | --------------------- | ----------------- |
| German   | `de.json` | `german_bubenheim`    | Bubenheim & Elyas |
| English  | `en.json` | `english_hilali_khan` | Hilali & Khan     |
| Turkish  | `tr.json` | `turkish_rwwad`       | Rwwad Center      |
| Albanian | `sq.json` | `albanian_nahi`       | Sherif Ahmeti     |
| Bosnian  | `bs.json` | `bosnian_rwwad`       | Rwwad Center      |

### Available Athan Translations

| Language | File      |
| -------- | --------- |
| German   | `de.json` |
| English  | `en.json` |
| Turkish  | `tr.json` |
| Albanian | `sq.json` |
| Bosnian  | `bs.json` |

To add a new language, use `notebooks/build_quran_dict.py` with a translation key from [quranenc.com](https://quranenc.com).

> **Note:** For languages without a curated dictionary, the app falls back to GPT's translation capabilities. Contributions for additional language sources are welcome!

> **Note on Hadith:** Unfortunately, we could not find a suitable open database for Hadith translations with the same quality and accessibility. Contributions for Hadith support are welcome!

---

## Verse Embeddings (Mini-RAG Database)

`data/embeddings/` contains precomputed vector embeddings for all 6,054 Quran verses. Instead of using an external vector database (like Pinecone, Weaviate, or ChromaDB), the embeddings are stored as a single NumPy matrix, a lightweight "mini-RAG" that lives fully in memory.

| File                           | Purpose                                                                                     |
| ------------------------------ | ------------------------------------------------------------------------------------------- |
| `quran_embeddings_gemini.npz`  | **What the app loads with the Gemini provider**: Gemini-space matrix (`gemini-embedding-001`); used when the AI provider is Gemini and this file exists |
| `quran_embeddings_openai.npz`  | OpenAI-space matrix, used for every other provider: verse texts + L2-normalized float32 matrix, 6,054 × 3072 (OpenAI `text-embedding-3-large`) |
| `quran_embeddings.json`        | Raw notebook output (~418 MB, git LFS): source of truth for rebuilding the `.npz`, **not** bundled into the EXE |

### Why this approach?

- No external database dependencies required
- The dataset is small enough (~6,054 verses, ~86 MB as `.npz`) to fit in memory and load in ~0.1 s
- Simple deployment: a single file, works offline once loaded
- Matching one query is a single matrix-vector product (~3 ms)

### How it works

1. Audio is transcribed to Arabic text (for non-Arabic sources, a secondary Arabic pass feeds the matcher)
2. The text is embedded with the embedding model matching the active space
3. **Cosine similarity** is computed against all stored verse embeddings in one matrix-vector product
4. The result decides what happens:
   - **similarity ≥ 0.85** and the segment length matches the verse → **verified-verse bypass**: the exact dictionary translation (e.g. Bubenheim & Elyas) is displayed with the 📖 marker, no LLM call
   - **similarity ≥ 0.60** → the top matches (up to 5) are passed to the LLM as translation hints
   - below → normal translation without hints

This ensures that when Quran is recited, the published translation is used rather than an AI paraphrase.

> Query embeddings and the verse matrix must live in the **same vector space**; that is why the embedding provider is pinned to the space of the loaded `.npz` and does not blindly follow the AI provider setting (see [providers.md](providers.md)).

---

## Translation Dictionaries

Translation files are organized by language under `data/translations/`:

```
data/translations/
├── quran/
│   ├── de.json    # German (Bubenheim & Elyas)
│   ├── en.json    # English (Hilali & Khan)
│   ├── tr.json    # Turkish (Rwwad Center)
│   ├── sq.json    # Albanian (Sherif Ahmeti)
│   └── bs.json    # Bosnian (Rwwad Center)
├── athan/
│   ├── de.json    # German
│   ├── en.json    # English
│   ├── tr.json    # Turkish
│   ├── sq.json    # Albanian
│   └── bs.json    # Bosnian
├── gui/           # GUI interface translations
│   ├── de.json    # German
│   ├── en.json    # English
│   ├── ar.json    # Arabic
│   ├── bs.json    # Bosnian
│   ├── sq.json    # Albanian
│   └── tr.json    # Türkçe
├── footer_translations.json   # Subtitle disclaimer footer, per language
└── status_messages.json       # Audience-facing status/error messages, per target language
```

### status_messages.json

Anything shown to the **audience** on the subtitle window (e.g. connection errors) comes from this file, localized to the current *target* language, never hardcoded. New target-language entries belong here.

### Adding a new target language (Quran + Athan)

1. Create `data/translations/quran/{lang_code}.json` with Arabic → translation mappings
   - Use `notebooks/build_quran_dict.py` with the appropriate translation key from quranenc.com
2. Create `data/translations/athan/{lang_code}.json` for Athan phrases
3. No code changes needed; the files are auto-detected when the target language matches

## Adding a New GUI Language

To add a new interface language:

1. **Create the translation file:**
   - Copy `data/translations/gui/en.json` as a template
   - Translate all values (keep the JSON keys unchanged)
   - Save as `data/translations/gui/{lang_code}.json`

2. **Register the language:**
   - Open `utils/settings.py`
   - Add a tuple to `GUI_LANGUAGES`: `("xx", "Language Name")`

Example for adding French:

```python
GUI_LANGUAGES = [
    ("de", "Deutsch"),
    ("en", "English"),
    ("ar", "العربية"),
    ("bs", "Bosanski"),
    ("fr", "Français"),  # ← New language
    ("sq", "Shqip"),
    ("tr", "Türkçe"),
]
```

That's it! The dropdown will automatically show the new language.

---

## Regenerating Embeddings

Two steps: the notebook produces the raw JSON, the script builds the `.npz` the app actually loads:

1. Run [notebooks/Build_Quran_EmbeddingSpace.ipynb](../notebooks/Build_Quran_EmbeddingSpace.ipynb) (requires `OPENAI_API_KEY`, costs ~$0.20, takes ~10-15 minutes)
2. Run `python notebooks/build_embeddings_npz.py` to rebuild `data/embeddings/quran_embeddings_openai.npz`

This is required whenever the embedding model (`EMBEDDING_MODEL` in `config.py`) changes.

**Gemini space (optional):** set `PROVIDER = "gemini"` in `build_embeddings_npz.py` and run it; it re-embeds all verse texts via `gemini-embedding-001` (requires a Gemini key) into `quran_embeddings_gemini.npz`. With that file present, Gemini users run verse matching entirely on Gemini.

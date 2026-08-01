---
name: add-language
description: Add a new language to MinbarLive — either a GUI/interface language (control panel strings) or a translation target language (Quran verse + Athan phrase dictionaries), or both. Use when asked to "add Urdu", "support French", "translate the UI into X", or when a language appears in one list but not the other.
---

# Adding a language

Two independent things share the word "language". Work out which one is being asked for:

| | GUI language | Target language |
| --- | --- | --- |
| What it changes | The control panel's own labels | What the audience reads on the subtitle overlay |
| Lives in | `data/translations/gui/{code}.json` | `data/translations/quran/{code}.json` + `data/translations/athan/{code}.json` |
| Registered in | `GUI_LANGUAGES` in `utils/settings.py` | Nothing — auto-detected at runtime |
| Currently | de, en, ar, bs, sq, tr | de, en, tr, sq, bs |

Note the asymmetry: **`ar` is a GUI language but has no Quran/Athan dictionary** (the
source text is already Arabic). That is correct, not a gap.

---

## A. GUI language

1. Copy `data/translations/gui/en.json` to `data/translations/gui/{code}.json`.
2. Translate the **values**. Keep every key exactly as-is — a missing key falls back and
   shows English mid-panel.
3. Add the entry to `GUI_LANGUAGES` in `utils/settings.py`, as `("xx", "Native Name")`.
   Use the language's own name (`Türkçe`, not `Turkish`) — that list is what the dropdown
   renders.

No other code changes.

**If the language is RTL**, check that it renders in dropdowns — the Tk tree needs
`reshape_rtl` on those strings. `ar` is the existing precedent; follow what it does.

## B. Target language (Quran + Athan)

1. Find the translation key on [quranenc.com](https://quranenc.com). Keys already in use:
   `german_bubenheim`, `english_hilali_khan`, `turkish_rwwad`, `albanian_nahi`,
   `bosnian_rwwad`.
2. Edit `notebooks/build_quran_dict.py` — set `language` and `translation_key` at the top
   of the CONFIGURATION block, then run it from inside `notebooks/`:
   ```bash
   cd notebooks && python build_quran_dict.py
   ```
   It fetches all 114 suras from `quranapi.pages.dev` (Arabic) and `quranenc.com`
   (translation) and writes `../data/translations/quran/{code}.json`. It is rate-limited
   and takes a few minutes.
3. Hand-write `data/translations/athan/{code}.json` — copy the shape from `de.json`. This
   is a small fixed set of call-to-prayer phrases; it is not generated.

No code changes and no registration — both directories are scanned at runtime.

---

## Verify

```bash
python -m pytest tests/test_dictionary.py -q
python -c "import json,glob; [json.load(open(f,encoding='utf-8')) for f in glob.glob('data/translations/**/*.json',recursive=True)]"
```

Then check key parity against English, which is the reference set:

```bash
python -c "
import json
en = json.load(open('data/translations/gui/en.json', encoding='utf-8'))
new = json.load(open('data/translations/gui/XX.json', encoding='utf-8'))
print('missing:', sorted(set(en) - set(new)))
print('extra:  ', sorted(set(new) - set(en)))
"
```

Both lists must be empty. Finally, launch the app and switch to the new language — a
too-long string in a fixed-width control is the usual visual break, and only a real run
shows it.

## Watch for

- **JSON must be UTF-8 without BOM.** A BOM makes the loader fail on the first key.
- **Don't translate placeholder tokens** (`{name}`, `{count}`) or the keys themselves.
- Verse translations are ~6,054 entries; the file is large but still plain JSON — don't
  reach for a database.

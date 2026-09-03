---
name: add-language
description: Add or remove a language in MinbarLive — a GUI/interface language (control panel strings), a translation TARGET language (Quran verse + Athan phrase dictionaries), or a SPOKEN/source language (what the STT engines transcribe), or any combination. Use when asked to "add Urdu", "support French", "translate the UI into X", "why can't I pick Somali", when an engine gains or loses a language upstream, or when a language appears in one list but not another.
---

# Adding a language

**Three** independent things share the word "language". Work out which one is being asked
for — they share almost nothing:

| | GUI language | Target language | **Spoken (source) language** |
| --- | --- | --- | --- |
| What it changes | The control panel's own labels | What the audience reads on the overlay | What the STT engine is told to listen for |
| Lives in | `data/translations/gui/{code}.json` | `data/translations/quran/{code}.json` + `.../athan/{code}.json` | Nothing on disk — it is a code sent to the API |
| Registered in | `GUI_LANGUAGES` (`utils/settings.py`) | Nothing — auto-detected at runtime | `SOURCE_LANGUAGES` (`utils/settings.py`) **and** the per-engine sets |
| Currently | de, en, ar, bs, sq, tr | de, en, tr, sq, bs | 68 + Automatic, **filtered per engine** |

Note the asymmetry: **`ar` is a GUI language but has no Quran/Athan dictionary** (the
source text is already Arabic). That is correct, not a gap.

**Adding a language to one list does not add it to the others**, and that is usually
intended — the app transcribes far more languages than it has verse dictionaries for.

---

## A. GUI language

1. Copy `data/translations/gui/en.json` to `data/translations/gui/{code}.json`.
2. Translate the **values**. Keep every key exactly as-is — a missing key falls back and
   shows English mid-panel.
3. Add the entry to `GUI_LANGUAGES` in `utils/settings.py`, as `("xx", "Native Name")`.
   Use the language's own name (`Türkçe`, not `Turkish`) — that list is what the dropdown
   renders.

No other code changes.

**If the language is RTL**, no reshaping call is needed — Qt shapes and bidi-orders
logical text with HarfBuzz. Do check it renders in the dropdowns and that any
`QTextOption` involved sets `Qt.LayoutDirectionAuto`; `ar` is the existing precedent.

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

## C. Spoken (source) language

What the STT engine is told to listen for. **Every entry here is filtered per engine** —
the engines do not agree on what they accept, and offering a language an engine rejects is
a live failure (`Invalid value: 'so'` on a reconnect loop, DEVLOG s60), not a bad result.

### Adding one

1. Add `("Name", "xx")` to `SOURCE_LANGUAGES` in `utils/settings.py`, in the
   alphabetical block after Albanian. That list is the UNION of what the engines
   accept, not a shortlist — do not curate it, the per-engine filter does that.
   **Plain lowercase ISO 639-1 only** — the code goes to the APIs as-is, and every one of them rejects a
   regional variant like `pt-BR`.
2. Add the endonym to `LANGUAGE_ENDONYMS` in the same file, in the language's own script
   (`Soomaali`, not `Somali`). Miss this and the dropdown silently shows the English name
   among native ones.
3. **That is all.** It appears automatically on every engine whose set contains the code,
   and nowhere else. Nothing needs registering per engine.
4. `pytest tests/test_settings.py tests/test_providers.py -q` — the guards there tell you
   exactly which edit was left out.

If it appears on no engine, it will only be offered under Gemini (which validates nothing).
`test_the_gemini_only_languages_are_the_ones_we_think` pins that set (currently Albanian,
Hausa, Kurdish and Somali) and will fail, on purpose — either the language is genuinely
Gemini-only (update the pin and say why) or the code is wrong.

### Removing one

Delete both entries from step 1 and 2. The endonym guard catches a half-removal.

### An engine gained or lost a language upstream

Edit the one `SUPPORTED_LANGUAGE_CODES` frozenset in that provider's module — nothing else:

| Engine | Set lives in | Source of truth |
| --- | --- | --- |
| `openai_realtime` | `providers/openai/realtime.py` | **A per-code probe** — the rejection message under-reports, see below |
| `openai` (segmented) | `providers/openai/transcription.py` | **A 1-second probe per code** — NOT the Whisper list, which is far wider |
| `deepgram` | `providers/deepgram/__init__.py` | **A per-(model, code) handshake** — the docs omit Pashto on Nova-3 |
| `gemini`, `gemini_realtime` | `providers/__init__.py`, as `None` | Nothing to maintain: neither path sends a language field the API can reject |

**Every engine set is measured against its own live endpoint. None of them is copied
from a doc, and the OpenAI Realtime one is not read off the API's error message either.**

The Realtime API refuses a bogus code with `Invalid value: 'zz'. Supported values are:
...`, and that list looks authoritative. It is not. It names **58** codes; the API accepts
**64**. `bn`, `gu`, `ka`, `ml`, `te` and `yue` are all accepted and none of them appears in
it (measured 2026-09-03). Believing the message hid Bengali, Gujarati, Georgian, Malayalam
and Telugu from the app's default engine.

So probe one code at a time. The asymmetry that makes this cheap: **a refusal arrives in
well under a second, an accepted code never errors at all**, so an 8-second deadline
separates them cleanly. No audio is sent and no session completes, so it costs nothing.

```python
import sys, threading
sys.path.insert(0, ".")
from concurrent.futures import ThreadPoolExecutor
from providers import get_stored_api_key
from providers.openai.client import set_api_key
set_api_key(get_stored_api_key("openai"))   # a bare script must activate the key itself
from providers.openai.realtime import (
    OpenAIRealtimeTranscriptionProvider, SUPPORTED_LANGUAGE_CODES)

noop = lambda *a, **k: None

def accepts(lang):
    errors, got = [], threading.Event()
    handle = OpenAIRealtimeTranscriptionProvider().open_stream(
        model="gpt-4o-transcribe", language=lang,
        on_transcript=noop, on_utterance_end=noop,
        on_error=lambda e: (errors.append(str(e)), got.set()))
    fired = got.wait(timeout=8)          # refusals land in <1s; see below
    handle.close()
    return not (fired and "Invalid value" in errors[0])

candidates = sorted(set(SUPPORTED_LANGUAGE_CODES) | {"zz", "bn", "ml"})  # widen as needed
with ThreadPoolExecutor(max_workers=4) as ex:
    live = {c for c, ok in zip(candidates, ex.map(accepts, candidates)) if ok}

assert "zz" not in live, "control code was accepted - the run is invalid, do not trust it"
print(len(live), "codes ·", "MATCH" if live == set(SUPPORTED_LANGUAGE_CODES) else "DRIFT")
print("gained:", sorted(live - set(SUPPORTED_LANGUAGE_CODES)))
print("lost:  ", sorted(set(SUPPORTED_LANGUAGE_CODES) - live))
```

**Always probe `zz` in the same run.** If the control is not refused, something else is
wrong (key, network, an SDK change) and every "accepted" result is meaningless.

**The rejection arrives asynchronously and that trips people up.** `session.created` comes
back first and `open_stream()` returns perfectly happily; the server refuses the
`session.update` a moment later, through `on_error`. Closing the handle immediately — or
passing a no-op `on_error` — makes a rejected language look accepted. It is also why the
user-visible symptom is `STREAMING Reconnected … new connection opened` immediately
followed by the error, over and over.

The other engines answer faster and more plainly:

- **OpenAI segmented** — POST a 1-second tone per code; an unsupported one comes back
  `400 invalid_value` naming `param: language` before any audio is read.
- **Deepgram** — open the WebSocket per (model, code); an unsupported pair is refused at
  the handshake with `HTTP 400`. Probe **per model**, they disagree.
- **Gemini** — nothing to probe. Neither path sends a field the API can reject.

---

## Verify

```bash
python -m pytest tests/test_dictionary.py tests/test_settings.py tests/test_providers.py -q
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

---
name: refresh-models
description: Check the AI providers for new or retired models and decide what MinbarLive should offer, add or remove. Use every couple of months, when a provider announces new models, when an offered model starts failing, or when asked "are there new models", "should we add X", "is model Y still worth offering". Listing is free; probing costs a few cents of real API money.
---

# Refreshing the model lists

**A listed model is not a usable one.** In July 2026 `gemini-2.5-flash` was still listed
while returning 404 to new accounts. In the October 2026 refresh, about half the
candidates failed through the app's own code path while passing the listing:
- a thinking setting they refuse;
- an empty transcript;
- a turn-detection mode they don't support;
- a doubled honorific.

So it is always **list → probe → decide**, never list → add.

## 1. List (free)

```bash
python bench/list_models.py
```

It compares every provider's model listing with what the app offers (dropdowns, defaults,
fallback chains, embedding models) and prints, per provider:

| Group | Meaning | Action |
| --- | --- | --- |
| Offered but NOT listed | The provider retired a model the app still sends | Probe it; remove it if it fails (step 4) |
| Listed, not offered | A candidate | Probe it (step 2) |
| Already probed and rejected | Turned down before, with reason and date | Skip, unless the reason may have changed (price cut, new snapshot) |

Image, video, voice, coding, legacy and domain-tuned models are hidden by the `_IRRELEVANT`
filter; `--all` prints everything. Keys come from the app's keychain or a `.env`.

## 2. Probe (cents)

```bash
python bench/probe_models.py --translation openai:gpt-6-sol gemini:gemini-3.7-flash
python bench/probe_models.py --stt openai:gpt-transcribe openai_realtime:gpt-transcribe
```

The prefix is the app's provider id. For `--stt` it is the engine:
- `openai` and `gemini` are segmented.
- `openai_realtime`, `gemini_realtime` and `deepgram` stream.

Everything goes through the app's own providers and prompts.

What to look for:

| Kind | Worth offering when |
| --- | --- |
| Translation | "no faults flagged" (honorifics, German kept as-is, no Arabic left in the output), and it beats something already offered on at least one of speed, price or German quality. Live subtitles want ~2 s or less; the default `gpt-5.2` measured 1.9 s |
| Segmented STT | Not FAILED, not EMPTY, CER near 0 on the clip, and well under the segment cadence (a 12 s segment arrives every 9 s, so anything over ~3 s is a poor trade) |
| Streaming STT | No ERRORS, the last final within ~1–2 s of the speech ending, and turn ends roughly matching finals (no turn ends = no subtitle flushes) |

**The probe speech is TTS, and too clean to rank models on real mosque audio.** It tells
you whether a model works and how fast. Offer a new model as an **option**. Change a
default only after someone has run it through a real session.

## 3. Add

| List | File |
| --- | --- |
| OpenAI translation | `utils/settings.py` `TRANSLATION_MODELS` |
| OpenAI segmented STT | `utils/settings.py` `TRANSCRIPTION_MODELS` |
| OpenAI real-time STT | `providers/openai/realtime.py` `TRANSCRIPTION_MODELS` |
| Gemini translation / STT | `providers/gemini/__init__.py` |
| Gemini Live | `providers/gemini/realtime.py` `TRANSCRIPTION_MODELS` |
| Claude | `providers/anthropic/__init__.py` |
| Deepgram | `providers/deepgram/__init__.py` |

For every model you add:

- **A comment above the entry** with the probe date and the numbers that justified it.
  The next refresh compares against them.
- **A price** in `MODEL_RATES` (`utils/cost_tracking.py`).
  - An unpriced model works, but it silently drops out of the session cost.
  - `tests/test_cost_tracking.py` fails for an unpriced OpenAI dropdown model.
  - Models that report usage as seconds of audio (`type="duration"`, like `whisper-1` and
    `gpt-transcribe`) take `duration_minute`, not token rates.
- **STT only: measure the languages it accepts** on each endpoint it is offered on. The
  add-language skill describes how ("An engine gained or lost a language upstream").
  - OpenAI's sets are per *engine*, so the set must hold for every model offered under it.
  - Deepgram's are per *model*.
  - Never assume a new model accepts what its sibling does.
- **Tests that pin a list.** `tests/test_providers.py` pins the OpenAI real-time model ids.
- **`docs/providers.md`**, where it names the models.

## 4. Remove

Remove a model when:

- **it fails.** Offered but no longer listed, or it fails its probe. Take it out of the
  fallback chain too: otherwise every failure first waits on a dead model.
- **it is strictly dominated.** Another offered model of the same provider is faster *and*
  cheaper *and* no worse on the translation checks. A short list an operator can read
  beats a complete one.

Removing is safe for existing users. A stored model id that is no longer known falls back
to that provider's default, so nobody is left with a broken setting. Both paths live in
`providers/__init__.py`: `_KNOWN_MODELS` in `_model_chain` for translation and segmented
STT, and `resolve_streaming_transcription_model` for streaming.

## 5. Record

- **Every rejected candidate** goes into `ALREADY_REJECTED` in `bench/list_models.py`, with
  its reason and the date, so the next refresh does not probe it again.
- **The numbers** go in the DEVLOG entry. A model list without its measurements gets
  re-litigated by the next session.

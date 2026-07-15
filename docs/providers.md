# AI Providers

MinbarLive talks to AI services through a provider abstraction layer (`providers/` package). All pipeline code goes through provider factories — nothing outside `providers/` imports an AI SDK directly, and the Gemini/Anthropic/Deepgram SDKs are imported lazily (you only pay for what you use; the app runs fine without the optional packages installed).

Two choices are independent of each other:

- **Translation provider** (`AI Provider` in the settings): which LLM translates the text — Google Gemini, OpenAI, or Anthropic Claude.
- **Transcription engine** (part of the Processing Strategy / Transcription section): which speech-to-text engine runs — and whether the pipeline is real-time streaming or segmented.

## Capability Matrix

| Provider          | Translation | Transcription (segmented) | Transcription (real-time) | Embeddings (RAG) |
| ----------------- | :---------: | :-----------------------: | :-----------------------: | :--------------: |
| Google Gemini     | ✅ (default) | ✅ (default)             | ✅ (default, Live API)    | ✅ (default, shipped) |
| OpenAI            | ✅          | ✅                        | ✅ (Realtime API)         | ✅ (shipped)     |
| Anthropic Claude  | ✅          | ❌ (falls back)           | ❌                        | ❌               |
| Deepgram          | ❌          | ❌                        | ✅                        | ❌               |

If a selected provider lacks a capability, the app falls back to the highest-ranked provider (Gemini → OpenAI → Anthropic) that has a usable key, and logs a warning once (e.g. Claude users transcribe via Gemini or OpenAI — a Claude setup needs one of those keys too).

## Translation Providers

| Provider | Default model | Fallback chain | Notes |
| -------- | ------------- | -------------- | ----- |
| **Google Gemini** (default) | `gemini-3.1-flash-lite` | `gemini-3.1-flash-lite` → `gemini-3.5-flash` | SDK (`google-genai`) imported lazily; the 2.x flash models were retired for new accounts (July 2026) |
| **OpenAI** | `gpt-5.2` | `gpt-5.2` → `gpt-5.1` → `gpt-4.1` → `gpt-4o-mini` | Full model list selectable in the GUI (GPT-5.5 … GPT-4o Mini) |
| **Anthropic Claude** | `claude-sonnet-5` | `claude-sonnet-5` → `claude-haiku-4-5` | `claude-opus-4-8` and `claude-fable-5` offered in the dropdown but kept out of the fallback chain (cost opt-in) |

If the primary model fails, the fallback chain is tried in order. Your model selection is only used when it belongs to the active provider — switching providers never sends e.g. a GPT model id to Gemini; the provider default takes over instead.

## Transcription Engines

### Segmented pipeline (fixed audio segments)

| Engine | Default model | Notes |
| ------ | ------------- | ----- |
| **Google Gemini** (default) | `gemini-3.5-flash` | Audio sent inline with a verbatim-transcription instruction (Gemini has no dedicated STT endpoint) |
| **OpenAI** | `gpt-4o-transcribe` | Fallbacks: `gpt-4o-mini-transcribe`, `whisper-1` |

### Real-time streaming (Processing Strategy: "Real-time streaming")

| Engine | Models | API key | Notes |
| ------ | ------ | ------- | ----- |
| **Google Gemini Live** (default) | `gemini-2.5-flash-native-audio-latest`, `…-preview-12-2025` | Reuses your Gemini key | Only live-verified models are offered; the Live API accepts no language hint, the model auto-detects internally |
| **OpenAI Realtime** | `gpt-4o-transcribe`, `gpt-4o-mini-transcribe` | Reuses your OpenAI key | Captures at 24 kHz (Realtime API requirement) |
| **Deepgram** | `nova-3` (default), `nova-2` | Own Deepgram key | |

Streaming replaces only the transcription side — translation still runs through your configured translation provider, and Quran/Athan matching works as usual (for Arabic sources). Real-time mode requires an explicit source language — "Automatic" only works in segmented mode.

## API Keys

- Keys are stored in the **OS keychain** (service `MinbarLive`, one entry per provider: `<provider>_api_key`). They are never written to `settings.json` or any other file.
- Environment variables work as a fallback and are read at startup (a `.env` file in the app directory is loaded too): `OPENAI_API_KEY`, `GEMINI_API_KEY` / `GOOGLE_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`.
- The real-time engines don't have keys of their own: OpenAI Realtime uses the OpenAI key, Gemini Live uses the Gemini key.
- The first-run wizard and the settings window manage keys per provider; switching to a provider without a stored key prompts for one.

**Simplest setup:** one Google Gemini key covers translation, real-time transcription, and Quran verse matching.

## Embeddings (Quran Verse Matching)

Query embeddings must live in the same vector space as the precomputed verse matrix, so the embedding provider does **not** simply follow the `AI Provider` setting:

- Gemini space (the default setup): used when the provider is Gemini **and** `data/embeddings/quran_embeddings_gemini.npz` exists (shipped; rebuild with `notebooks/build_embeddings_npz.py`, `PROVIDER="gemini"`, model `gemini-embedding-001`). Verse matching then needs no OpenAI key at all.
- OpenAI space: `text-embedding-3-large` against `data/embeddings/quran_embeddings_openai.npz` (shipped) — used for every non-Gemini provider.
- Anthropic has no embeddings — Claude setups use the OpenAI space.

See [data-files.md](data-files.md) for the embedding files and how to regenerate them.

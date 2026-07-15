# Configuration

## User Settings (GUI)

These settings are configurable from the control panel / settings window and saved between sessions in `settings.json` (see [Runtime Files](project-structure.md#runtime-files)). API keys are **not** stored there — they go to the OS keychain.

| Setting                | Default              | Description                                                            |
| ---------------------- | -------------------- | ---------------------------------------------------------------------- |
| GUI Language           | Deutsch              | Interface language (DE, EN, AR, BS, SQ, TR)                             |
| Appearance             | Light                | Light/dark theme — control panel and subtitle window are set separately |
| Source Language        | Automatic            | Spoken language (Arabic, Turkish, Urdu, …); real-time streaming requires an explicit language (no "Automatic") |
| Target Language        | German               | Translation output (35+ languages)                                      |
| Processing Strategy    | Real-time streaming  | Real-time streaming, Chunk-based, or Semantic buffering (Beta)          |
| Transcription Engine   | OpenAI (real-time)   | STT engine + model; the real-time engines imply streaming mode          |
| AI Provider            | OpenAI               | Translation provider (OpenAI, Google Gemini, Anthropic Claude)          |
| Translation Model      | GPT-5.2              | Per-provider model list; "use default" recommended                      |
| Subtitle Mode          | Realtime             | Realtime feed, Continuous (ticker), or Static (latest only) — Realtime is only available while streaming |
| Show original text     | Off                  | Bilingual display: source text above the translation                    |
| Islamic mode           | On                   | Quran verse & Athan recognition + Islamic translation style; off = general translator (turning it off asks for confirmation) |
| Font Size              | 40                   | Subtitle font size                                                      |
| Scroll Speed           | 1.0                  | Speed for continuous mode (0.5x - 5x)                                   |
| Adaptive catch-up      | On                   | Speeds up continuous scrolling when a backlog builds                    |
| Subtitle window height | 50 %                 | Height of the subtitle window as % of the screen                        |
| Transparent            | Off                  | Transparent overlay for static mode                                     |
| Input Device           | System default       | Audio input source selection                                            |
| Subtitle Screen        | Monitor 1            | Monitor for subtitle display                                            |
| Show footer            | On                   | AI-disclaimer pill on the subtitle window                               |
| Hide subtitles on stop | Off                  | Hide the subtitle window while translation is stopped                   |
| Auto start             | Off                  | Start translating as soon as the app launches                           |
| Auto cleanup           | On                   | Purge old log/history files at startup (see retention below)            |

A **first-run wizard** (interface language & appearance → languages → microphone → provider & API key → disclaimer) sets the essentials on the first launch.

### GUI Languages

The control panel interface is available in 6 languages:

| Code | Language |
| ---- | -------- |
| de   | Deutsch  |
| en   | English  |
| ar   | العربية  |
| bs   | Bosanski |
| sq   | Shqip    |
| tr   | Türkçe   |

Select your preferred language from the dropdown in the top-right corner. Changes apply immediately without restart.

### Processing Strategy Options

- **Real-time streaming** (default): live word-by-word transcript, translation per utterance (~1–3 s latency). Engine selectable: Google Gemini Live (default), OpenAI Realtime, Deepgram.
- **Chunk-based**: translates each 12 s audio segment immediately (~4–14 s latency). No streaming connection needed.
- **Semantic buffering** (Beta): waits for complete sentences before translating. Best sentence quality, highest latency; heuristics tuned for Arabic.

## AI Models

Model selection is **user-configurable in the GUI** and lives in `utils/settings.py` (OpenAI lists) and `providers/<provider>/` (Gemini, Anthropic, streaming engines) — not in `config.py`. Each provider has a default plus a fallback chain that is tried automatically when a model fails:

| Capability              | Default                  | Fallback chain                                    |
| ----------------------- | ------------------------ | ------------------------------------------------- |
| Translation (Gemini)    | `gemini-3.1-flash-lite`  | `gemini-3.1-flash-lite` → `gemini-3.5-flash`      |
| Translation (OpenAI)    | `gpt-5.2`                | `gpt-5.2` → `gpt-5.1` → `gpt-4.1` → `gpt-4o-mini` |
| Transcription (OpenAI)  | `gpt-4o-transcribe`      | `gpt-4o-transcribe` → `gpt-4o-mini-transcribe` → `whisper-1` |
| Embeddings (RAG)        | `gemini-embedding-001` / `text-embedding-3-large` | — (must match the precomputed verse matrix of the active space) |

See [providers.md](providers.md) for the Gemini/Anthropic/streaming model catalogs.

> **Note:** If you change the embedding model, you must regenerate the verse embedding matrix — see [data-files.md](data-files.md).

## Technical Constants (config.py)

`config.py` holds the immutable technical constants. The most relevant:

### Audio & segmentation

| Parameter              | Default | Description                                    |
| ---------------------- | ------- | ---------------------------------------------- |
| `DURATION`             | 12 s    | Length of each audio segment (segmented mode)  |
| `OVERLAP`              | 3 s     | Overlap between segments                       |
| `FS`                   | 16000   | Sample rate (OpenAI Realtime captures at 24 kHz) |
| `SAME_LANG_DURATION`   | 5 s     | Shorter segments when source = target language |
| `SILENCE_THRESHOLD`    | 0.001   | Amplitude below which a frame counts as silent |
| `SILENCE_RATIO`        | 0.8     | Fraction of silent frames to skip a segment    |

### Buffering & streaming

| Parameter                         | Default | Description                                         |
| --------------------------------- | ------- | --------------------------------------------------- |
| `SEMANTIC_MAX_CHUNKS`             | 3       | Max segments to buffer before forcing flush         |
| `SEMANTIC_MAX_SECONDS`            | 10      | Max seconds to buffer before forcing flush          |
| `STREAMING_CHUNK_MS`              | 50      | PCM chunk size fed to the streaming connection      |
| `STREAMING_UTTERANCE_END_MS`      | 1000    | Deepgram silence threshold for utterance end        |
| `STREAMING_GEMINI_SILENCE_MS`     | 800     | Gemini Live silence threshold for turn end          |
| `STREAMING_MAX_UTTERANCE_SECONDS` | 12      | Forced flush cap for unbroken speech                |

### Quran / Athan matching

| Parameter                        | Default | Description                                          |
| -------------------------------- | ------- | ---------------------------------------------------- |
| `RAG_MIN_SIMILARITY`             | 0.60    | Minimum cosine similarity for a verse hint           |
| `RAG_TOP_K`                      | 5       | Max number of verse candidates per segment           |
| `RAG_HARD_MATCH_THRESHOLD`       | 0.85    | Similarity at which the verified-verse bypass fires  |
| `RAG_HARD_MATCH_MIN/MAX_LENGTH_RATIO` | 0.75 / 1.25 | Segment/verse word-count band for the bypass   |
| `RAG_HARD_MATCH_MAX_WORD_DIFF`   | 6       | Absolute word-count difference cap for the bypass    |
| `QURAN_VERIFIED_MARKER`          | 📖      | Prefix shown on verified verse subtitles             |
| `ATHAN_MATCH_THRESHOLD`          | 0.75    | Minimum fuzzy match score for Athan detection        |

### Batch mode segmentation

| Parameter                       | Default | Description                                            |
| ------------------------------- | ------- | ------------------------------------------------------ |
| `BATCH_MAX_SEGMENT_SECONDS`     | 15.0    | Cap for one transcription chunk of unbroken speech     |
| `BATCH_MIN_SILENCE_GAP_SECONDS` | 0.4     | Micro-pauses shorter than this stay inside a block     |
| `BATCH_MAX_SILENCE_KEEP_SECONDS`| 2.0     | Pause length absorbed into surrounding segments        |
| `BATCH_MIN_STANDALONE_SECONDS`  | 2.0     | Speech blocks shorter than this are merged into a neighbor |

### Context & retention

| Parameter                   | Default | Description                               |
| --------------------------- | ------- | ----------------------------------------- |
| `CONTEXT_RECENT_RAW_COUNT`  | 3       | Raw transcription segments kept           |
| `CONTEXT_SUMMARIZE_EVERY_N` | 10      | Segments between rolling summary updates  |
| `CONTEXT_HOURLY_INTERVAL`   | 3600    | Seconds between hourly summary snapshots  |
| `LOGS_RETENTION_DAYS`       | 30      | Auto-cleanup age for log files            |
| `HISTORY_RETENTION_DAYS`    | 90      | Auto-cleanup age for history/summaries    |
| `BATCH_RETENTION_DAYS`      | 90      | Auto-cleanup age for batch transcripts    |

## Retry Configuration (utils/retry.py)

API calls automatically retry on transient failures (rate limits, timeouts, connection errors):

| Parameter     | Default | Description                             |
| ------------- | ------- | --------------------------------------- |
| `max_retries` | 3       | Maximum retry attempts                  |
| `base_delay`  | 1.0s    | Initial delay between retries           |
| `max_delay`   | 30.0s   | Maximum delay (caps exponential growth) |

Retries use exponential backoff with jitter to prevent thundering herd problems. On top of the retries, model fallback chains switch to an alternative model when a model keeps failing.

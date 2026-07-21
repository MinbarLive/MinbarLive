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
| Transcription Engine   | Google Gemini (real-time) | STT engine + model; the real-time engines imply streaming mode     |
| AI Provider            | Google Gemini        | Translation provider (Google Gemini, OpenAI, Anthropic Claude)          |
| Translation Model      | `gemini-3.1-flash-lite` | Per-provider model list; "use default" recommended                    |
| Subtitle Mode          | Realtime             | Realtime feed, Continuous (ticker), or Static (latest only) — Realtime is only available while streaming |
| Show original text     | On                   | Bilingual display: source text above the translation                    |
| Show live transcript   | On                   | Realtime mode: show the in-progress transcript line while the speaker talks (independent of "Show original text") |
| Islamic mode           | On                   | Quran verse & Athan recognition + Islamic translation style; off = general translator (turning it off asks for confirmation) |
| Noise filter           | On                   | Voice-activity gate: drops static/hum the loudness-based silence gate lets through |
| Font Size              | 40                   | Subtitle font size                                                      |
| Original text size     | 70 %                 | Size of the original-text line, relative to the translation (Subtitle appearance) |
| Translation colour     | Theme default        | Override for the translation text colour (Subtitle appearance)          |
| Original text colour   | Theme default        | Override for the original-text colour (Subtitle appearance)             |
| Scroll Speed           | 1.0                  | Speed for continuous mode (0.5x - 5x)                                   |
| Adaptive catch-up      | On                   | Speeds up continuous scrolling when a backlog builds                    |
| Subtitle window height | 50 %                 | Height of the subtitle window as % of the screen                        |
| Transparent            | Off                  | Transparent overlay for static mode                                     |
| Input Device           | System default       | Microphone, or a Windows `(Loopback)` output device (system audio)      |
| Subtitle Screen        | Monitor 1            | Monitor for subtitle display                                            |
| Show subtitles while running | On             | Off = transcribe and translate to history only, no subtitle window       |
| Show footer            | On                   | AI-disclaimer pill on the subtitle window                               |
| Hide subtitles on stop | Off                  | Hide the subtitle window while translation is stopped                   |
| Keep windows always on top | On               | Control panel + subtitle window float above other windows               |
| Hide announcement when stopped | On           | Clears an "until stopped" announcement when the session is stopped      |
| Auto start             | Off                  | Start translating as soon as the app launches                           |
| Auto stop when idle    | On                   | Stop a running session after 10 min without any transcription (cost guard) |
| Auto cleanup (logs)    | On                   | Purge old log files at startup (see retention below)                    |
| Auto cleanup (content) | Off                  | Purge old history + batch files at startup — your own content, so opt-in |
| Check for updates      | On                   | One anonymous GitHub releases request at startup                        |

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

| Capability                    | Default                  | Fallback chain                                    |
| ----------------------------- | ------------------------ | ------------------------------------------------- |
| Translation (Gemini, default) | `gemini-3.1-flash-lite`  | `gemini-3.1-flash-lite` → `gemini-3.5-flash`      |
| Translation (OpenAI)          | `gpt-5.2`                | `gpt-5.2` → `gpt-5.1` → `gpt-4.1` → `gpt-4o-mini` |
| Translation (Anthropic)       | `claude-sonnet-5`        | `claude-sonnet-5` → `claude-haiku-4-5`            |
| Transcription (Gemini, default) | `gemini-3.5-flash`     | — (segmented; audio sent inline)                  |
| Transcription (OpenAI)        | `gpt-4o-transcribe`      | `gpt-4o-transcribe` → `gpt-4o-mini-transcribe` → `whisper-1` |
| Real-time STT (Gemini, default) | `gemini-2.5-flash-native-audio-latest` | — (whitelisted Live models only)|
| Embeddings (RAG)              | `gemini-embedding-001` / `text-embedding-3-large` | — (must match the precomputed verse matrix of the active space) |

See [providers.md](providers.md) for the Gemini/Anthropic/streaming model catalogs.

> **Note:** If you change the embedding model, you must regenerate the verse embedding matrix — see [data-files.md](data-files.md).

## Technical Constants (config.py)

`config.py` holds the immutable technical constants. The most relevant:

### Audio & segmentation

| Parameter                   | Default | Description                                    |
| --------------------------- | ------- | ---------------------------------------------- |
| `DURATION`                  | 12 s    | Length of each audio segment (segmented mode)  |
| `OVERLAP`                   | 3 s     | Overlap between segments                       |
| `FS`                        | 16000   | Sample rate (OpenAI Realtime captures at 24 kHz) |
| `SAME_LANG_DURATION`        | 5 s     | Shorter segments when source = target language |
| `SILENCE_THRESHOLD`         | 0.001   | Amplitude below which a frame counts as silent |
| `SILENCE_RATIO`             | 0.8     | Fraction of silent frames to skip a segment    |
| `SEGMENT_OVERLAP_MAX_WORDS` | 10      | Cap on words stripped when de-duplicating the overlap repeat between consecutive live segments |
| `MIN_TRANSLATABLE_LETTERS`  | 3       | Fragment gate: fewer alphabetic characters than this never reaches a translation call |

### Noise filter (voice-activity gate)

The loudness-based silence gate above cannot tell speech from static or hum — `audio/vad.py` classifies frames by spectral shape instead. Toggled by the "Noise filter" setting.

| Parameter                    | Default | Description                                                     |
| ---------------------------- | ------- | ---------------------------------------------------------------- |
| `VAD_AGGRESSIVENESS`         | 2       | webrtcvad strictness (0–3)                                       |
| `VAD_MIN_SPEECH_RATIO`       | 0.05    | Segmented/batch: skip a segment below this fraction of speech frames |
| `VAD_STREAM_HANGOVER_SECONDS`| 2.0     | Streaming: sustained non-speech beyond this is fed as digital silence |
| `VAD_STREAM_WINDOW_SECONDS`  | 1.0     | Rolling window for the streaming open/close decision             |
| `VAD_STREAM_OPEN_RATIO`      | 0.1     | Speech-frame fraction that opens the streaming gate              |
| `VAD_DECISION_TARGET_PEAK`   | 0.03    | Quiet audio is boosted to ≈ -30.5 dBFS **for the decision only** — the audio passed on is never modified |
| `VAD_DECISION_MAX_BOOST`     | 16.0    | Cap on that boost (+24 dB)                                       |

### Buffering & streaming

| Parameter                          | Default | Description                                         |
| ---------------------------------- | ------- | --------------------------------------------------- |
| `SEMANTIC_MAX_CHUNKS`              | 3       | Max segments to buffer before forcing flush         |
| `SEMANTIC_MAX_SECONDS`             | 10      | Max seconds to buffer before forcing flush          |
| `SEMANTIC_MAX_WORDS`               | 28      | A longer flush is split at sentence ends so it can be read |
| `STREAMING_CHUNK_MS`               | 50      | PCM chunk size fed to the streaming connection      |
| `STREAMING_UTTERANCE_END_MS`       | 1000    | Deepgram silence threshold for utterance end        |
| `STREAMING_GEMINI_SILENCE_MS`      | 800     | Gemini Live silence threshold for turn end          |
| `STREAMING_MAX_UTTERANCE_SECONDS`  | 12      | Forced flush cap for unbroken speech                |
| `STREAMING_COALESCE_MIN_WORDS`     | 6       | Short utterances are held and merged up to this length so the LLM sees a whole clause |
| `STREAMING_COALESCE_HOLD_SECONDS`  | 2       | How long a held short utterance waits for a follow-up |
| `STREAMING_RECONNECT_BASE_SECONDS` | 1.0     | First backoff delay after a dropped connection      |
| `STREAMING_RECONNECT_MAX_SECONDS`  | 30.0    | Backoff cap; retries continue until Stop            |

### Display scaling (gui/scaling.py)

Window sizes are defined in DPI-logical units, so Windows multiplies them by the display scaling. On a small high-DPI screen (a 1920×1080 laptop at the recommended 150 %) that made every window fill 80–100 % of the usable height, and at 175 % the wizard and settings windows were clipped. A single clamp scales all windows down so the largest one fits.

| Parameter             | Default | Description                                                              |
| --------------------- | ------- | ------------------------------------------------------------------------ |
| `DESIGN_W`/`DESIGN_H` | 900/672 | Largest window the app can open (history viewer width, wizard height)     |
| `MAX_SCREEN_FRACTION` | 0.85    | A window may use at most this much of the usable screen area              |

The factor is never above 1.0 — it only shrinks when the design would not fit, so a large monitor at 150 % keeps the bigger text the user asked for and is left exactly as-is.

### Control-panel window & card grid (gui/app_gui.py)

The control panel opens at a size that shows every card at once and can then be
dragged as large or as small as you like — the cards reflow to fit. The last
size and position are remembered in `window_geometry`.

| Parameter                 | Default   | Description                                                    |
| ------------------------- | --------- | -------------------------------------------------------------- |
| `_DEFAULT_W`/`_DEFAULT_H` | 880/630   | Size the window opens at on a fresh install (logical units)     |
| `_MIN_W`/`_MIN_H`         | 380/300   | Floor the window may be dragged down to                         |
| `_COL2_MIN_W`             | 720       | Card-grid width from which two columns are used                 |
| `_COL3_MIN_W`             | 1320      | …and three columns (a maximized window shows everything at once) |
| `_MAX_CARD_AREA_W`        | 1200      | Beyond this the 1/2-column grid is centered instead of stretched |
| `_MAX_CARD_AREA_W_WIDE`   | 1800      | Same cap for the 3-column grid                                  |

### Announcements (config.py)

| Parameter                       | Default            | Description                                            |
| ------------------------------- | ------------------ | ------------------------------------------------------ |
| `ANNOUNCEMENT_DURATIONS_SECONDS`| 10/30/60/300/0     | Preset display durations; `0` = until the operator stops it |
| `ANNOUNCEMENT_HISTORY_MAX`      | 3                  | Recent announcement texts kept for re-use              |
| `ANNOUNCEMENT_FAVORITES_MAX`    | 5                  | Pinned announcements kept                              |

### Realtime subtitle feed

| Parameter                  | Default | Description                                                  |
| -------------------------- | ------- | ------------------------------------------------------------ |
| `REALTIME_MAX_BLOCK_CHARS` | 220     | Longer settled translations are split at sentence boundaries (display only) |
| `REALTIME_LIVE_MAX_ROWS`   | 1       | Wrapped rows rendered for the in-progress live line           |
| `REALTIME_BLOCK_SPACING`   | 34      | Vertical gap between feed blocks                              |

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

| Parameter                       | Default | Description                                          |
| ------------------------------- | ------- | ---------------------------------------------------- |
| `CONTEXT_RECENT_RAW_COUNT`      | 3       | Raw transcription segments kept                      |
| `CONTEXT_SUMMARIZE_EVERY_N`     | 10      | Pending segments needed for a rolling summary        |
| `CONTEXT_SUMMARIZE_MIN_SECONDS` | 180     | …and this much time must also have passed. Both must hold: streaming utterances arrive every few seconds, and the count alone re-summarized near-identical text every ~45 s |
| `CONTEXT_HOURLY_INTERVAL`       | 3600    | Seconds between hourly summary snapshots             |
| `AUTO_STOP_INACTIVITY_SECONDS`  | 600     | Idle time before a running session auto-stops (when enabled) |
| `LOGS_RETENTION_DAYS`           | 30      | Auto-cleanup age for log files                       |
| `HISTORY_RETENTION_DAYS`        | 90      | Auto-cleanup age for history/summaries               |
| `BATCH_RETENTION_DAYS`          | 90      | Auto-cleanup age for batch transcripts               |

## Retry Configuration (utils/retry.py)

API calls automatically retry on transient failures (rate limits, timeouts, connection errors):

| Parameter     | Default | Description                             |
| ------------- | ------- | --------------------------------------- |
| `max_retries` | 3       | Maximum retry attempts                  |
| `base_delay`  | 1.0s    | Initial delay between retries           |
| `max_delay`   | 30.0s   | Maximum delay (caps exponential growth) |

Retries use exponential backoff with jitter to prevent thundering herd problems. On top of the retries, model fallback chains switch to an alternative model when a model keeps failing.

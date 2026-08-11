# Configuration

## User Settings (GUI)

These settings are configurable from the control panel / settings window and saved between sessions in `settings.json` (see [Runtime Files](project-structure.md#runtime-files)). API keys are **not** stored there; they go to the OS keychain.

| Setting                | Default              | Description                                                            |
| ---------------------- | -------------------- | ---------------------------------------------------------------------- |
| GUI Language           | Deutsch              | Interface language (DE, EN, AR, BS, SQ, TR)                             |
| Appearance             | Light                | Light/dark theme; control panel and subtitle window are set separately |
| Source Language        | Automatic            | Spoken language (Arabic, Turkish, Urdu, …); real-time streaming requires an explicit language (no "Automatic") |
| Target Language        | German               | Translation output (35+ languages)                                      |
| Processing Strategy    | Real-time streaming  | Real-time streaming, Chunk-based, or Semantic buffering (Beta)          |
| Transcription Engine   | OpenAI (real-time)   | STT engine + model; the real-time engines imply streaming mode          |
| AI Provider            | OpenAI               | Translation provider (OpenAI, Google Gemini, Anthropic Claude)          |
| Translation Model      | `gpt-5.2`               | Per-provider model list; "use default" recommended                    |
| Subtitle Mode          | Realtime             | Realtime feed, Continuous (ticker), or Static (latest only); Realtime is only available while streaming |
| Show original text     | On                   | Bilingual display: source text above the translation                    |
| Show live transcript   | Off                  | Realtime mode: show the in-progress transcript line while the speaker talks (independent of "Show original text") |
| Islamic mode           | On                   | Quran verse & Athan recognition + Islamic translation style; off = general translator (turning it off asks for confirmation) |
| Noise filter           | On                   | Voice-activity gate: drops static/hum the loudness-based silence gate lets through |
| Font Size              | 40                   | Subtitle font size                                                      |
| Original text size     | 70 %                 | Size of the original-text line, relative to the translation (Subtitle appearance) |
| Translation colour     | Theme default        | Override for the translation text colour (Subtitle appearance)          |
| Original text colour   | Theme default        | Override for the original-text colour (Subtitle appearance)             |
| Scroll Speed           | 1.0                  | Speed for continuous mode (0.5x - 5x)                                   |
| Adaptive catch-up      | On                   | Speeds up continuous scrolling when a backlog builds                    |
| Subtitle window height | 50 %                 | Height of the subtitle window as % of the screen                        |
| Transparent            | Off                  | Transparent overlay for static mode (genuine per-pixel alpha; on X11 it needs a compositing window manager) |
| Input Device           | System default       | Microphone, or a Windows `(Loopback)` output device (system audio)      |
| Subtitle Screen        | Monitor 1            | Monitor for subtitle display                                            |
| Hide subtitle window   | Never                | 3-way: **Never** (always shown) / **When stopped** / **Always** (no overlay at all — transcription and translation still run to history) |
| Show footer            | On                   | AI-disclaimer pill on the subtitle window                               |
| Windows on top         | When running         | 3-way: **Never** / **When running** / **Always**. The control panel is only ever topmost while a subtitle overlay is open |
| Window style           | Integrated           | **Integrated** = secondary windows open inside the control panel over a dim overlay; **Windows** = separate OS windows (see below) |
| Hide announcement when stopped | On           | Clears an "until stopped" announcement when the session is stopped      |
| Auto start             | Off                  | Start translating as soon as the app launches                           |
| Auto stop when idle    | On                   | Stop a running session after 10 min without any transcription (cost guard) |
| Auto cleanup (logs)    | On                   | Purge old log files at startup (see retention below)                    |
| Auto cleanup (content) | Off                  | Purge old history + batch files at startup; your own content, so opt-in |
| Check for updates      | On                   | One anonymous GitHub releases request at startup                        |
| Also offer beta and test versions | Off       | Widens that check to release candidates. The website's download buttons always serve the stable release, whatever this says |
| Skip this version      | —                    | On the update notice itself. Silences it for that release for good (the next release brings it back); the notice's ✕ only hides it until the next launch |
| Feedback               | —                    | Opens the anonymous feedback form in your browser. The same question appears once by itself, after three completed sessions — its ✕ asks again after another three, "Never show again" does not |
| Delete everything      | —                    | Removes the app-data folder **and** every provider's keychain entry, reports what went, then closes the app. For leaving, not for updating — to update, replace the program file and everything is kept |

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

- **Real-time streaming** (default): live word-by-word transcript, translation per utterance (~1–3 s latency). Engine selectable: OpenAI Realtime (default), Google Gemini Live, Deepgram.
- **Chunk-based**: translates each 12 s audio segment immediately (~4–14 s latency). No streaming connection needed.
- **Semantic buffering** (Beta): waits for complete sentences before translating. Best sentence quality, highest latency; heuristics tuned for Arabic.

### Window behaviour

Two of the settings above are 3-way selectors that replaced four older checkboxes; a `settings.json` written by an older version migrates automatically on load:

| Stored setting       | Values                          | Replaces                                          |
| -------------------- | ------------------------------- | ------------------------------------------------- |
| `subtitle_hide_mode` | `never` / `stopped` / `always`  | `subtitle_output_enabled` + `hide_subtitle_on_stop` |
| `always_on_top_mode` | `never` / `running` / `always`  | `always_on_top` (a boolean)                        |

**Window style** (`window_style`, default `integrated`) chooses between Discord-style panels that open inside the control panel over a dimmed backdrop and separate OS windows. Esc, the floating ✕ or a click on the dim closes the topmost panel; dialogs are never closed by a stray click.

Integrated mode works on **every platform**. It was Windows-only under the old Tk GUI, which built the panels as borderless top-level windows and needed per-window opacity from the window manager — something X11 without a compositor ignores, so the backdrop rendered solid black. The Qt host reparents the panels *into* the control panel as child widgets and paints the dim into the app's own back buffer, which asks nothing of the window manager (`gui/modal_host.py`).

The default changed to `integrated` on 2026-08-04, and only for new installs: `save_settings` writes `window_style` out, so an existing `settings.json` keeps whatever it already had.

The control panel also remembers whether it was **maximized** (`window_maximized`), which a `WxH+X+Y` geometry string cannot express. `window_geometry` keeps the last restored-down size, so un-maximizing after start-up lands somewhere sensible instead of filling the screen again.

## AI Models

Model selection is **user-configurable in the GUI** and lives in `utils/settings.py` (OpenAI lists) and `providers/<provider>/` (Gemini, Anthropic, streaming engines), not in `config.py`. Each provider has a default plus a fallback chain that is tried automatically when a model fails:

| Capability                    | Default                  | Fallback chain                                    |
| ----------------------------- | ------------------------ | ------------------------------------------------- |
| Translation (OpenAI, default) | `gpt-5.2`                | `gpt-5.2` → `gpt-5.1` → `gpt-4.1` → `gpt-4o-mini` |
| Translation (Gemini)          | `gemini-3.1-flash-lite`  | `gemini-3.1-flash-lite` → `gemini-3.5-flash`      |
| Translation (Anthropic)       | `claude-sonnet-5`        | `claude-sonnet-5` → `claude-haiku-4-5`            |
| Transcription (OpenAI)        | `gpt-4o-transcribe`      | `gpt-4o-transcribe` → `gpt-4o-mini-transcribe` → `whisper-1` |
| Transcription (Gemini)          | `gemini-3.5-flash`     | n/a (segmented; audio sent inline)                  |
| Real-time STT (OpenAI, default) | `gpt-4o-transcribe`    | n/a (Realtime API; captures at 24 kHz)              |
| Real-time STT (Gemini)          | `gemini-2.5-flash-native-audio-latest` | n/a (whitelisted Live models only)|
| Real-time STT (Deepgram)        | `nova-3`               | n/a (own Deepgram key)                              |
| Embeddings (RAG)              | `text-embedding-3-large` / `gemini-embedding-001` | n/a (must match the precomputed verse matrix of the active space) |

See [providers.md](providers.md) for the Gemini/Anthropic/streaming model catalogs.

> **Note:** If you change the embedding model, you must regenerate the verse embedding matrix; see [data-files.md](data-files.md).

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

The loudness-based silence gate above cannot tell speech from static or hum; `audio/vad.py` classifies frames by spectral shape instead. Toggled by the "Noise filter" setting.

| Parameter                    | Default | Description                                                     |
| ---------------------------- | ------- | ---------------------------------------------------------------- |
| `VAD_AGGRESSIVENESS`         | 2       | webrtcvad strictness (0–3)                                       |
| `VAD_MIN_SPEECH_RATIO`       | 0.05    | Segmented/batch: skip a segment below this fraction of speech frames |
| `VAD_STREAM_HANGOVER_SECONDS`| 2.0     | Streaming: sustained non-speech beyond this is fed as digital silence |
| `VAD_STREAM_WINDOW_SECONDS`  | 1.0     | Rolling window for the streaming open/close decision             |
| `VAD_STREAM_OPEN_RATIO`      | 0.1     | Speech-frame fraction that opens the streaming gate              |
| `VAD_DECISION_TARGET_PEAK`   | 0.03    | Quiet audio is boosted to ≈ -30.5 dBFS **for the decision only**; the audio passed on is never modified |
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

### Secondary window sizing (gui/window_size.py)

Settings, Batch and Announcement are the same shape — a hero, a scrolling column
of cards, and an action bar pinned below — so they open at the same width and
take the height their content asks for, up to a cap. Past the cap the cards
scroll rather than pushing the action bar off the bottom.

| Parameter            | Default | Description                                                     |
| -------------------- | ------- | ---------------------------------------------------------------- |
| `SECONDARY_WINDOW_W` | 520     | Shared width; wider and the single-column cards stretch out of shape |
| `SECONDARY_MAX_H`    | 760     | Height cap. Not lower: the batch window's collapsed cards already come to ~720 |
| `_MAX_SCREEN_SHARE`  | 0.92    | …and never more than this share of a short screen                |

There is no display-scaling clamp any more. The old Tk tree needed one because
window sizes were logical units that Windows multiplied by the display scaling,
which clipped the wizard at 175 %; Qt handles per-monitor DPI itself (the frozen
EXE declares per-monitor-aware-v2 in `MinbarLive.manifest`), and the windows that
used to be fixed designs are now content-sized by the rule above.

### Control-panel window & card grid (gui/control_panel.py)

The control panel opens at a size that shows every card at once and can then be
dragged as large or as small as you like; the cards reflow to fit. The last
size and position are remembered in `window_geometry`, and whether it was
maximized in `window_maximized`.

| Parameter                 | Default   | Description                                                    |
| ------------------------- | --------- | -------------------------------------------------------------- |
| `_DEFAULT_W`/`_DEFAULT_H` | 880/640   | Size the window opens at on a fresh install (logical units)     |
| minimum size              | 420×420   | Floor the window may be dragged down to (`setMinimumSize`)      |
| `_COL2_MIN_W`             | 800       | Card-grid width from which two columns are used — a *floor*, raised at run time to what the columns measure |
| `_COL3_MIN_W`             | 1030      | …and three columns (a maximized window shows everything at once) |
| `_SIDEBAR_W_WITH_LOG`     | 500       | Width the card sidebar keeps when the log panel is open          |
| `_LOG_PANEL_MIN_W`        | 340       | Minimum width of the log panel; the window widens only if both cannot fit |

The column thresholds are measured from what the columns actually need. The
horizontal scrollbar is off, so a threshold that lets a column drop below its
minimum does not scroll — it clips.

`_COL2_MIN_W` alone cannot state that need, because it comes from the font
engine: the same panel measures 658 px in Arabic, 758 in German and 869 on
Linux. `CardGrid.two_column_min_width()` therefore takes the larger of the
constant and the two columns in front of it, so the constant governs where it is
already big enough (all of Windows) and the measurement takes over where it is
not. `_COL3_MIN_W` stays a plain constant on purpose: three columns pin the
Advanced card open, which moves column C's minimum by ~11 px, and a threshold
that moves with the arrangement it produces oscillates.

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

# Architecture

## System Overview

```
                       ┌──────────────────────────────────────────────────────────┐
                       │                     App Controller                       │
                       │               (Thread Lifecycle Manager)                 │
                       └──────────────────────────────────────────────────────────┘
                                │                                      │
              ┌─────────────────┴─────────────────┐                    │
              ▼                                   ▼                    ▼
   ┌──────────────────────┐          ┌──────────────────────┐   ┌─────────────────┐
   │  SEGMENTED PIPELINE  │          │  STREAMING PIPELINE  │   │   Control GUI   │
   │                      │          │      (default)       │   │ (settings, log, │
   │ Audio Capture        │          │                      │   │  batch, history)│
   │  (ring buffer,       │          │ Audio Capture        │   └─────────────────┘
   │   silence detection) │          │  (PCM chunks)        │
   │        │             │          │        │             │
   │        ▼             │          │        ▼             │
   │ Transcription        │          │ Streaming STT engine │
   │  (whole segments)    │          │  (interim + final    │
   │        │             │          │   text, utterance-   │
   │        ▼             │          │   end detection)     │
   │ Buffering strategy   │          │        │             │
   │  (chunk / semantic)  │          │        │ live text ──┼──▶ subtitle window
   └────────┬─────────────┘          └────────┬─────────────┘    (word by word)
            │                                 │
            └───────────────┬─────────────────┘
                            ▼
              ┌───────────────────────────┐        ┌──────────────────┐
              │        Translation        │ ◀────▶ │ Context Manager  │
              │ Athan dictionary matching │        │ (async rolling / │
              │ Quran RAG (+ verified-    │        │ hourly summaries)│
              │  verse bypass)            │        └──────────────────┘
              │ LLM call via provider     │
              └────────────┬──────────────┘
                           ▼
                 ┌───────────────────┐
                 │   Subtitle GUI    │
                 │   (full screen)   │
                 └───────────────────┘
```

All AI calls (transcription, translation, embeddings) go through the **provider abstraction** in `providers/`; see [providers.md](providers.md). The pipeline never imports an AI SDK directly.

## Pipelines

The Processing Strategy setting selects one of two pipelines:

### Streaming pipeline (default: "Real-time streaming")

1. Audio (microphone or system loopback) is fed as small PCM chunks into a live connection to the streaming engine (Gemini Live, OpenAI Realtime, or Deepgram). With the noise filter on, sustained non-speech is replaced by **digital silence of the same length**, so the connection's timing and endpointing stay intact
2. The engine sends back **interim transcripts** (word by word, self-correcting) and marks **utterance ends** on natural pauses
3. The live transcript line is shown on the subtitle window while the speaker talks (Realtime subtitle mode)
4. Very short utterances are **coalesced**: a 1–3 word fragment from a rhetorical pause is held briefly and merged with the next one, so the LLM translates a whole clause instead of an isolated word (and one API call is saved per fragment)
5. Each finished utterance goes through the full translation step (Athan matching, Quran RAG, context, LLM)
6. The settled translation replaces the live line; typical speech → subtitle latency is ~1–3 s
7. An utterance that never pauses is force-flushed after 12 s so latency stays bounded
8. If the connection drops, a supervisor thread **reconnects with exponential backoff** (1 s → 30 s) until Stop; the audience sees one connection message per outage, and the first transcript after recovery clears it

### Segmented pipeline ("Chunk-based" / "Semantic buffering")

1. **Audio Capture** records into a ring buffer; 12 s segments with 3 s overlap are written as WAV (silent segments are skipped entirely, and with the noise filter on, loud-but-speechless segments are dropped before any API call)
2. **Transcription** converts each segment to text via the configured provider, walking a model fallback chain on errors
3. **Overlap dedup + fragment gate**: consecutive segments overlap by 3 s, so each transcription repeats the tail of the previous one; the repeated prefix is stripped (fuzzy-matched, since the model transcribes the overlap slightly differently each pass). A residual with fewer than 3 letters is dropped rather than translated
4. The **buffering strategy** groups transcriptions:
   - **Chunk-based** (segmented default): every segment is translated immediately (~4–14 s latency)
   - **Semantic buffering** (Beta): waits for sentence-ending punctuation before translating; flushes anyway after 3 segments or 10 s, and a stale buffer is flushed during silence. The sentence heuristics are Arabic-tuned.
5. The grouped text goes through the same translation step as above

## Translation Step

Every text to be translated passes through, in order:

1. **Same-language bypass**: if the source and target language are the same (or the source is "Automatic" and an Arabic-script transcription meets an Arabic target), the text is passed through without an LLM call
2. **Athan dictionary matching**: fuzzy matching against known Athan phrases (threshold 0.75) returns the curated translation directly
3. **Quran RAG matching**: the text is embedded and compared against 6,054 precomputed verse embeddings (one matrix-vector product, ~3 ms):
   - similarity ≥ **0.85** and the segment is essentially the verse alone (word-count guards) → **verified-verse bypass**: the exact published dictionary translation is displayed, marked 📖, and no LLM call is made
   - similarity ≥ 0.60 → the matched verses are passed to the LLM as translation hints
   - below 0.60 → no hint
4. **LLM translation** via the configured provider, with the session context and any hints. The system prompt handles **code-switching**: passages already in the target language are passed through unchanged, embedded Arabic quotations are still translated, and Islamic terms are preserved.

For non-Arabic source languages, the segmented and batch pipelines run a **second transcription pass in Arabic** on the same audio so Quran/Athan matching still works (skipped when the primary transcription is already Arabic script, in same-language runs, and in streaming mode).

### Islamic Mode

`Islamic mode` (settings, on by default) gates steps 2–3 and the Islamic translation prompt. Switched off, MinbarLive is a general-purpose live translator: no Athan/Quran matching, a neutral professional-translator prompt, and no Arabic re-transcription pass.

## Adaptive Context Management

For long sessions (1-4+ hours), the app uses intelligent context management:

- **Recent segments (last 3)**: Kept raw for immediate disambiguation
- **Rolling summary**: Updated when ≥10 segments are pending **and** ≥3 minutes have passed since the last summary (async, no delay). Both conditions must hold: streaming utterances arrive every few seconds, so the count alone burned an LLM call on near-identical text every ~45 s
- **Hourly summaries**: Long-term context compressed to ~20 words each

This keeps context under ~1500 tokens while maintaining session continuity.

### What the translation LLM receives for each segment

```
1. Source Text: Current transcription to translate
2. Context:
   - [Session overview: Hr1: ... | Hr2: ...]     ← Hourly summaries (if >1hr)
   - [Recent topics: ...]                         ← Rolling summary (~50 words)
   - [Last segments: seg1 / seg2 / seg3]          ← Last 3 raw transcriptions
3. Quran Hints: Matched verses from RAG (if any)
```

The context helps disambiguate unclear words without bloating the prompt.

## Batch Mode

`batch/processor.py` runs a pre-recorded file through the same transcription → RAG → translation pipeline linearly and writes an `.srt` file ([srt_writer.py](../batch/srt_writer.py)):

- Non-WAV input is converted to 16 kHz mono WAV via ffmpeg (auto-download offered on Windows)
- Segmentation is VAD-style: splits fall in natural pauses, unbroken speech is capped at 15 s (cut at the quietest sustained window), and sub-2 s snippets are merged into a neighbor to avoid ASR hallucinations
- Each segment's transcription is prompted with the previous segment's tail for cross-segment context
- Timestamps come from the segment positions; output lands next to the source as `{name}.{target_code}.srt`

## Audio Input

Both pipelines read from the same device selection (`gui/device_list.py`):

- **Microphones** via `sounddevice`, enumerated per host API (WASAPI preferred; WDM-KS is excluded because its device names are unusable).
- **Loopback devices** (Windows) via `soundcard`'s WASAPI loopback, capturing what an output device is *playing*, so system audio can be translated without a virtual audio cable. Loopback speakers get synthetic negative indices registered in `audio/loopback.py`, which lets the controller resolve them at stream-open time without importing GUI code. They are always recorded in stereo and mixed to mono (single-channel WASAPI recording returns garbage).

`soundcard` is an optional import: if it is missing, loopback entries simply don't appear.

The control panel shows a live **input level** (smoothed RMS in dBFS, `audio/level_meter.py`) under the device dropdown. While a session runs it is fed by the capture stream itself; while stopped, the **Test mic** button opens a meter-only capture that starts no writers, providers or translation. Changing the input device moves a running test to the new device. The level is measured on the raw capture, *before* the noise gate, so it always shows what the device delivers.

## Cost Tracking

`utils/cost_tracking.py` meters what each provider call reports (tokens, audio seconds) per session and applies a versioned snapshot of published list prices, so the figure is an **estimate**, not an invoice. Worker threads only update memory; the Tk thread flushes to `cost_history/` so live subtitles never wait for disk I/O. Only counters, provider/model ids and timestamps are persisted, never prompts, transcripts, audio or credentials. The history viewer's **Costs** tab renders it (`utils/cost_display.py`). Anthropic and Deepgram usage is currently not metered.

## Announcements

Independently of the translation pipeline, the operator can push a message onto the subtitle screen (megaphone button → text + duration). The message renders large and centred above the subtitles and below the disclaimer pill. The active-announcement state lives on the control panel, not on the subtitle window, so an "until stopped" announcement survives both a translation stop and the subtitle window being destroyed and recreated.

## Cost Guards

Every stage that can avoid an API call does:

- Silent segments are deleted before transcription; with the noise filter on, loud-but-speechless ones are too
- Sub-word fragments never reach a translation call
- Short streaming utterances are coalesced into one call instead of several
- Verified Quran verses and same-language text bypass the translation call entirely
- The Arabic re-transcription pass is skipped whenever its output would go unused
- The rolling summary is rate-limited by both count and time
- A running session auto-stops after 10 minutes without any transcription (streaming bills silent minutes)

## Threading Model

The pipeline runs on multiple threads (audio capture, segment writer, transcription/translation processor, streaming receive/feeder/reconnect-supervisor threads, async context summarizer, GUI main thread). The GUI communicates with the pipeline via queues polled from the Tk event loop; logging goes through the thread-safe `utils/logging.py`.

Each thread loop **captures its stop event at entry** rather than reading `controller.stop_event` live: `start()` replaces the event, so a thread that outlived a previous `stop()`'s join timeout (e.g. one blocked in an API call) would otherwise be re-armed by the next start and run as a zombie alongside the new session.

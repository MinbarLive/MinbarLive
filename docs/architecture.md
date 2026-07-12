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

All AI calls (transcription, translation, embeddings) go through the **provider abstraction** in `providers/` — see [providers.md](providers.md). The pipeline never imports an AI SDK directly.

## Pipelines

The Processing Strategy setting selects one of two pipelines:

### Streaming pipeline (default: "Real-time streaming")

1. Microphone audio is fed as small PCM chunks into a live connection to the streaming engine (OpenAI Realtime, Gemini Live, or Deepgram)
2. The engine sends back **interim transcripts** (word by word, self-correcting) and marks **utterance ends** on natural pauses
3. The live transcript line is shown on the subtitle window while the speaker talks (Realtime subtitle mode)
4. Each finished utterance goes through the full translation step (Athan matching, Quran RAG, context, LLM)
5. The settled translation replaces the live line; typical speech → subtitle latency is ~1–3 s
6. An utterance that never pauses is force-flushed after 12 s so latency stays bounded

### Segmented pipeline ("Chunk-based" / "Semantic buffering")

1. **Audio Capture** records into a ring buffer; 12 s segments with 3 s overlap are written as WAV (silent segments are skipped entirely)
2. **Transcription** converts each segment to text via the configured provider, walking a model fallback chain on errors
3. The **buffering strategy** groups transcriptions:
   - **Chunk-based** (segmented default): every segment is translated immediately (~4–14 s latency)
   - **Semantic buffering** (Beta): waits for sentence-ending punctuation before translating; flushes anyway after 3 segments or 10 s, and a stale buffer is flushed during silence. The sentence heuristics are Arabic-tuned.
4. The grouped text goes through the same translation step as above

## Translation Step

Every text to be translated passes through, in order:

1. **Same-language bypass** — if the source and target language are the same (or the source is "Automatic" and an Arabic-script transcription meets an Arabic target), the text is passed through without an LLM call
2. **Athan dictionary matching** — fuzzy matching against known Athan phrases (threshold 0.75) returns the curated translation directly
3. **Quran RAG matching** — the text is embedded and compared against 6,054 precomputed verse embeddings (one matrix-vector product, ~3 ms):
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
- **Rolling summary**: Updated every ~10 segments (async, no delay)
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

## Threading Model

The pipeline runs on multiple threads (audio capture, segment writer, transcription/translation processor, streaming receive thread, async context summarizer, GUI main thread). The GUI communicates with the pipeline via queues polled from the Tk event loop; logging goes through the thread-safe `utils/logging.py`.

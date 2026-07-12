<div align="center">
    <a href="https://github.com/mosait/MinbarLive" />
        <img alt="Logo" height="200px" src="./public/MinbarLive2.png">
    </a>
</div>

# MinbarLive - Islamic Live Translation

Real-time translation system for mosque lectures and prayers, supporting multiple languages.

## Overview

This application captures live audio from a microphone, transcribes and translates the speech using AI models, and displays the translation as subtitles on a full-screen window (ideal for a second monitor, a projector, or an OBS overlay).

By default it runs in **real-time streaming mode**: the spoken text appears word by word while the speaker talks, and the translation follows each finished utterance after ~1–3 seconds. You can choose your AI provider — **OpenAI** (default), **Google Gemini**, or **Anthropic Claude** — and a first-run setup wizard walks you through language, microphone, provider, and API key.

> **⚠️ Language Note:** The primary development and testing focus was **Arabic → German**. While the app supports 15+ source languages and 35+ target languages, other language combinations have not been extensively tested. The Quran and Athan dictionaries are available in **German, English, Turkish, Albanian, and Bosnian**. Contributions for additional language support are welcome!

### Key Features

- **Real-time streaming transcription** (default): live word-by-word transcript with utterance-based translation — engines: OpenAI Realtime, Google Gemini Live, or Deepgram
- **Segmented mode** as an alternative: chunk-based or semantic sentence buffering
- **Multiple AI providers**: translation via OpenAI, Google Gemini, or Anthropic Claude — switchable in the settings
- **Verified Quran verse output**: RAG matching over precomputed verse embeddings; high-confidence matches display the exact published translation (marked 📖) instead of an AI paraphrase
- Dictionary matching for Athan phrases
- **Bilingual subtitles**: optionally show the original text above the translation
- Three subtitle modes: Realtime feed, continuous ticker, or static display
- **Batch mode**: turn a pre-recorded audio/video file into an `.srt` subtitle file
- **Session history viewer** with AI-generated session summaries
- **Islamic mode toggle**: switch off the Quran/Athan features to use MinbarLive as a general live translator
- First-run setup wizard; control panel in 6 languages (DE, EN, AR, BS, SQ, TR); light & dark theme
- Multi-monitor support with transparent overlay option
- Secure API key storage using the OS keychain
- Automatic silence detection, retries with exponential backoff, model fallback chains

📚 **More details:** See the [docs/](docs/) folder for architecture, providers, configuration, and data file documentation.

## ⚠️ API Cost Warning

This application makes continuous API calls while running. **You will be charged for usage by your AI provider.**

Rough guide for the default OpenAI setup (segmented mode, Arabic → German):

| Usage Pattern                   | Transcription | Translation | Embeddings | **Total**        |
| ------------------------------- | ------------- | ----------- | ---------- | ---------------- |
| 1 hour session                  | ~$0.36        | ~$0.10      | ~$0.05     | **~$0.50**       |
| Weekly Friday prayer (1 hr × 4) | ~$1.44        | ~$0.40      | ~$0.20     | **~$2.00/month** |

- **Real-time streaming mode** (the default) bills every audio minute **including silence**, and translates per utterance (more, smaller translation calls). Expect a somewhat higher total than segmented mode for the same session.
- Costs differ per provider and model — check [OpenAI](https://openai.com/pricing), [Google Gemini](https://ai.google.dev/pricing), [Anthropic](https://www.anthropic.com/pricing), or [Deepgram](https://deepgram.com/pricing) pricing for current rates, and set a usage limit in your provider account to avoid surprises.

## Setup

### Prerequisites

- An API key for your AI provider — an **OpenAI key is the simplest option**: one key covers translation, real-time transcription, and Quran verse matching. (Gemini/Claude/Deepgram keys are only needed if you choose those providers; Claude has no speech-to-text, so it additionally needs an OpenAI key for transcription.)
- Audio input device (microphone or virtual audio cable)
- Python 3.10+ (Option B only)

> **Routing computer audio as input:** To translate audio playing on your computer (e.g., from a stream or recording), use a virtual audio cable to loop the system output back as a microphone input. [VB-CABLE](https://vb-audio.com/Cable/) is a free option for Windows and macOS.

### Option A: Use the EXE (recommended)

1. Download the latest EXE: [Click here](https://github.com/mosait/MinbarLive/releases)
2. Run `MinbarLive.exe`
3. Follow the first-run wizard: interface language & appearance → spoken/subtitle language → microphone → AI provider & API key → disclaimer. API key tutorial: [EN](https://youtu.be/OB99E7Y1cMA)/[DE](https://youtu.be/SISlgzB_qpQ?si=v3yiOK0-1C3GxYaf)
4. It's Running!

> **Windows SmartScreen:** You may see a warning because the EXE is not code-signed. Click "More info" → "Run anyway".

> **Platform Note:** The EXE is Windows-only. Linux users have had success with Wine. macOS is not supported via EXE.

### Option B: Build it yourself (Python)

```bash
git clone https://github.com/mosait/MinbarLive.git
cd MinbarLive
python -m venv .venv
.\.venv\Scripts\activate      # Windows
# source .venv/bin/activate   # Linux/Mac
pip install -r requirements.txt
python main.py
```

Enter your API key in the first-run wizard (stored securely in the OS keychain), or provide it via a `.env` file / environment variable (`OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPGRAM_API_KEY`).

Two windows will appear:

- **Control Panel** - Start/Stop, settings, batch mode, history, API key management
- **Subtitles** - Full-screen translated text display

Press `Escape` to exit.

## Real-time vs. Segmented Mode

The **Processing Strategy** dropdown in the control panel selects the pipeline:

| Strategy                          | How it works                                                                      | Speech → subtitle delay |
| --------------------------------- | --------------------------------------------------------------------------------- | ----------------------- |
| **Real-time streaming** (default) | Live transcript appears word by word; translation follows each finished utterance | ~1–3 s                  |
| **Chunk-based**                   | Fixed 12 s audio segments, each translated immediately                            | ~4–14 s                 |
| **Semantic buffering** (Beta)     | Buffers segments until a complete sentence is detected                            | ~5–15 s                 |

Real-time mode supports three transcription engines: **OpenAI Realtime** (default — uses your existing OpenAI key), **Google Gemini Live**, and **Deepgram Nova**. Segmented mode transcribes via OpenAI or Gemini. See [docs/providers.md](docs/providers.md).

## Batch Mode: Subtitle Files from Recordings

The **Batch / File** card in the control panel processes a pre-recorded audio or video file through the same transcription → Quran matching → translation pipeline and writes an `.srt` subtitle file next to the source file (e.g. `lecture.de.srt`).

- Any common audio/video format — non-WAV files are converted via **ffmpeg** (on Windows the app offers a one-time automatic download if ffmpeg is not installed)
- Transcription/translation model selectable per run
- Finished runs are stored in the session history (Batch tab)

## History & Session Summaries

The ⟲ button in the control panel opens the session history: browse past live sessions, batch runs, and log files, export transcripts, and generate an **AI summary** of a session in a language of your choice (summaries are saved alongside the history).

## Mirroring/Streaming/Record with OBS

Easiest way to mirror, stream or record with camera + subtitles using [OBS Studio](https://obsproject.com/):

1. **Add your camera**: Sources → Add → Video Capture Device
2. **Add the subtitle window**: Sources → Add → Window Capture → Select `[MinbarLive.exe]: MinbarLive Subtitles`
3. **Position subtitles at bottom**: Right-click the subtitle source → Transform → Edit Transform → Set "Positional Alignment" to **Bottom Center**
4. **Display on another monitor**: Right-click the canvas → Open Preview Projector → Select your monitor (press `Escape` to exit)
5. **Auto-restore projector on startup**: Go to File → Settings → General → Projectors → Enable "Save projectors on exit" to automatically reopen the projector window when OBS starts

This overlays the live translations on your camera feed for Mirroring, YouTube, Zoom, or recording.

## Runtime Files

Runtime files are written to a per-user app data folder:

- **Windows**: `%APPDATA%\MinbarLive\`
- **macOS**: `~/Library/Application Support/MinbarLive/`
- **Linux**: `~/.local/share/MinbarLive/`

API keys are **never** written there — they live in the OS keychain.

## Documentation

| Document                                               | Description                                            |
| ------------------------------------------------------ | ------------------------------------------------------ |
| [docs/architecture.md](docs/architecture.md)           | System architecture, pipelines, and data flow          |
| [docs/providers.md](docs/providers.md)                 | AI providers, transcription engines, models, API keys  |
| [docs/project-structure.md](docs/project-structure.md) | Full project tree and file descriptions                |
| [docs/configuration.md](docs/configuration.md)         | All configurable settings and constants                |
| [docs/data-files.md](docs/data-files.md)               | Quran/Athan translations, embeddings, adding languages |
| [docs/testing.md](docs/testing.md)                     | Running tests and coverage                             |

## Feedback

- **GitHub Issues**: [Open an issue](https://github.com/mosait/MinbarLive/issues)
- **Google Forms**: [Submit feedback](https://forms.gle/T7hvU4yEbVRM4PmWA) anonymously

## Contributing

Contributions are welcome! Please see [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

## Acknowledgments

MinbarLive would not exist without the help of:

- **[Helaloov](https://github.com/Helaloov)** & **[marxmoo](https://github.com/marxmoo)** — backend
- **[Merisgrund](https://github.com/Merisgrund)** — frontend

Barakallahu feekum 🌙

## License

GPL-3.0. See `LICENSE`.

# Project Structure

```
├── main.py                  # Entry point (single-instance guard, loads .env,
│                            #   runs the onboarding wizard on first start)
├── app_controller.py        # Thread lifecycle controller (segmented + streaming pipelines)
├── config.py                # Static technical constants and paths
├── version.py               # App version
├── requirements.txt         # Python dependencies
├── pytest.ini               # Pytest configuration
├── ruff.toml                # Linter configuration
├── MinbarLive.spec          # PyInstaller build spec
│
├── providers/                       # AI provider abstraction (see docs/providers.md)
│   ├── base.py                      # Protocols: Transcription/Translation/Embedding +
│   │                                #   StreamingTranscriptionProvider/StreamHandle
│   ├── __init__.py                  # Factories, model chains, per-provider key handling
│   ├── openai/                      # OpenAI: client singleton, transcription, translation,
│   │   └── realtime.py              #   embeddings + Realtime streaming STT (24 kHz)
│   ├── gemini/                      # Gemini: translation, transcription, embeddings +
│   │   └── realtime.py              #   Live-API streaming STT (lazy SDK import)
│   ├── anthropic/                   # Claude: translation only (lazy SDK import)
│   └── deepgram/                    # Deepgram: streaming STT only (lazy SDK import)
│
├── audio/                   # Audio capture and processing
│   ├── capture.py           # Ring buffer, silence detection
│   ├── device_support.py    # Input-device capability probing (channels, rates)
│   ├── level_meter.py       # Smoothed RMS/peak level behind the input meter
│   ├── loopback.py          # Registry of WASAPI loopback (system-audio) devices
│   ├── resampler.py         # Streaming windowed-sinc resampler (devices that
│   │                        #   cannot open at the pipeline rate, e.g. 48 kHz Linux mics)
│   ├── vad.py               # webrtcvad noise gate: has_speech + StreamNoiseGate
│   └── writer.py            # Async WAV segment writing
│
├── translation/             # Translation pipeline
│   ├── buffering.py         # Segmented-mode strategies (chunk-based, semantic)
│   ├── stt.py               # Shared STT helpers: model-fallback chain, Arabic re-pass
│   ├── dictionary.py        # Dictionary loading, Arabic normalization, Athan fuzzy matching
│   ├── rag.py               # Vectorized Quran verse matching over the .npz embedding matrix
│   └── translator.py        # Translation: same-language bypass, verified-verse bypass,
│                            #   RAG hints, code-switching prompt, Islamic/general mode
│
├── batch/                   # Batch mode: file → SRT / text
│   ├── processor.py         # VAD-style segmentation, ffmpeg conversion, pipeline run
│   ├── srt_writer.py        # SRT output (UTF-8 BOM)
│   └── text_writer.py       # Plain transcript + translation export (output format txt/both)
│
├── gui/                     # User interface — PySide6/Qt, the only GUI (see gui/AGENTS.md)
│   ├── app.py               # Qt application bootstrap (QApplication, wizard or panel)
│   ├── control_panel.py     # Control panel core (reflowing card grid, start/stop,
│   │                        #   theming, input-level meter, collapsible log panel)
│   ├── control_state.py     # Settings-derived rules (key/mode/strategy), toolkit-free, unit-tested
│   ├── pipeline_bridge.py   # Controller → GUI handoff as Qt signals (no polling loop)
│   ├── subtitle_window.py   # Full-screen subtitle overlay (realtime/continuous/static)
│   ├── subtitle_text.py     # Footer wording + oversized-block splitter (toolkit-free)
│   ├── settings_window.py   # Settings window + per-provider API key management
│   ├── batch_window.py      # Batch/File window (file picker, progress, ffmpeg download)
│   ├── history_window.py    # History | Batch | Costs | Log viewer + "Summarise session"
│   ├── announce_window.py   # Announcement window (message + duration)
│   ├── onboarding.py        # First-run setup wizard (5 steps)
│   ├── api_keys.py          # API key entry dialog (storage via providers.save_api_key)
│   ├── already_running.py   # "MinbarLive is already running" dialog
│   ├── dialogs.py           # Themed message/confirm dialogs — never QMessageBox
│   ├── widgets.py           # Shared controls (segmented button, dropdown, slider) + on-top helpers
│   ├── theme.py             # Application stylesheet built from the shared palette
│   ├── palette.py           # Theme palettes — plain hex, toolkit-free
│   ├── fonts.py             # Font families per platform and per script
│   ├── icons.py             # The cached QIcon every window carries (logo mark)
│   ├── i18n.py              # GUI string loading from data/translations/gui/
│   ├── levels.py            # Input-level dBFS scale shared by panel and wizard
│   ├── modal_host.py        # Integrated window style: in-app panels over a dim
│   │                        #   backdrop, clamping/centering, modal stack
│   ├── window_size.py       # One size rule for the content-sized secondary windows
│   ├── platform_setup.py    # QT_QPA_PLATFORM / logging rules, set before QApplication
│   ├── update_banner.py     # "A newer release exists" strip in the control panel
│   └── device_list.py       # Audio input device enumeration (mics + WASAPI loopback)
│
├── utils/                   # Utilities
│   ├── app_paths.py         # Per-user writable app data directory
│   ├── cleanup.py           # Log/history/batch file retention cleanup
│   ├── context_manager.py   # Adaptive context with async summarization
│   ├── cost_display.py      # Formatting/grouping of cost sessions for the GUI
│   ├── cost_tracking.py     # Provider usage metering + per-session cost history
│   ├── ffmpeg_download.py   # One-time ffmpeg download for batch mode (Windows)
│   ├── history.py           # Transcription/translation logging + history parsing
│   ├── icons.py             # Icon paths + logo mark drawing (gui/icons.py builds the QIcon)
│   ├── json_helpers.py      # JSON file I/O
│   ├── keyring_storage.py   # OS keychain (one entry per provider)
│   ├── logging.py           # Thread-safe logging
│   ├── retry.py             # Exponential backoff for API calls
│   ├── session_summary.py   # AI session summaries (history viewer)
│   ├── settings.py          # User preferences dataclass + model/provider lists
│   ├── update_check.py      # Anonymous startup check for a newer GitHub release
│   ├── user_messages.py     # Audience-facing status messages in the target language
│   └── windows_dpi.py       # UNUSED at runtime: Qt claims per-monitor-v2 itself and the
│                            #   EXE gets it from MinbarLive.manifest. Kept + tested as the
│                            #   record of the contract (calling it first took it from Qt)
│
├── data/                            # Static data files (see docs/data-files.md)
│   ├── embeddings/
│   │   ├── quran_embeddings_openai.npz  # Verse embedding matrix the app loads (OpenAI space)
│   │   ├── quran_embeddings_gemini.npz  # Optional Gemini-space matrix
│   │   └── quran_embeddings.json        # Raw notebook output (git LFS, not bundled)
│   └── translations/
│       ├── quran/                   # Verse translations (de, en, tr, sq, bs)
│       ├── athan/                   # Athan phrase translations (de, en, tr, sq, bs)
│       ├── gui/                     # Control panel UI strings (de, en, ar, bs, sq, tr)
│       ├── footer_translations.json # Subtitle disclaimer footer
│       └── status_messages.json     # Audience-facing status/error messages
│
├── notebooks/               # Development notebooks & scripts
│   ├── Build_Quran_EmbeddingSpace.ipynb  # Generate raw verse embeddings (JSON)
│   ├── build_embeddings_npz.py           # Convert/re-embed into the .npz the app loads
│   ├── build_quran_dict.py               # Rebuild translation dictionaries
│   └── test_translation_and_rag.ipynb    # Interactive RAG & translation testing
│
├── docs/                    # This documentation + the GitHub Pages landing page (index.html)
│
└── tests/                   # Pytest suite (1148 tests), see docs/testing.md
```

## Runtime Files

Runtime files are written to a per-user app data folder:

- **Windows**: `%APPDATA%\MinbarLive\`
- **macOS**: `~/Library/Application Support/MinbarLive/`
- **Linux**: `~/.local/share/MinbarLive/`

Contents:

- `history/` - Transcript + translation logs, plus `{date}.summary` sidecars for AI session summaries
- `logs/` - Daily application log files (e.g., `2026-07-10.log`)
- `recordings/` - Temporary WAV segments
- `batch/` - Per-run batch transcripts (shown in the history viewer's Batch tab)
- `cost_history/` - Per-session API usage counters and cost estimates (no text, no audio)
- `bin/` - ffmpeg, if downloaded via the batch card (Windows)
- `settings.json` - All user preferences (NOT the API keys)

> **Note:** API keys are stored in your OS keychain, never in settings.json. On a machine with no keychain backend at all they are kept for the running session only; see [providers.md](providers.md#api-keys).

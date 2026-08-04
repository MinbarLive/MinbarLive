# Testing

The project includes a test suite of 1148 tests using pytest. Provider tests run against faked SDK connections; no API keys or network access needed.

## Running Tests

```bash
# Run all tests
python -m pytest

# Run with verbose output
python -m pytest -v

# Run a specific test file
python -m pytest tests/test_dictionary.py

# Run with coverage (requires pytest-cov)
python -m pytest --cov=.
```

## Test Coverage

| Test File                    | Coverage Area                                                    |
| ---------------------------- | ---------------------------------------------------------------- |
| `test_audio_devices.py`      | Microphone enumeration, host-API selection, input stream settings |
| `test_audio_level.py`        | Input-level meter: dBFS mapping and controller routing           |
| `test_batch.py`              | Batch mode: segmentation, SRT output, ffmpeg handling, cancel    |
| `test_buffering.py`          | Chunk/semantic buffering strategies, stale-buffer flush          |
| `test_cleanup.py`            | Log/history/batch file retention                                 |
| `test_context_manager.py`    | Adaptive context management                                      |
| `test_control_state.py`      | Settings-derived control-panel rules (headless, no display)      |
| `test_cost_display.py`       | Cost formatting/grouping for the history viewer's Costs tab      |
| `test_cost_tracking.py`      | Usage metering, price table, per-session cost history files      |
| `test_dictionary.py`         | Arabic normalization, Athan fuzzy matching                       |
| `test_ffmpeg_download.py`    | One-time ffmpeg download/extraction                              |
| `test_gui.py`                | The whole Qt tree on real windows — see below (345 tests)        |
| `test_gui_translations.py`   | GUI translation files: all keys present in all 6 languages       |
| `test_history.py`            | History parsing, session listing, writer→reader roundtrip        |
| `test_json_helpers.py`       | JSON loading, edge cases                                         |
| `test_keyring_storage.py`    | Secure per-provider API key storage                              |
| `test_logging.py`            | Log file persistence: concurrent appends must not tear a line    |
| `test_logging_safety.py`     | Credential redaction — no API key shape reaches a log file       |
| `test_providers.py`          | Provider factories, model chains, streaming engines (faked SDKs) |
| `test_rag.py`                | Cosine similarity, embedding-space selection, RAG availability   |
| `test_resampler.py`          | Streaming windowed-sinc resampler, input-rate selection          |
| `test_retry.py`              | Exponential backoff for API calls                                |
| `test_segmented_pipeline.py` | Controller-level segmented pipeline (WAV → subtitle queue)       |
| `test_session_summary.py`    | AI session summaries                                             |
| `test_settings.py`           | Settings dataclass, migrations, language codes                   |
| `test_silence_detection.py`  | Audio silence detection                                          |
| `test_single_instance.py`    | POSIX `flock` instance lock (skipped on Windows, which uses a mutex) |
| `test_streaming_pipeline.py` | Controller-level streaming pipeline, live-transcript session, reconnect |
| `test_stt.py`                | Shared STT helpers: model fallback chain, Arabic re-pass, overlap dedup |
| `test_subtitle_split.py`     | Splitting oversized realtime blocks at sentence boundaries       |
| `test_translator.py`         | Verified-verse bypass, code-switching prompts, same-language and Islamic-mode behavior |
| `test_update_check.py`       | Version comparison, GitHub release fetch, failure tolerance      |
| `test_user_messages.py`      | Audience-facing localized status messages, error classification  |
| `test_vad.py`                | Noise gate: real-webrtcvad hiss/hum cases, quiet-speech boost    |
| `test_windows_dpi.py`        | The DPI-awareness helper and the PyInstaller manifest that embeds it |

**There is no `conftest.py`.** Every file is self-contained, deliberately: a shared
fixture file would couple the headless layer to the one that builds real windows.

## GUI Tests

The GUI is covered in two layers:

- **`test_control_state.py`**: the rules the panel derives from `Settings` (which providers need a key, which subtitle modes are offered, what a strategy choice does). These live in `gui/control_state.py`, import no GUI toolkit, and run headlessly in milliseconds. Most control-panel logic belongs here.
- **`test_gui.py`**: the Qt tree on real windows with a fake controller — subtitle overlay layout, control panel startup and start/stop, the settings and history windows, theming, the setup wizard. Every test in it locks in a defect that reached a real run. The module is skipped when PySide6 is missing, and the control-panel half imports lazily so the file still collects without a display.

Two traps that produce confident, wrong results:

- **Don't trust `QT_QPA_PLATFORM=offscreen`.** Four tests fail there and pass on the real platform, and on Windows the offscreen plugin loads no system fonts at all — every glyph renders as tofu, so geometry still measures but anything about rendered text is meaningless.
- **Run the suite on an idle machine.** The GUI tests build real windows; a live app window on the same desktop can stall them.

> **Still worth a human pass:** these tests drive handlers and geometry, not pixels. They cannot see that something is misaligned, clipped or the wrong colour; verify visual changes by running the app (`python main.py`).

**When adding control-panel logic:** if it only reads/writes `Settings`, put it in `gui/control_state.py` and test it headlessly. Reserve `test_gui.py` for behaviour that genuinely needs widgets; every test there builds a whole window, which is slow and needs a display.

Platform-specific tests use `pytest.mark.skipif(sys.platform != "...")`. **Never patch
`sys.platform` globally** — it applies to code that already imported it, and it has
crashed a whole run while spawning real windows.

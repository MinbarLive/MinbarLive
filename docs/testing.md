# Testing

The project includes a test suite of 860 tests using pytest. Provider tests run against faked SDK connections; no API keys or network access needed.

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
| `test_app_gui.py`            | Control-panel drive-through on a real window (fake controller)    |
| `test_batch.py`              | Batch mode: segmentation, SRT output, ffmpeg handling, cancel    |
| `test_control_state.py`      | Settings-derived control-panel rules (headless, no display)      |
| `test_buffering.py`          | Chunk/semantic buffering strategies, stale-buffer flush          |
| `test_cleanup.py`            | Log/history/batch file retention                                 |
| `test_context_manager.py`    | Adaptive context management                                      |
| `test_cost_display.py`       | Cost formatting/grouping for the history viewer's Costs tab      |
| `test_cost_tracking.py`      | Usage metering, price table, per-session cost history files      |
| `test_dictionary.py`         | Arabic normalization, Athan fuzzy matching                       |
| `test_dropdown_keyboard.py`  | Keyboard navigation in the shared themed dropdown                |
| `test_ffmpeg_download.py`    | One-time ffmpeg download/extraction                              |
| `test_gui_translations.py`   | GUI translation files: all keys present in all 6 languages       |
| `test_history.py`            | History parsing, session listing, writer→reader roundtrip        |
| `test_json_helpers.py`       | JSON loading, edge cases                                         |
| `test_keyring_storage.py`    | Secure per-provider API key storage                              |
| `test_providers.py`          | Provider factories, model chains, streaming engines (faked SDKs) |
| `test_rag.py`                | Cosine similarity, embedding-space selection, RAG availability   |
| `test_retry.py`              | Exponential backoff for API calls                                |
| `test_rtl_shaping.py`        | Arabic RTL reshaping on both the Windows and non-Windows branches |
| `test_scaling.py`            | Display-scaling clamp: fits small/high-DPI screens, never upscales |
| `test_segmented_pipeline.py` | Controller-level segmented pipeline (WAV → subtitle queue)       |
| `test_session_summary.py`    | AI session summaries                                             |
| `test_settings.py`           | Settings dataclass, migrations, language codes                   |
| `test_silence_detection.py`  | Audio silence detection                                          |
| `test_streaming_pipeline.py` | Controller-level streaming pipeline, live-transcript session, reconnect |
| `test_stt.py`                | Shared STT helpers: model fallback chain, Arabic re-pass, overlap dedup |
| `test_subtitle_font_reflow.py` | Subtitle reflow on font/typography changes, shallow windows     |
| `test_subtitle_split.py`     | Splitting oversized realtime blocks at sentence boundaries       |
| `test_subtitle_window_visuals.py` | Subtitle window rendering: fonts, colours, line stacking     |
| `test_translator.py`         | Verified-verse bypass, code-switching prompts, same-language and Islamic-mode behavior |
| `test_update_check.py`       | Version comparison, GitHub release fetch, failure tolerance      |
| `test_user_messages.py`      | Audience-facing localized status messages, error classification  |
| `test_vad.py`                | Noise gate: real-webrtcvad hiss/hum cases, quiet-speech boost    |
| `test_windows_dpi.py`        | Startup DPI awareness + the PyInstaller manifest that embeds it  |

## GUI Tests

The control panel is covered in two layers, deliberately:

- **`test_control_state.py`**: the rules the panel derives from `Settings` (which providers need a key, which subtitle modes are offered, what a strategy choice does). These live in `gui/control_state.py`, import no Tk, and run headlessly in milliseconds. Most control-panel logic belongs here.
- **`test_app_gui.py`**: a real `AppGUI` on a real Tk root with a fake controller, covering what genuinely needs a window: startup, start/stop, the settings window, theme/language switching, and that the panel is wired to the rules above. It is skipped automatically when no display is available.

> **Still worth a human pass:** these tests drive handlers, not pixels. They cannot see that something is misaligned, clipped or the wrong colour; verify visual changes by running the app (`python main.py`).

**When adding control-panel logic:** if it only reads/writes `Settings`, put it in `gui/control_state.py` and test it headlessly. Reserve `test_app_gui.py` for behaviour that genuinely needs widgets; every test there builds a whole window, which is slow and needs a display.

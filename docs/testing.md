# Testing

The project includes a test suite of 400+ tests using pytest. Provider tests run against faked SDK connections — no API keys or network access needed.

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
| `test_batch.py`              | Batch mode: segmentation, SRT output, ffmpeg handling, cancel    |
| `test_buffering.py`          | Chunk/semantic buffering strategies, stale-buffer flush          |
| `test_cleanup.py`            | Log/history/batch file retention                                 |
| `test_context_manager.py`    | Adaptive context management                                      |
| `test_dictionary.py`         | Arabic normalization, Athan fuzzy matching                       |
| `test_ffmpeg_download.py`    | One-time ffmpeg download/extraction                              |
| `test_gui_translations.py`   | GUI translation files: all keys present in all 6 languages       |
| `test_history.py`            | History parsing, session listing, writer→reader roundtrip        |
| `test_json_helpers.py`       | JSON loading, edge cases                                         |
| `test_keyring_storage.py`    | Secure per-provider API key storage                              |
| `test_providers.py`          | Provider factories, model chains, streaming engines (faked SDKs) |
| `test_rag.py`                | Cosine similarity, embedding-space selection, RAG availability   |
| `test_retry.py`              | Exponential backoff for API calls                                |
| `test_segmented_pipeline.py` | Controller-level segmented pipeline (WAV → subtitle queue)       |
| `test_session_summary.py`    | AI session summaries                                             |
| `test_settings.py`           | Settings dataclass, migrations, language codes                   |
| `test_silence_detection.py`  | Audio silence detection                                          |
| `test_streaming_pipeline.py` | Controller-level streaming pipeline, live-transcript session     |
| `test_stt.py`                | Shared STT helpers: model fallback chain, Arabic re-pass         |
| `test_translator.py`         | Verified-verse bypass, code-switching prompts, same-language and Islamic-mode behavior |
| `test_user_messages.py`      | Audience-facing localized status messages                        |

> **Note:** The pytest suite does **not** exercise the GUI — no test imports `gui/app_gui.py`. GUI changes additionally need a manual pass (or a scripted drive-through with a fake controller).

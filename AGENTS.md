# AGENTS.md — MinbarLive

Shared project context for AI coding agents and new contributors. Committed and public.

- Personal working agreement for the maintainer's sessions: `CLAUDE.md` (local, gitignored).
- Current branch state and next steps: `.claude/HANDOFF.md` (local, not auto-loaded).
- Session-by-session history: `.claude/DEVLOG.md` (local, not auto-loaded).
- Directory-scoped rules: [gui/AGENTS.md](gui/AGENTS.md).
- Step-by-step procedures live as skills in [.claude/skills/](.claude/skills/).

---

## What is MinbarLive?

An open-source, real-time Islamic live translation tool in Python. It captures live
microphone audio (a mosque lecture or Friday khutbah), transcribes the speech through a
cloud STT provider, and renders translated subtitles on a full-screen overlay for a
second monitor or OBS.

Primary use case: **Arabic → German** during mosque lectures (the most tested path).
15+ source and 35+ target languages are supported.

It is a **broadcaster/streamer-facing** tool — subtitles on a display — not an
audience-facing phone app. That distinction drives most product decisions below.

---

## Tech Stack

- **Language:** Python 3.11+ (`numpy`/`scipy` require it; CI and the release builds use 3.12)
- **GUI:** PySide6/Qt (`gui/`) — the CustomTkinter tree it replaced is gone (issue #44)
- **Audio:** `sounddevice` + ring buffer, webrtcvad noise gate, WAV segment writing
- **RAG:** in-memory cosine similarity over precomputed Quran verse embeddings — no vector DB
- **Packaging:** PyInstaller (`MinbarLive.spec` → Windows EXE, Linux AppImage, macOS .app).
  One-file on Windows/Linux; **onedir on macOS** — one-file inside a `.app` is an error in
  PyInstaller 7, and the `.app` is a directory either way (issue #51)
- **Testing:** pytest · **Linting:** ruff (`ruff.toml`) · **Secrets:** OS keychain via `keyring`

**Providers.** OpenAI is the default everywhere (see the decisions table): `gpt-5.2`
translation, `openai_realtime` streaming STT, `gpt-4o-transcribe` segmented STT,
`text-embedding-3-large` for the RAG space. Google Gemini and Anthropic remain
selectable. Model lists and fallback chains live in `utils/settings.py` and
`providers/<id>/` — **not** in `config.py`.

---

## Architecture

```
App Controller (thread lifecycle)
    │
    ├─▶ Audio Capture (ring buffer, silence detection, VAD gate)
    │       └─▶ Transcription (streaming or segmented STT)
    │               └─▶ Buffering Strategy (semantic or chunk-based)
    │                       └─▶ Translation (RAG + LLM)
    │                               └─▶ Subtitle window (full-screen overlay)
    │
    ├─▶ Context Manager (async rolling/hourly summaries → fed back to Translation)
    └─▶ Control GUI (settings, logs, start/stop)
```

**Data flow**

1. Audio → ring buffer segments (12 s, 3 s overlap, 16 kHz — see `config.py`)
2. Segment → STT → source-language text
3. Buffering strategy: `semantic` waits for a sentence end; `chunk` passes immediately
4. Context Manager keeps the last 3 raw segments + a rolling ~50-word summary + hourly
   ~20-word snapshots, capped around 1500 tokens
5. RAG: the text is embedded → one matrix-vector product against 6,054 precomputed verse
   embeddings → top-5 matches above the similarity threshold become translation hints
6. Dictionary fuzzy-matching for Athan phrases
7. The LLM translates with all of that context → subtitle window

All tunables (durations, thresholds, intervals, `EMBEDDING_MODEL`) live in `config.py`
with inline comments. Read them there — they are not duplicated in this file.

---

## Key Files

| Path | Purpose |
| --- | --- |
| `main.py` | Entry point (single-instance guard, `.env`, first-run wizard) |
| `app_controller.py` | Thread lifecycle — starts/stops the full pipeline |
| `streaming_session.py` | One live realtime-STT connection: feed/utterance queues, reconnect, stall watchdog, utterance accumulation. Built per `start()` by the controller |
| `config.py` | Static constants: durations, thresholds, model names |
| `audio/capture.py` | Ring buffer, silence detection |
| `audio/vad.py` | webrtcvad noise gate: `has_speech` (segmented/batch) + `StreamNoiseGate` (streaming) |
| `audio/resampler.py` | Windowed-sinc resampler for devices that can't open at the pipeline rate |
| `audio/writer.py` | Async WAV segment writing |
| `translation/buffering.py` | Semantic vs. chunk strategies |
| `translation/dictionary.py` | Dictionary loading, Arabic normalization, fuzzy Athan matching |
| `translation/rag.py` | Cosine similarity search over Quran embeddings |
| `translation/translator.py` | LLM translation with RAG hints and context |
| `translation/stt.py` | Shared STT fallback + Arabic re-pass helpers |
| `providers/base.py` | Provider Protocols (transcription, translation, embeddings) |
| `providers/__init__.py` | Provider factories driven by the `ai_provider` setting |
| `providers/<id>/` | Per-provider implementations — the only place importing each SDK |
| `batch/` | File/batch processing: `processor.py`, `srt_writer.py`, `text_writer.py` |
| `gui/` | PySide6 tree, the only GUI — see [gui/AGENTS.md](gui/AGENTS.md) |
| `bench/` | Translation-latency harness. **Makes real, paid API calls** — not a test, not in CI, never run by pytest |
| `utils/settings.py` | User-preferences dataclass, model lists, fallback chains, `GUI_LANGUAGES` |
| `utils/keyring_storage.py` | OS keychain integration |
| `gui/api_keys.py` | API key prompting (storage goes through `providers.save_api_key`) |
| `utils/context_manager.py` | Adaptive rolling + hourly async summarization |
| `utils/user_messages.py` | Audience-facing status messages in the target language |
| `utils/logging.py` | Thread-safe logging — use this, not `print` |
| `utils/` (rest) | `app_paths`, `cleanup`, `history`, `json_helpers`, `retry`, `icons`, `update_check` |

---

## Data Files

```
data/
├── embeddings/
│   ├── quran_embeddings_openai.npz   # shipped default space (6,054 verses × 3072 dims, L2-normalized float32)
│   ├── quran_embeddings_gemini.npz   # used only when ai_provider=gemini AND present
│   └── quran_embeddings.json         # raw notebook output (~418 MB, git LFS); NOT bundled into the EXE
└── translations/
    ├── quran/                  # Arabic → target-language verse mappings (de, en, tr, sq, bs)
    ├── athan/                  # Athan phrase translations (de, en, tr, sq, bs)
    ├── gui/                    # control-panel UI strings (de, en, ar, bs, sq, tr)
    ├── footer_translations.json
    └── status_messages.json    # audience-facing messages, per target language
```

- Quran text from `quranapi.pages.dev`; translations from `quranenc.com`.
- The dataset holds 6,054 verses (the source merges some ayahs), not the canonical 6,236 —
  embeddings cover all of them 1:1.
- Adding a language or regenerating embeddings: use the skills in `.claude/skills/`.

---

## Runtime Files (per user, never in the repo)

| Platform | Location |
| --- | --- |
| Windows | `%APPDATA%\MinbarLive\` |
| macOS | `~/Library/Application Support/MinbarLive/` |
| Linux | `~/.local/share/MinbarLive/` |

Contents: `history/`, `logs/`, `recordings/`, `settings.json`.
**API keys live in the OS keychain and are never written to any file.**

---

## Build & Test

```bash
python -m pytest                        # full suite
python -m pytest --cov=.                # with coverage (needs pytest-cov)
ruff check .                            # lint

pyinstaller MinbarLive.spec             # Windows EXE
```

Qt work needs the venv — `./venv/Scripts/python.exe`, see [gui/AGENTS.md](gui/AGENTS.md).

**Measure bundle size from a CI artifact, never from `site-packages`.** `requirements.txt`
pins **`PySide6-Essentials`**, so PyInstaller never collects the QML/3D/WebEngine payload in
the first place — `MinbarLive.spec`'s Qt binary filter drops 1 file on Windows and 0 on
Linux/macOS, and the exclusion lists' real saving is ~7 MB from the Python-level `excludes`.
A developer machine that also has **`PySide6-Addons`** installed measures a completely
different tree, which is where two separate "we can save 35+ MB" figures came from. Both
described a configuration nobody ships. The build prints
`Qt binaries: keeping N, dropping M` — read it from the run, and note `M == 0` is normal on
Linux/macOS rather than a broken filter.

**CI smoke-launches all three binaries.** `release.yml` starts the Windows EXE detached and
fails if it exits inside 30 s; Linux launches three ways (direct, AppImage without FUSE,
AppImage with no xcb libraries installed); macOS once. A green build therefore does mean the
binary opens — it does **not** mean any feature inside it works.

**`bench/` is not part of the suite and costs money.** `python bench/run_bench.py` runs the
real `translate_text` path (RAG + LLM) over a fixed Arabic corpus — 13 utterances × `-n`
repeats of billed API calls. Use it to answer a latency question with numbers, then compare
two runs with `--compare`. Results land in `bench/results/`, which is gitignored: a
committed latency figure is a claim nobody can reproduce.

Test conventions:

- **There is no `conftest.py`.** Each of the 35 test files is self-contained. Keep it that
  way — a shared fixture file would couple the window-building layer to the headless one.
- **Platform-specific tests use `pytest.mark.skipif(sys.platform != "...")`.** Never patch
  `sys.platform` globally: it applies to code that has already imported it, and it has
  crashed the whole run (exit 255) while spawning real GUI windows.
- The full suite can stall in the Qt GUI tests when a real app window is open on the same
  desktop. Run it on an idle machine before pushing.
- **`QT_QPA_PLATFORM=offscreen` is not a shortcut for a display** — four tests fail there
  and pass on the real platform, and on Windows it loads no fonts at all.

---

## Invariants

Break these and something ships broken:

- **Thread safety.** Audio, transcription, translation and GUI each run on their own
  thread. Log through `utils/logging.py`.
- **API keys never touch disk.** Everything goes through `utils/keyring_storage.py`. No
  keychain means session-only, with a notice — never a plaintext fallback.
- **Audience-facing strings are localized to the _target_ language.** Anything drawn on
  the subtitle window goes through `utils/user_messages.py` /
  `data/translations/status_messages.json`. Never hardcode.
- **RAG stays in-memory.** No external vector DB; the dataset is small enough and simple
  deployment is the point.
- **Embeddings must match `EMBEDDING_MODEL`.** Changing the model means regenerating the
  `.npz`, or RAG silently returns garbage.
- **API cost is a review criterion.** Any change to segment frequency, context size or
  model selection moves the ~$0.50/hr running cost. Say so in the change description.
- **Providers stay behind the Protocols in `providers/base.py`.** SDK imports belong in
  `providers/<id>/` and nowhere else.

---

## Decisions Already Made

Do not revisit without a new explicit decision.

| Decision | Rationale |
| --- | --- |
| Stay fully local, no cloud hosting | Core identity; privacy for mosque audio |
| No QR code / phone-based access | Most attendees aren't on the same network; Baian.ai already does this better |
| No mobile app | Separate platform, out of scope |
| In-memory RAG, no vector DB | Simple deployment, dataset small enough |
| Python Protocols, not ABCs, for providers | More flexible, no forced inheritance |
| ffmpeg for batch audio extraction | Industry standard, widely available |
| Debug log hidden by default | The control panel must stay approachable for AV volunteers |
| "Show original text" ON by default (2026-07-15) | Bilingual output is the expected default |
| "Show live transcript" OFF by default (2026-07-22) | `show_interim_transcript` defaults False in the dataclass **and** the load fallback. Independent of the bilingual switch — the 2026-07-14 coupling was removed |
| **OpenAI is the default provider everywhere** (2026-07-22, `f0427e5`) | Measured, not preferred: Gemini Live realtime transcribes at 0.75x realtime and falls behind without recovering; OpenAI Realtime holds 1.00x on the identical sample. One OpenAI key covers translation, realtime STT and RAG. `PROVIDER_RANKING` openai > gemini > anthropic applies to fallback only, never overrides an explicit choice |
| Streaming (`openai_realtime`) is the shipped transcription default | Same measurement. Chunk and semantic both stay as the *segmented* strategies (chunk is the segmented default; semantic's sentence heuristics are Arabic-tuned) — don't remove either |
| **Streaming translates whole sentences out of a running turn — `openai_realtime` ONLY** (2026-08-07, #26) | A pauseless speaker is one server-VAD turn, so translating at turn end alone showed nothing for a minute and then a wall of text. `UtteranceSession(sentence_flush=True)` cuts finished sentences out of the interim and records the words, so the turn's final transcript contributes only its tail. **Each engine handles the long-turn problem in the layer that can see it, and they are not interchangeable:** OpenAI emits no final until the turn ends (`_COMPLETED_EVENT` carries the transcript *and* the utterance end), so only a session-level flush can act — safe because its deltas are append-only per conversation item. **Deepgram never had the bug**: `is_final` (endpointing) is separate from `speech_final`, so finals arrive mid-turn, `_parts` fills and `STREAMING_MAX_UTTERANCE_SECONDS` fires as designed — and its interims are revised hypotheses, so the word-prefix bookkeeping would not hold. **Gemini Live had the bug and already fixes it in the provider** (`_maybe_cut_turn`, measured live at 89 s in one turn); a cut anywhere else arrives a second time when the turn completes. Don't "unify" these three. Costs more, smaller translation calls — that is the trade the fix makes |
| **Pre-release update notices are opt-in, default off** (2026-08-07, PR #79) | A pre-release is invisible to everyone by default: the update check and all six download buttons in `docs/index.html` read `/releases/latest`, which excludes pre-releases by definition. Ticking `include_prereleases` points **only the in-app check** at `RELEASE_LIST_API_URL` (the full list) and takes the **highest version**, not the first entry — the list arrives newest-*published* first, which is not the same ordering. The website buttons stay on `/releases/latest` on purpose: the landing page serves stable. **`_parse_version` must keep comparing suffix text** (`beta < rc`, `rc.1 < rc.2 < rc.10`, every pre-release below its final) — it previously discarded the suffix, which made `rc.1` and `rc.2` compare equal and would have made the whole feature a no-op. **Consequence for releases: re-pointing an existing pre-release tag notifies nobody**, because the version string is unchanged — an opted-in tester keeps the old bytes with no signal. Cut a new suffix instead of moving a published one |
| **API keys are NEVER written to disk** (2026-07-29, PR #43) | Supersedes the earlier "OpenAI-only plaintext fallback" exception. No keychain ⇒ session-only for every provider; a legacy plaintext key is migrated into the keychain or deleted. `has_insecure_key_fallback()` was deleted — don't reintroduce a caller |
| **`integrated` is the default window style, on every platform** (2026-08-04) | Maintainer's call after using the Qt panels. Applies to new installs only — `save_settings` writes `window_style` out, so an existing `settings.json` keeps its value. There is **no platform gate any more**: the Qt host reparents the panels into the control panel as child widgets and paints the dim into its own back buffer, so nothing is asked of the window manager (`gui/modal_host.py`). The Windows-only gate from PR #25 belonged to the Tk tree, which built them as borderless top-levels and needed per-window alpha — which X11 without a compositor ignores |
| Window behaviour is two 3-way selectors, not four checkboxes (2026-07-24, PR #22) | `subtitle_hide_mode` (never/stopped/always) and `always_on_top_mode` (never/running/always) replace the old booleans; old values migrate on load |
| Qt migration uses QtWidgets only — no QML (2026-07-30, #44) | The Phase 0 spike hit 60 fps, per-pixel alpha and correct Arabic with plain `QWidget`/`QPainter`. QtQuick needs `PySide6-Addons` (634 MB) and is a second language for contributors |
| Qt keeps the Tk control arrangement (2026-07-30, #44) | Segmented buttons for themes and both 3-way selectors, −/+ steppers for font size and scroll speed, slider only for height. Don't swap in dropdowns |
| **Static mode splits on the Transparent toggle** (2026-08-07) | *Transparent* static has no backdrop of its own, so it takes the whole monitor and its backdrop is a per-line ribbon hugging the text (`gui/subtitle_window.py _ribbon_rects`) — a full-height window then paints only as much as the text needs. The height slider becomes a **lift** there: subtitles and footer pill move up together, capped at 50%. *Opaque* static stays a band, because full height would wash the whole screen at the backdrop opacity; there the **text is fitted into the band** rather than overflowing it, at the largest size that fits. One stored field (`window_height_percent`) carries both meanings: read clamped, written only when the slider is dragged, so neither mode rewrites the other's value |
| **Side by side draws the original bold AND upright** (2026-08-07) | Weight was decided in #49; the upright face follows for the same reason. Italic marks the original as subordinate to its translation, which it only is when stacked above one — in a row of two equals it read as a quotation beside a sentence. Stacked keeps italic; Arabic was never italic anywhere |
| Qt subtitle backdrop defaults to opacity 75 (alpha 190/255) (2026-07-30, #44) | Reviewed against live video and chosen. Adjustable 0–100; a test pins the default. The control exists to adjust it, not to replace the decision |
| **The Qt tree asks for X11 (xcb) before Wayland on Linux** (2026-08-04, #44) | A Wayland client cannot position its own windows and has no always-on-top protocol — the subtitle overlay is exactly those two things, and under Wayland the compositor centred it and the always-on-top setting did nothing. `gui/platform_setup.py` sets `QT_QPA_PLATFORM=xcb;wayland` (fallback kept, so a session without XWayland still starts); an explicit `QT_QPA_PLATFORM` always wins. The plugin that loaded is logged at startup |
| **Arabic source lines stay regular weight — in the stacked layout** (2026-08-05) | They were bold in the Tk overlay and are regular in Qt. Maintainer's call after seeing both: keep the Qt weight. Don't "restore parity" here. **Narrowed 2026-08-06 (#49):** the side-by-side layout draws the original bold, because there it is the other half of the row rather than a subordinate line under its translation |
| **Closing the batch window asks, it does not decide** (2026-08-05) | Qt cancels the run on close; Tk let it finish. Neither is right silently — closing a window is not a statement about the job. A dialog on close offers *cancel the run* or *keep it running in the background*. Not implemented yet |
| **Don't cut over to Qt before Linux/macOS verification** (2026-07-30, #44) — satisfied | The migration exists to fix issues #35/#39, which are Linux/macOS bugs, so deleting the working Tk tree before Qt had run there would have been reckless. Both platforms ran the Qt tree first; the cut-over followed on 2026-08-04 |

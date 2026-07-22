# Continuous Integration

GitHub Actions runs the test suite and a lint check on every pull request, and
on every push to `main`. The workflow lives in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

| Job           | Runner           | What it does                                            |
| ------------- | ---------------- | ------------------------------------------------------- |
| `test`        | `windows-latest` | `pip install -r requirements.txt` + `pytest`            |
| `linux-smoke` | `ubuntu-latest`  | the same suite under `xvfb`, plus the Linux system libs |
| `lint`        | `ubuntu-latest`  | `ruff check` on the Python files the PR changed         |

`test` runs on Windows because that is the primary target: the code has
`sys.platform == "win32"` branches (RTL shaping in `gui/subtitle_window.py`, DPI
awareness in `gui/scaling.py`), and roughly a tenth of the suite builds real Tk
windows. `lint` has no such constraint and runs on Linux, which starts faster.

`linux-smoke` exists because Linux users do run MinbarLive from source, and
until it was added nothing in CI ever executed a line of the Linux branches.
Both Linux bugs reported so far — the fatal X11 `BadLength` from passing a
3200×3200 PNG to `wm iconphoto`, and CustomTkinter's missing `<Button-4>`/
`<Button-5>` wheel bindings — reached users before anyone noticed. It installs
three system packages the runner does not carry:

| Package         | Needed for                                                              |
| --------------- | ----------------------------------------------------------------------- |
| `tk`            | `setup-python`'s interpreter links against libtk8.6/libtcl8.6; without them `import tkinter` fails outright |
| `libportaudio2` | the Linux `sounddevice` wheel does not bundle PortAudio, unlike the Windows and macOS ones |
| `xvfb`          | `tests/test_app_gui.py` and friends build real Tk windows, which need a display |

It is a *smoke* job, not a guarantee: a green run means the code imports and the
headless tests pass on Linux, not that the subtitle overlay looks right there.

Fork pull requests are handled by the `pull_request` trigger, so they run with a
read-only token and no access to repository secrets. **Do not change this to
`pull_request_target`**: that would give fork code write access.

## Git LFS is deliberately disabled

The checkout step pins `lfs: false`. This is not an oversight.

The three files tracked by LFS total roughly 600 MB:

| File                                       | Size   |
| ------------------------------------------ | ------ |
| `data/embeddings/quran_embeddings.json`    | 418 MB |
| `data/embeddings/quran_embeddings_gemini.npz` | 91 MB  |
| `data/embeddings/quran_embeddings_openai.npz` | 91 MB  |

GitHub's free LFS allowance is 1 GB of bandwidth per month, so fetching them
would exhaust the quota in under two CI runs.

No *test* needs them. Without LFS, checkout writes ~130-byte pointer stubs in
their place; `_load_npz` in `translation/rag.py` catches the resulting load
failure, logs an error and returns an empty store, so RAG reports itself
unavailable and the suite passes unchanged. This was verified by cloning with
`GIT_LFS_SKIP_SMUDGE=1` and running the full suite against the stubs.

If you ever add a test that genuinely requires real embeddings, mark it to skip
when `is_rag_available()` is false rather than enabling LFS in CI.

The release workflow is the exception: the EXE bundles the two `.npz` matrices,
so it pulls them, but selectively, and never the raw JSON. See below.

## Release builds

[`.github/workflows/release.yml`](../.github/workflows/release.yml) builds the
Windows EXE. Pushing a `v*` tag also publishes it; `workflow_dispatch` builds
without publishing, for testing the build itself. A second job, `build-linux`,
builds an experimental Linux binary that stays a workflow artifact — see
[below](#the-linux-build-is-experimental-and-unpublished).

It runs the suite before touching LFS, so the tests see the same pointer stubs
the `test` job does, then pulls only the matrices:

```
git lfs pull --include="data/embeddings/*.npz"
```

That is ~182 MB per release build against the 1 GB monthly LFS allowance,
roughly five builds a month. `lfs: true` on the checkout step would pull all
600 MB and exhaust the quota in under two.

Three steps exist to fail loudly rather than ship something broken:

| Step                           | Catches                                                                                                   |
| ------------------------------ | --------------------------------------------------------------------------------------------------------- |
| Check the tag matches version.py | A tag like `v1.0.1-beta` on a `1.0.0-beta` build. The in-app update check compares release tags against the built-in version, so a mismatch prompts users forever or never |
| Verify the matrices are real   | An exhausted LFS quota, which leaves pointer stubs behind *without* failing the pull; the EXE would build fine with Quran verse matching silently dead |
| Verify the EXE                 | A build that lost the `data/` bundle, which shows up as a far smaller file                                |

The asset must keep the exact name `MinbarLive.exe` and the release must be
marked latest (`--latest`, and never pre-release): the website's download
buttons and the in-app update check both resolve
`releases/latest/download/MinbarLive.exe`.

Every release description starts with the download instructions:

```
**Windows**
- "MinbarLive.exe" herunterladen
- Download "MinbarLive.exe"
```

`gh` prepends `--notes-file` to what `--generate-notes` produces, so the
generated changelog follows underneath. If the release already exists because
it was created through the web UI, the workflow prepends the block to the
existing description instead, and skips that if the block is already there, so
hand-written notes are never overwritten.

## The Linux build is experimental and unpublished

`build-linux` runs after `build` and produces `dist/MinbarLive`, an ELF binary,
as the workflow artifact `MinbarLive-linux`. It is **not** attached to the
release, and the release notes stay Windows-only. Download the artifact from
the workflow run to test it.

That is not timidity about the packaging — the packaging works. It is that
nobody has run MinbarLive on a Linux desktop yet, and these are Windows-only
today:

| Feature                              | On Linux                                                                 |
| ------------------------------------ | ------------------------------------------------------------------------ |
| Borderless overlay visible to OBS    | The `WS_EX_APPWINDOW` styling in `gui/subtitle_window.py` is Win32; Linux falls back to a bare `overrideredirect` window |
| Transparent static mode              | `wm attributes -transparentcolor` does not exist outside Windows          |
| Loopback capture ("what the speakers play") | WASAPI-only by design, so Linux has microphone input only          |
| OS keychain                          | Without a Secret Service backend, `utils/settings.set_saved_api_key` falls back to plaintext for OpenAI and to session-only for every other provider — a Gemini key is lost on restart |
| ffmpeg download-on-first-use         | Windows-only; batch mode expects ffmpeg from the package manager          |

Publishing the binary means owning those as support questions. Before flipping
it into the release, run it on a Linux desktop and decide which of them to fix,
document, or hide in the UI.

### Why `ubuntu-22.04` and not `ubuntu-latest`

A PyInstaller binary is glibc-pinned: it starts only on a glibc at least as new
as the one it was built against. Building on 24.04 (glibc 2.39) would produce a
binary that fails on Debian 12, Ubuntu 22.04 and everything older — the opposite
of the EXE, which runs on any Windows 10 or newer. 22.04 (glibc 2.35) is the
oldest runner GitHub still provides. When that image is retired, build inside a
manylinux container or switch to AppImage rather than moving the floor up.

### The matrices come from the Windows job, not from LFS

`build` uploads `data/embeddings/*.npz` as a short-lived `embeddings` artifact
and `build-linux` downloads it over the pointer stubs. A second `git lfs pull`
would cost another ~182 MB against the 1 GB monthly allowance, halving the
number of releases that fit in it. The price is that the two jobs run in
sequence rather than in parallel.

### Two steps fail loudly

| Step                    | Catches                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------ |
| Verify the binary       | A build that lost the `data/` bundle, which shows up as a far smaller file                        |
| Smoke-launch the binary | A startup crash — a shared library the bundle missed, an X11 request the toolkit rejects. The binary is launched under `xvfb` with a 30-second `timeout`, so **exit 124 is the success case**: it means the app was still running when the timeout killed it, sitting in the onboarding wizard. Any other exit code means it died on its own |

## Lint checks changed files only

`ruff check` runs against the files the pull request touches, not the whole
repository, because the repo carries pre-existing findings that are
intentionally left as they are. A repository-wide check would fail on day one
and teach everyone to ignore it.

Two files additionally carry a documented exemption in
[`ruff.toml`](../ruff.toml) under `[lint.per-file-ignores]`, because their
findings predate CI and appear in files a pull request often touches for
unrelated reasons:

| File                     | Ignored        | Why                                                                                 |
| ------------------------ | -------------- | ----------------------------------------------------------------------------------- |
| `gui/subtitle_window.py` | `I001`, `E402` | Imports sit below the `arabic_reshaper` try-block and module-level regex constants   |
| `gui/dropdown.py`        | `E741`         | Ambiguous loop variable `l`                                                          |

These are a baseline, not a blanket pass: both files are still checked for every
other rule, and every other file is still checked for these rules. Remove an
entry once the underlying finding is fixed.

## Required status checks

The check names only appear in the branch ruleset's picker after the workflow
has run at least once. Once it has, add `test` and `lint` under **Require status
checks to pass**, and enable **Require branches to be up to date before
merging** so a green check cannot go stale against a moved `main`.

Leave `linux-smoke` out of the required set until it has been green on a few
pull requests. It has never run, so its first result is information about the
codebase, not a verdict on the branch it happens to run against.

## Python version

CI pins Python 3.12. Despite the comment in `requirements.txt`, 3.10 is not
supported: `numpy==2.4.0` and `scipy==1.16.3` both declare
`Requires-Python >=3.11`.

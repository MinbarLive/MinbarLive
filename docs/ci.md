# Continuous Integration

GitHub Actions runs the test suite and a lint check on every pull request, and
on every push to `main`. The workflow lives in
[`.github/workflows/ci.yml`](../.github/workflows/ci.yml).

| Job    | Runner          | What it does                                     |
| ------ | --------------- | ------------------------------------------------ |
| `test` | `windows-latest` | `pip install -r requirements.txt` + `pytest`     |
| `lint` | `ubuntu-latest`  | `ruff check` on the Python files the PR changed  |

`test` runs on Windows because that is the primary target: the code has
`sys.platform == "win32"` branches (RTL shaping in `gui/subtitle_window.py`, DPI
awareness in `gui/scaling.py`), and roughly a tenth of the suite builds real Tk
windows. `lint` has no such constraint and runs on Linux, which starts faster.

Fork pull requests are handled by the `pull_request` trigger, so they run with a
read-only token and no access to repository secrets. **Do not change this to
`pull_request_target`** — that would give fork code write access.

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

Nothing needs them. Without LFS, checkout writes ~130-byte pointer stubs in
their place; `_load_npz` in `translation/rag.py` catches the resulting load
failure, logs an error and returns an empty store, so RAG reports itself
unavailable and the suite passes unchanged. This was verified by cloning with
`GIT_LFS_SKIP_SMUDGE=1` and running the full suite against the stubs.

If you ever add a test that genuinely requires real embeddings, mark it to skip
when `is_rag_available()` is false rather than enabling LFS in CI.

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

## Python version

CI pins Python 3.12. Despite the comment in `requirements.txt`, 3.10 is not
supported: `numpy==2.4.0` and `scipy==1.16.3` both declare
`Requires-Python >=3.11`.

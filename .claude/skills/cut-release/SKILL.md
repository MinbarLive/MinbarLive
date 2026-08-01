---
name: cut-release
description: Cut a MinbarLive release — bump version.py, tag, and let CI build and publish the Windows EXE, Linux AppImage and macOS .app. Use when asked to release, ship a version, publish a build, or when the in-app update banner or website download buttons need a new version to point at.
---

# Cutting a release

Releases are **entirely CI-driven**. Pushing a `v*` tag to `.github/workflows/release.yml`
builds and publishes all three platforms. There is no local build step to hand off.

**Never run any of this without the maintainer explicitly asking in the current session.**
Tagging is a push, and a published release is visible to users immediately.

## The one rule that breaks users

**The tag must equal `__version__` in `version.py`, minus the leading `v`.**

CI enforces it (`Check the tag matches version.py`) and fails the build on mismatch — but
understand *why*: the in-app update check compares release tags against the built-in
version. A mismatch makes every installed copy either prompt forever or never prompt
again.

`v1.0.1-beta` ⇄ `__version__ = "1.0.1-beta"`.

## Steps

1. **Confirm the branch is merged to `main`.** `main` is protected — changes land through
   a PR. Tag the merge commit, not a feature branch.

2. **Bump `version.py`.** Update `__version__` and add a bullet to the version-history
   comment block at the bottom of the file.

3. **Run the full suite on an idle machine.**
   ```bash
   python -m pytest -q
   ruff check .
   ```
   The Tk GUI tests stall if a real app window is open on the same desktop. CI runs tests
   too, but finding it here is cheaper than a failed release.

4. **Commit the bump** — `chore: release vX.Y.Z` — and get it onto `main` via PR.

5. **Tag and push the tag** (only on explicit instruction):
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

6. **Watch the run.**
   ```bash
   gh run watch
   ```
   Three jobs: `build` (windows-latest → EXE), `build-linux` (ubuntu-22.04 → AppImage),
   `build-macos` (macos-14 → arm64 `.app`). Each verifies the LFS embedding matrices are
   real content and not pointer files, smoke-launches its binary, checksums it, and
   publishes it to the release.

7. **Confirm the release is *published*, not just tagged.** A tag alone is not enough —
   the in-app update banner and the website download buttons both read the GitHub
   *release*. Check that all three assets plus checksums are attached.

## Re-tagging

Moving an existing tag works and has been done before (`v1.0.0-beta` was moved twice), but
it needs a force-push of the tag and it re-runs every platform. Prefer a new version.

## Known state

- macOS builds are **arm64 only** (decided; no Intel) and are **unsigned/un-notarized** —
  users get a Gatekeeper warning. Tracked in issue #17.
- Windows builds are **unsigned**. Code signing is paused: SignPath Foundation rejected
  the application, Azure Trusted Signing is unavailable to German individuals, and no
  certificate grants instant SmartScreen reputation anyway. The decision was free + wait.
  ⚠️ The website's `code-signing.html` still claims signing is "being set up" — that is
  false and should be corrected.
- The Linux AppImage is FUSE-less (uruntime) and self-integrating. AppImage only — `.deb`
  was rejected as Debian-specific.

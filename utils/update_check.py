"""Startup check for a newer MinbarLive release on GitHub.

One anonymous GET to the GitHub releases API per app launch, opt-out via
the ``check_for_updates`` setting. No telemetry: GitHub only sees the
request IP, nothing about the installation. Any failure (offline,
rate-limited, malformed response) is silent — the app must never block
or nag because the update check couldn't run.

Pre-releases stay invisible unless the user asks for them: the
``include_prereleases`` setting (default off) is the only thing that widens
the check from "newest final release" to "newest release of any kind". The
running version never decides this on its own — v1.0.0-beta shipped as the
public stable download, so a tag suffix says nothing about who wants rcs.
"""

from __future__ import annotations

import json
import urllib.request
from typing import NamedTuple

from utils.logging import log
from version import __version__

LATEST_RELEASE_API_URL = (
    "https://api.github.com/repos/MinbarLive/MinbarLive/releases/latest"
)
# The full list. Unlike /latest — which GitHub defines as the newest
# non-prerelease, non-draft release — this one does include pre-releases.
# Only pre-release builds read it. Anonymous requests never see drafts.
RELEASE_LIST_API_URL = "https://api.github.com/repos/MinbarLive/MinbarLive/releases"
RELEASES_PAGE_URL = "https://github.com/MinbarLive/MinbarLive/releases/latest"

# Any html_url we are willing to open in the user's browser must start with
# this — see fetch_latest_release.
_RELEASE_URL_PREFIX = "https://github.com/MinbarLive/MinbarLive/releases/"

_TIMEOUT_SECONDS = 10


class UpdateInfo(NamedTuple):
    version: str  # display version, without the leading "v"
    url: str  # release page to open in the browser


def _strip_v(version: str) -> str:
    text = version.strip()
    return text[1:] if text[:1] in ("v", "V") else text


def _suffix_key(suffix: str) -> tuple[tuple[int, int, str], ...]:
    """Ordering key for a pre-release suffix like ``rc.1``.

    Dot-separated parts, numeric ones compared as numbers and sorted below
    text ones (semver's rule), so ``rc.1 < rc.2 < rc.10`` and ``beta < rc``.
    Without this every suffix would compare equal and an rc.1 build would
    never be offered rc.2.
    """
    return tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part)
        for part in suffix.split(".")
    )


def _parse_version(
    version: str,
) -> tuple[tuple[int, ...], int, tuple[tuple[int, int, str], ...]] | None:
    """Ordering key for a release tag like ``v1.2.0-rc.1``.

    Returns ``(numbers, is_final, suffix)`` where any pre-release suffix
    makes ``is_final`` 0, so ``1.0.0`` sorts above ``1.0.0-rc.2``. Numbers
    are padded to three parts so ``1.1`` and ``1.1.0`` compare equal, and
    ``suffix`` orders pre-releases of the same version among themselves.
    None when the tag isn't a version at all.
    """
    text = _strip_v(version)
    nums_part, _, suffix = text.partition("-")
    try:
        nums = tuple(int(part) for part in nums_part.split("."))
    except ValueError:
        return None
    if len(nums) < 3:
        nums += (0,) * (3 - len(nums))
    # A final has no suffix to compare; is_final already outranks every
    # pre-release of the same numbers, so an empty key is never reached.
    return nums, (0 if suffix else 1), _suffix_key(suffix) if suffix else ()


def is_newer_version(remote: str, current: str = __version__) -> bool:
    """True when ``remote`` is a strictly newer version than ``current``."""
    remote_key = _parse_version(remote)
    current_key = _parse_version(current)
    if remote_key is None or current_key is None:
        return False
    return remote_key > current_key


def _fetch_json(url: str):
    """Anonymous GET against the GitHub API, decoded. Raises on failure."""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "MinbarLive",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _release_from(entry: object) -> tuple[str, str] | None:
    """``(tag_name, html_url)`` out of one release object, None if unusable.

    The banner hands the URL straight to webbrowser.open(), so only accept a
    real release page on github.com — never an arbitrary scheme or host out
    of the response body.
    """
    if not isinstance(entry, dict):
        return None
    tag = entry.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    url = entry.get("html_url")
    if not isinstance(url, str) or not url.startswith(_RELEASE_URL_PREFIX):
        url = RELEASES_PAGE_URL
    return tag, url


def fetch_latest_release(include_prereleases: bool = False) -> tuple[str, str] | None:
    """``(tag_name, html_url)`` of the newest release GitHub offers.

    Default is ``/releases/latest``, the newest final release. With
    ``include_prereleases`` the full list is read instead and the highest
    version wins, pre-release or not.

    Raises on network errors; returns None on a malformed response.
    """
    if not include_prereleases:
        return _release_from(_fetch_json(LATEST_RELEASE_API_URL))

    data = _fetch_json(RELEASE_LIST_API_URL)
    if not isinstance(data, list):
        return None
    best: tuple[str, str] | None = None
    best_key = None
    for entry in data:
        found = _release_from(entry)
        if found is None:
            continue
        key = _parse_version(found[0])
        # The list arrives newest-published first, which is not the same as
        # newest version — a patch on an older branch can be published later.
        if key is not None and (best_key is None or key > best_key):
            best, best_key = found, key
    return best


def check_for_update(include_prereleases: bool = False) -> UpdateInfo | None:
    """Return UpdateInfo when a newer release exists, else None.

    ``include_prereleases`` mirrors the setting of the same name: off, an rc
    is never offered; on, the newest release of any kind counts.

    Never raises — designed for a fire-and-forget background thread at
    startup.
    """
    try:
        fetched = fetch_latest_release(include_prereleases)
        if fetched is None:
            return None
        tag, url = fetched
        if is_newer_version(tag, __version__):
            return UpdateInfo(version=_strip_v(tag), url=url)
        return None
    except Exception as exc:
        log(f"Update check skipped: {exc}", level="DEBUG")
        return None

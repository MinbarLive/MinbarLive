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

# Where the notice sends the user. The project's own page, not the GitHub
# release it came from: its download buttons are per platform and named, where
# the release page asks a non-technical user to expand "Assets" and pick the
# right file out of a list that includes an AppImage and two source archives.
#
# A constant, and never a URL out of the response body — the browser is opened
# with it, so the answer must not be something the network gets to choose.
# **The page's buttons read ``/releases/latest``**, which by GitHub's own
# definition excludes pre-releases: someone on the pre-release channel
# (``include_prereleases``) is told about an rc and offered the stable build
# when they get there. Opt-in, and testers are handed the tag directly, so it
# is left as the cost of one destination for everyone.
DOWNLOAD_PAGE_URL = "https://minbarlive.info/"

_TIMEOUT_SECONDS = 10


class UpdateInfo(NamedTuple):
    version: str  # display version, without the leading "v"
    url: str  # the page to open in the browser


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


def _tag_of(entry: object) -> str | None:
    """``tag_name`` out of one release object, None if unusable."""
    if not isinstance(entry, dict):
        return None
    tag = entry.get("tag_name")
    return tag if isinstance(tag, str) and tag else None


def fetch_latest_release(include_prereleases: bool = False) -> str | None:
    """The ``tag_name`` of the newest release GitHub offers.

    Default is ``/releases/latest``, the newest final release. With
    ``include_prereleases`` the full list is read instead and the highest
    version wins, pre-release or not.

    Raises on network errors; returns None on a malformed response.
    """
    if not include_prereleases:
        return _tag_of(_fetch_json(LATEST_RELEASE_API_URL))

    data = _fetch_json(RELEASE_LIST_API_URL)
    if not isinstance(data, list):
        return None
    best: str | None = None
    best_key = None
    for entry in data:
        tag = _tag_of(entry)
        if tag is None:
            continue
        key = _parse_version(tag)
        # The list arrives newest-published first, which is not the same as
        # newest version — a patch on an older branch can be published later.
        if key is not None and (best_key is None or key > best_key):
            best, best_key = tag, key
    return best


def check_for_update(include_prereleases: bool = False) -> UpdateInfo | None:
    """Return UpdateInfo when a newer release exists, else None.

    ``include_prereleases`` mirrors the setting of the same name: off, an rc
    is never offered; on, the newest release of any kind counts.

    Never raises — designed for a fire-and-forget background thread at
    startup.
    """
    try:
        tag = fetch_latest_release(include_prereleases)
        if tag is None:
            return None
        if is_newer_version(tag, __version__):
            return UpdateInfo(version=_strip_v(tag), url=DOWNLOAD_PAGE_URL)
        return None
    except Exception as exc:
        log(f"Update check skipped: {exc}", level="DEBUG")
        return None

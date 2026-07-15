"""Startup check for a newer MinbarLive release on GitHub.

One anonymous GET to the GitHub releases API per app launch, opt-out via
the ``check_for_updates`` setting. No telemetry: GitHub only sees the
request IP, nothing about the installation. Any failure (offline,
rate-limited, malformed response) is silent — the app must never block
or nag because the update check couldn't run.
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
RELEASES_PAGE_URL = "https://github.com/MinbarLive/MinbarLive/releases/latest"

_TIMEOUT_SECONDS = 10


class UpdateInfo(NamedTuple):
    version: str  # display version, without the leading "v"
    url: str  # release page to open in the browser


def _strip_v(version: str) -> str:
    text = version.strip()
    return text[1:] if text[:1] in ("v", "V") else text


def _parse_version(version: str) -> tuple[tuple[int, ...], int] | None:
    """Ordering key for a release tag like ``v1.2.0-beta``.

    Returns ``(numbers, is_final)`` where any pre-release suffix makes
    ``is_final`` 0, so ``1.0.0`` sorts above ``1.0.0-beta``. Numbers are
    padded to three parts so ``1.1`` and ``1.1.0`` compare equal.
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
    return nums, 0 if suffix else 1


def is_newer_version(remote: str, current: str = __version__) -> bool:
    """True when ``remote`` is a strictly newer version than ``current``."""
    remote_key = _parse_version(remote)
    current_key = _parse_version(current)
    if remote_key is None or current_key is None:
        return False
    return remote_key > current_key


def fetch_latest_release() -> tuple[str, str] | None:
    """``(tag_name, html_url)`` of the latest GitHub release.

    Raises on network errors; returns None on a malformed response.
    """
    request = urllib.request.Request(
        LATEST_RELEASE_API_URL,
        headers={
            "User-Agent": "MinbarLive",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    tag = data.get("tag_name") if isinstance(data, dict) else None
    if not isinstance(tag, str) or not tag:
        return None
    url = data.get("html_url")
    if not isinstance(url, str) or not url:
        url = RELEASES_PAGE_URL
    return tag, url


def check_for_update() -> UpdateInfo | None:
    """Return UpdateInfo when a newer release exists, else None.

    Never raises — designed for a fire-and-forget background thread at
    startup.
    """
    try:
        fetched = fetch_latest_release()
        if fetched is None:
            return None
        tag, url = fetched
        if is_newer_version(tag):
            return UpdateInfo(version=_strip_v(tag), url=url)
        return None
    except Exception as exc:
        log(f"Update check skipped: {exc}", level="DEBUG")
        return None

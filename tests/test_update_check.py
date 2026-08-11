"""Tests for the startup update check (utils/update_check.py)."""

import io
import json
import sys
import urllib.request
from pathlib import Path

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import update_check
from utils.update_check import (
    UpdateInfo,
    check_for_update,
    fetch_latest_release,
    is_newer_version,
)


class TestIsNewerVersion:
    @pytest.mark.parametrize(
        ("remote", "current"),
        [
            ("1.0.1", "1.0.0"),
            ("1.1.0", "1.0.9"),
            ("2.0.0", "1.9.9"),
            ("v1.0.1", "1.0.0"),  # GitHub tags carry a leading v
            ("1.0.0", "1.0.0-beta"),  # final release beats its pre-release
            ("1.0.1-beta", "1.0.0"),  # newer numbers beat older final
            ("1.1", "1.0.9"),  # short tag pads to 1.1.0
            ("1.0.0-rc.2", "1.0.0-rc.1"),  # the next rc reaches an rc build
            ("1.0.0-rc.10", "1.0.0-rc.9"),  # suffix numbers compare as numbers
            ("1.0.0-rc.1", "1.0.0-beta"),  # beta -> rc is forward
            ("1.0.0-rc.1", "1.0.0-rc"),  # a numbered rc beats the bare one
        ],
    )
    def test_newer(self, remote, current):
        assert is_newer_version(remote, current) is True

    @pytest.mark.parametrize(
        ("remote", "current"),
        [
            ("1.0.0", "1.0.0"),
            ("1.0.0-beta", "1.0.0-beta"),
            ("1.0.0-beta", "1.0.0"),  # pre-release never beats its final
            ("0.9.9", "1.0.0"),
            ("1.0", "1.0.0"),  # padded equal
            ("not-a-version", "1.0.0"),
            ("", "1.0.0"),
            ("1.0.0", "garbage"),  # unparseable current -> never "newer"
            ("1.0.0-rc.1", "1.0.0-rc.2"),  # an rc never goes backwards
            ("1.0.0-rc.1", "1.0.0-rc.1"),
            ("1.0.0-beta", "1.0.0-rc.1"),
        ],
    )
    def test_not_newer(self, remote, current):
        assert is_newer_version(remote, current) is False

    def test_default_current_is_app_version(self):
        # The shipped __version__ must be parseable, otherwise the whole
        # check silently never fires.
        assert is_newer_version("999.0.0") is True


def _fake_urlopen(payload, monkeypatch):
    """Point urllib.request.urlopen at a canned HTTP response body."""

    class FakeResponse(io.BytesIO):
        def __init__(self):
            body = payload if isinstance(payload, bytes) else json.dumps(
                payload
            ).encode("utf-8")
            super().__init__(body)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    captured = {}

    def fake(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.headers)
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    return captured


class TestFetchLatestRelease:
    def test_returns_the_tag(self, monkeypatch):
        captured = _fake_urlopen(
            {
                "tag_name": "v9.9.9",
                "html_url": "https://github.com/MinbarLive/MinbarLive/releases/tag/v9.9.9",
            },
            monkeypatch,
        )
        assert fetch_latest_release() == "v9.9.9"
        assert captured["url"] == update_check.LATEST_RELEASE_API_URL
        # GitHub's API rejects requests without a User-Agent.
        assert any(k.lower() == "user-agent" for k in captured["headers"])

    def test_missing_tag_returns_none(self, monkeypatch):
        _fake_urlopen({"html_url": "https://example.com"}, monkeypatch)
        assert fetch_latest_release() is None

    @pytest.mark.parametrize(
        "hostile_url",
        [
            "javascript:alert(1)",
            "file:///C:/Windows/System32/calc.exe",
            "https://evil.example.com/releases/",
            "https://github.com.evil.test/MinbarLive/MinbarLive/releases/",
            "http://github.com/MinbarLive/MinbarLive/releases/tag/v9",  # not https
        ],
    )
    def test_the_response_can_never_choose_what_the_browser_opens(
        self, hostile_url, monkeypatch
    ):
        """The notice opens a browser at this URL. It used to be the release's
        own ``html_url``, validated against a github.com prefix; it is now the
        project's download page, a constant, so the body has no say at all."""
        _fake_urlopen({"tag_name": "v999.0.0", "html_url": hostile_url}, monkeypatch)
        assert check_for_update().url == update_check.DOWNLOAD_PAGE_URL


class TestCheckForUpdate:
    def test_newer_release_returns_info(self, monkeypatch):
        _fake_urlopen(
            {
                "tag_name": "v999.0.0",
                "html_url": "https://github.com/MinbarLive/MinbarLive/releases/tag/v999.0.0",
            },
            monkeypatch,
        )
        info = check_for_update()
        assert info == UpdateInfo(
            version="999.0.0", url=update_check.DOWNLOAD_PAGE_URL
        )

    def test_current_release_returns_none(self, monkeypatch):
        from version import __version__

        _fake_urlopen(
            {"tag_name": f"v{__version__}", "html_url": "https://example.com"},
            monkeypatch,
        )
        assert check_for_update() is None

    def test_network_error_is_silent(self, monkeypatch):
        def boom(*args, **kwargs):
            raise OSError("offline")

        monkeypatch.setattr(urllib.request, "urlopen", boom)
        assert check_for_update() is None  # must never raise

    def test_malformed_body_is_silent(self, monkeypatch):
        _fake_urlopen(b"<html>rate limited</html>", monkeypatch)
        assert check_for_update() is None


def _tag_url(tag: str) -> str:
    return f"https://github.com/MinbarLive/MinbarLive/releases/tag/{tag}"


class TestFetchPrereleases:
    """include_prereleases reads the full list, which does contain rcs."""

    def test_uses_release_list_endpoint(self, monkeypatch):
        captured = _fake_urlopen(
            [{"tag_name": "v1.0.0-rc.1", "html_url": _tag_url("v1.0.0-rc.1")}],
            monkeypatch,
        )
        assert fetch_latest_release(include_prereleases=True) == "v1.0.0-rc.1"
        assert captured["url"] == update_check.RELEASE_LIST_API_URL

    def test_highest_version_wins_not_first_entry(self, monkeypatch):
        # GitHub returns the list newest-published first, which is not the
        # same as newest version.
        _fake_urlopen(
            [
                {"tag_name": "v0.9.1", "html_url": _tag_url("v0.9.1")},
                {"tag_name": "v1.0.0-rc.2", "html_url": _tag_url("v1.0.0-rc.2")},
                {"tag_name": "v1.0.0-rc.1", "html_url": _tag_url("v1.0.0-rc.1")},
            ],
            monkeypatch,
        )
        assert fetch_latest_release(include_prereleases=True) == "v1.0.0-rc.2"

    def test_unparseable_tags_are_skipped(self, monkeypatch):
        _fake_urlopen(
            [
                {"tag_name": "nightly", "html_url": _tag_url("nightly")},
                {"tag_name": "v1.0.0-rc.1", "html_url": _tag_url("v1.0.0-rc.1")},
            ],
            monkeypatch,
        )
        assert fetch_latest_release(include_prereleases=True) == "v1.0.0-rc.1"

    def test_empty_list_returns_none(self, monkeypatch):
        _fake_urlopen([], monkeypatch)
        assert fetch_latest_release(include_prereleases=True) is None

    def test_non_list_body_returns_none(self, monkeypatch):
        _fake_urlopen({"tag_name": "v9.9.9"}, monkeypatch)
        assert fetch_latest_release(include_prereleases=True) is None


class TestPrereleaseOptIn:
    """The setting is the only thing that can expose an rc to a user."""

    def test_opted_out_never_sees_the_rc(self, monkeypatch):
        # What every normal user gets: /releases/latest, which excludes the
        # rc, so the answer is the release they already run.
        monkeypatch.setattr(update_check, "__version__", "1.0.0-beta")
        captured = _fake_urlopen(
            {"tag_name": "v1.0.0-beta", "html_url": _tag_url("v1.0.0-beta")},
            monkeypatch,
        )
        assert check_for_update() is None
        assert captured["url"] == update_check.LATEST_RELEASE_API_URL

    def test_opted_in_sees_the_rc(self, monkeypatch):
        monkeypatch.setattr(update_check, "__version__", "1.0.0-beta")
        _fake_urlopen(
            [
                {"tag_name": "v1.0.0-rc.1", "html_url": _tag_url("v1.0.0-rc.1")},
                {"tag_name": "v1.0.0-beta", "html_url": _tag_url("v1.0.0-beta")},
            ],
            monkeypatch,
        )
        assert check_for_update(include_prereleases=True) == UpdateInfo(
            version="1.0.0-rc.1", url=update_check.DOWNLOAD_PAGE_URL
        )

    def test_opted_in_still_prefers_the_final(self, monkeypatch):
        # An rc tester must not be parked on the rc once 1.0.0 ships.
        monkeypatch.setattr(update_check, "__version__", "1.0.0-rc.1")
        _fake_urlopen(
            [
                {"tag_name": "v1.0.0", "html_url": _tag_url("v1.0.0")},
                {"tag_name": "v1.0.0-rc.1", "html_url": _tag_url("v1.0.0-rc.1")},
            ],
            monkeypatch,
        )
        assert check_for_update(include_prereleases=True) == UpdateInfo(
            version="1.0.0", url=update_check.DOWNLOAD_PAGE_URL
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

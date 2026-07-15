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
    def test_returns_tag_and_url(self, monkeypatch):
        captured = _fake_urlopen(
            {
                "tag_name": "v9.9.9",
                "html_url": "https://github.com/MinbarLive/MinbarLive/releases/tag/v9.9.9",
            },
            monkeypatch,
        )
        assert fetch_latest_release() == (
            "v9.9.9",
            "https://github.com/MinbarLive/MinbarLive/releases/tag/v9.9.9",
        )
        assert captured["url"] == update_check.LATEST_RELEASE_API_URL
        # GitHub's API rejects requests without a User-Agent.
        assert any(k.lower() == "user-agent" for k in captured["headers"])

    def test_missing_tag_returns_none(self, monkeypatch):
        _fake_urlopen({"html_url": "https://example.com"}, monkeypatch)
        assert fetch_latest_release() is None

    def test_missing_url_falls_back_to_releases_page(self, monkeypatch):
        _fake_urlopen({"tag_name": "v9.9.9"}, monkeypatch)
        assert fetch_latest_release() == ("v9.9.9", update_check.RELEASES_PAGE_URL)


class TestCheckForUpdate:
    def test_newer_release_returns_info(self, monkeypatch):
        _fake_urlopen(
            {"tag_name": "v999.0.0", "html_url": "https://example.com/rel"},
            monkeypatch,
        )
        info = check_for_update()
        assert info == UpdateInfo(version="999.0.0", url="https://example.com/rel")

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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

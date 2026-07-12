"""Tests for localized subtitle status messages."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils import user_messages
from utils.settings import TARGET_LANGUAGES


class TestStatusMessageCoverage:
    """The status_messages.json file must cover every target language."""

    def test_expected_keys_present(self):
        messages = user_messages._load_messages()
        assert set(messages) >= {"connection_error", "translation_unavailable"}

    def test_all_target_languages_covered(self):
        messages = user_messages._load_messages()
        for key, translations in messages.items():
            for name, _code in TARGET_LANGUAGES:
                assert name in translations, f"'{key}' missing language '{name}'"
                assert translations[name].strip(), f"'{key}' empty for '{name}'"


class TestGetUserMessage:
    """Tests for message lookup and fallback behavior."""

    def _patch_target_language(self, monkeypatch, language: str):
        monkeypatch.setattr(
            user_messages,
            "load_settings",
            lambda: SimpleNamespace(target_language=language),
        )

    def test_returns_target_language_message(self, monkeypatch):
        self._patch_target_language(monkeypatch, "German")
        assert user_messages.get_user_message("connection_error") == (
            "[⚠️ Verbindungsfehler]"
        )

    def test_unknown_language_falls_back_to_english(self, monkeypatch):
        self._patch_target_language(monkeypatch, "Klingon")
        assert user_messages.get_user_message("connection_error") == (
            "[⚠️ Connection error]"
        )

    def test_unknown_key_returns_key(self, monkeypatch):
        self._patch_target_language(monkeypatch, "German")
        assert user_messages.get_user_message("nonexistent_key") == "nonexistent_key"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

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
        assert set(messages) >= {
            "connection_error",
            "translation_unavailable",
            "invalid_api_key",
            "api_credits_exhausted",
            "app_stopped",
        }

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


class TestClassifyError:
    """Exception → audience-facing message key, across provider SDK shapes."""

    def test_openai_invalid_key(self):
        exc = Exception(
            "Error code: 401 - {'error': {'message': 'Incorrect API key "
            "provided: sk-proj-***'}}"
        )
        assert user_messages.classify_error(exc) == "invalid_api_key"

    def test_gemini_invalid_key_is_a_400(self):
        exc = Exception(
            "400 INVALID_ARGUMENT. API key not valid. Please pass a valid API key."
        )
        assert user_messages.classify_error(exc) == "invalid_api_key"

    def test_authentication_error_type_name(self):
        AuthenticationError = type("AuthenticationError", (Exception,), {})
        assert (
            user_messages.classify_error(AuthenticationError("bad request"))
            == "invalid_api_key"
        )

    def test_openai_insufficient_quota(self):
        exc = Exception(
            "Error code: 429 - insufficient_quota: You exceeded your current "
            "quota, please check your plan and billing details."
        )
        assert user_messages.classify_error(exc) == "api_credits_exhausted"

    def test_anthropic_low_credit_is_a_400(self):
        exc = Exception("Your credit balance is too low to access the Anthropic API.")
        assert user_messages.classify_error(exc) == "api_credits_exhausted"

    def test_gemini_resource_exhausted(self):
        exc = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded.")
        assert user_messages.classify_error(exc) == "api_credits_exhausted"

    def test_rate_limit_error_type_name(self):
        RateLimitError = type("RateLimitError", (Exception,), {})
        assert (
            user_messages.classify_error(RateLimitError("slow down"))
            == "api_credits_exhausted"
        )

    def test_network_errors_stay_generic(self):
        assert (
            user_messages.classify_error(ConnectionError("connection refused"))
            == "connection_error"
        )
        assert (
            user_messages.classify_error(TimeoutError("timed out"))
            == "connection_error"
        )
        assert (
            user_messages.classify_error(Exception("HTTP 500 server error"))
            == "connection_error"
        )

    def test_status_code_needs_word_boundary(self):
        # A request id containing the digits must not classify as auth error
        assert (
            user_messages.classify_error(Exception("failed (req_401abc)"))
            == "connection_error"
        )

    def test_bare_403_is_not_misclassified_as_invalid_key(self):
        assert (
            user_messages.classify_error(Exception("HTTP 403 model access denied"))
            == "connection_error"
        )

    def test_403_with_explicit_invalid_key_message_is_still_auth(self):
        assert (
            user_messages.classify_error(Exception("HTTP 403 invalid API key"))
            == "invalid_api_key"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

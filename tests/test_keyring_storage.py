"""Tests for secure API key storage."""

import pytest
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.keyring_storage import (
    SERVICE_NAME,
    _username_for,
    is_keyring_available,
    get_api_key_from_keyring,
    set_api_key_in_keyring,
    delete_api_key_from_keyring,
)


class TestKeyringStorageConstants:
    """Tests for keyring storage constants."""

    def test_service_name_is_string(self):
        """Service name should be a non-empty string."""
        assert isinstance(SERVICE_NAME, str)
        assert len(SERVICE_NAME) > 0

    def test_openai_entry_name_is_stable(self):
        """The OpenAI entry name predates multi-provider support and must
        never change, or existing stored keys stop being found."""
        assert _username_for("openai") == "openai_api_key"


class TestIsKeyringAvailable:
    """Tests for keyring availability check."""

    def test_returns_bool(self):
        """Should return a boolean."""
        result = is_keyring_available()
        assert isinstance(result, bool)

    def test_cached_result(self):
        """Should cache the result for performance."""
        # Call twice and verify same result (caching)
        result1 = is_keyring_available()
        result2 = is_keyring_available()
        assert result1 == result2


class TestKeyringFunctionsWithActualBackend:
    """Tests for keyring functions using actual backend (if available)."""

    def test_get_api_key_returns_string_or_none(self):
        """get_api_key_from_keyring should return str or None."""
        result = get_api_key_from_keyring()
        assert result is None or isinstance(result, str)

    def test_set_api_key_returns_bool(self):
        """set_api_key_in_keyring should return bool."""
        # Don't actually set a key in tests to avoid polluting keychain
        # Just verify the function signature works when keyring unavailable
        with patch(
            "utils.keyring_storage._check_keyring_available", return_value=False
        ):
            result = set_api_key_in_keyring("sk-test-key")
        assert isinstance(result, bool)
        assert result is False  # Should fail when unavailable

    def test_delete_api_key_returns_bool(self):
        """delete_api_key_from_keyring should return bool."""
        with patch(
            "utils.keyring_storage._check_keyring_available", return_value=False
        ):
            result = delete_api_key_from_keyring()
        assert isinstance(result, bool)


class TestKeyringUnavailable:
    """Tests for when keyring is unavailable."""

    @patch("utils.keyring_storage._check_keyring_available", return_value=False)
    def test_get_returns_none_when_unavailable(self, mock_check):
        """Should return None when keyring unavailable."""
        result = get_api_key_from_keyring()
        assert result is None

    @patch("utils.keyring_storage._check_keyring_available", return_value=False)
    def test_set_returns_false_when_unavailable(self, mock_check):
        """Should return False when keyring unavailable."""
        result = set_api_key_in_keyring("sk-test-key")
        assert result is False

    @patch("utils.keyring_storage._check_keyring_available", return_value=False)
    def test_delete_returns_false_when_unavailable(self, mock_check):
        """Should return False when keyring unavailable."""
        result = delete_api_key_from_keyring()
        assert result is False

"""Secure API key storage using the OS keychain.

Uses the `keyring` library to store API keys securely:
- Windows: Windows Credential Manager
- macOS: Keychain
- Linux: Secret Service API (GNOME Keyring, KWallet, etc.)

Falls back to plaintext settings.json storage if keyring is unavailable,
with a warning to the user.
"""

from __future__ import annotations

from utils.logging import log

# Service name for keyring storage
SERVICE_NAME = "MinbarLive"

# Each AI provider gets its own keychain entry: "<provider>_api_key".
# "openai" produces the same entry name as before multi-provider support,
# so existing stored keys keep working without migration.
DEFAULT_PROVIDER = "openai"


def _username_for(provider: str) -> str:
    """Keychain username for a provider's API key entry."""
    return f"{provider}_api_key"

# Track whether keyring is available
_keyring_available: bool | None = None


def _check_keyring_available() -> bool:
    """Check if keyring is available and working."""
    global _keyring_available

    if _keyring_available is not None:
        return _keyring_available

    try:
        import keyring
        from keyring.errors import KeyringError, NoKeyringError

        # Test if a backend is actually available
        try:
            # Try to get a non-existent key to test the backend
            keyring.get_password(SERVICE_NAME, "__test__")
            _keyring_available = True
            log("Keyring backend available for secure storage.", level="DEBUG")
        except NoKeyringError:
            _keyring_available = False
            log(
                "No keyring backend available. API key will be stored in settings file.",
                level="WARNING",
            )
        except KeyringError as e:
            _keyring_available = False
            log(f"Keyring error: {e}. Falling back to settings file.", level="WARNING")
        except Exception as e:
            _keyring_available = False
            log(
                f"Keyring check failed: {e}. Falling back to settings file.",
                level="WARNING",
            )

    except ImportError:
        _keyring_available = False
        log(
            "keyring library not installed. API key will be stored in settings file.",
            level="WARNING",
        )

    return _keyring_available


def is_keyring_available() -> bool:
    """Check if secure keyring storage is available."""
    return _check_keyring_available()


def get_api_key_from_keyring(provider: str = DEFAULT_PROVIDER) -> str | None:
    """
    Retrieve a provider's API key from the OS keychain.

    Returns:
        The API key if found, None otherwise.
    """
    if not _check_keyring_available():
        return None

    try:
        import keyring

        key = keyring.get_password(SERVICE_NAME, _username_for(provider))
        if key:
            log("API key retrieved from secure storage.", level="DEBUG")
        return key
    except Exception as e:
        log(f"Failed to retrieve API key from keyring: {e}", level="ERROR")
        return None


def set_api_key_in_keyring(api_key: str, provider: str = DEFAULT_PROVIDER) -> bool:
    """
    Store a provider's API key in the OS keychain.

    Args:
        api_key: The API key to store.
        provider: The AI provider this key belongs to.

    Returns:
        True if successfully stored, False otherwise.
    """
    if not _check_keyring_available():
        return False

    try:
        import keyring

        keyring.set_password(SERVICE_NAME, _username_for(provider), api_key)
        log("API key stored in secure storage.", level="INFO")
        return True
    except Exception as e:
        log(f"Failed to store API key in keyring: {e}", level="ERROR")
        return False


def delete_api_key_from_keyring(provider: str = DEFAULT_PROVIDER) -> bool:
    """
    Delete a provider's API key from the OS keychain.

    Returns:
        True if successfully deleted (or didn't exist), False on error.
    """
    if not _check_keyring_available():
        return False

    try:
        import keyring
        from keyring.errors import PasswordDeleteError

        try:
            keyring.delete_password(SERVICE_NAME, _username_for(provider))
            log("API key deleted from secure storage.", level="INFO")
        except PasswordDeleteError:
            # Key didn't exist, that's fine
            pass
        return True
    except Exception as e:
        log(f"Failed to delete API key from keyring: {e}", level="ERROR")
        return False

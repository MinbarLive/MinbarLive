"""Anthropic client singleton with runtime-configurable API key.

The anthropic SDK is imported lazily so that users of other providers pay no
startup cost and the app keeps working when the package is unavailable —
Claude calls are the only thing that would fail.
"""

from __future__ import annotations

import os

_client = None  # anthropic.Anthropic, created lazily
_api_key: str | None = None


def set_api_key(api_key: str | None) -> None:
    """Set the API key and reset the client instance."""
    global _client, _api_key
    _api_key = (api_key or "").strip() or None
    _client = None


def has_api_key() -> bool:
    return bool((_api_key or "").strip())


def _load_stored_key() -> str | None:
    """Look up an Anthropic key from the OS keychain, then environment."""
    from utils.keyring_storage import get_api_key_from_keyring

    return (
        get_api_key_from_keyring("anthropic")
        or (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        or None
    )


def get_client():
    """Get (or create) an Anthropic client for the current API key.

    If no key was set explicitly, tries the OS keychain ("anthropic" entry)
    and the ANTHROPIC_API_KEY environment variable.
    """
    global _client
    if _client is None:
        if not has_api_key():
            set_api_key(_load_stored_key())
        if not has_api_key():
            raise RuntimeError("Anthropic API key is not configured.")

        import anthropic

        _client = anthropic.Anthropic(api_key=_api_key)
    return _client

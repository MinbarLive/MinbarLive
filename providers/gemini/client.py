"""Gemini client singleton with runtime-configurable API key.

The google-genai SDK is imported lazily so that OpenAI-only users pay no
startup cost and the app keeps working when the package is unavailable —
Gemini calls are the only thing that would fail.
"""

from __future__ import annotations

import os

_client = None  # google.genai.Client, created lazily
_live_client = None  # separate client pinned to v1alpha for the Live API
_api_key: str | None = None


def set_api_key(api_key: str | None) -> None:
    """Set the API key and reset the client instances."""
    global _client, _live_client, _api_key
    _api_key = (api_key or "").strip() or None
    _client = None
    _live_client = None


def has_api_key() -> bool:
    return bool((_api_key or "").strip())


def _load_stored_key() -> str | None:
    """Look up a Gemini key from the OS keychain, then environment."""
    from utils.keyring_storage import get_api_key_from_keyring

    return (
        get_api_key_from_keyring("gemini")
        or (os.getenv("GEMINI_API_KEY") or "").strip()
        or (os.getenv("GOOGLE_API_KEY") or "").strip()
        or None
    )


def get_client():
    """Get (or create) a Gemini client for the current API key.

    If no key was set explicitly, tries the OS keychain ("gemini" entry) and
    the GEMINI_API_KEY / GOOGLE_API_KEY environment variables.
    """
    global _client
    if _client is None:
        if not has_api_key():
            set_api_key(_load_stored_key())
        if not has_api_key():
            raise RuntimeError("Gemini API key is not configured.")

        from google import genai

        _client = genai.Client(api_key=_api_key)
    return _client


def get_live_client():
    """Get (or create) a Gemini client for Live API sessions.

    Pinned to the v1alpha API version: the Live proactivity field (used to
    silence the model in transcription-only sessions) is not served on the
    default version (verified July 2026).
    """
    global _live_client
    if _live_client is None:
        if not has_api_key():
            set_api_key(_load_stored_key())
        if not has_api_key():
            raise RuntimeError("Gemini API key is not configured.")

        from google import genai
        from google.genai import types

        _live_client = genai.Client(
            api_key=_api_key,
            http_options=types.HttpOptions(api_version="v1alpha"),
        )
    return _live_client

"""OpenAI client singleton with runtime-configurable API key."""

from __future__ import annotations

from typing import Any

from openai import OpenAI

_client: OpenAI | None = None
_api_key: str | None = None


def set_api_key(api_key: str | None) -> None:
    """Set the API key and reset the client instance."""
    global _client, _api_key
    _api_key = (api_key or "").strip() or None
    _client = None


def has_api_key() -> bool:
    return bool((_api_key or "").strip())


def get_client() -> OpenAI:
    """Get (or create) an OpenAI client for the current API key."""
    global _client
    if _client is None:
        if not has_api_key():
            raise RuntimeError("OpenAI API key is not configured.")
        _client = OpenAI(api_key=_api_key)
    return _client


def _is_unsupported_parameter_error(exc: Exception, param_name: str) -> bool:
    """Return True if the API error indicates an unsupported request parameter."""
    msg = str(exc).lower()
    return "unsupported parameter" in msg and f"'{param_name.lower()}'" in msg


def create_chat_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    max_output_tokens: int | None = None,
    **kwargs: Any,
):
    """Create a chat completion with cross-model token parameter compatibility.

    Some models require ``max_completion_tokens`` while older ones may only support
    ``max_tokens``. This helper tries the newer parameter first and falls back when
    needed so callers can stay model-agnostic.
    """
    client = get_client()
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        **kwargs,
    }

    if max_output_tokens is None:
        return client.chat.completions.create(**payload)

    try:
        return client.chat.completions.create(
            **payload,
            max_completion_tokens=max_output_tokens,
        )
    except Exception as exc:
        if not _is_unsupported_parameter_error(exc, "max_completion_tokens"):
            raise

    return client.chat.completions.create(
        **payload,
        max_tokens=max_output_tokens,
    )

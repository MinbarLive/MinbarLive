"""Status messages shown on the subtitle window, localized to the target language.

These are audience-facing (e.g. connection errors during a live session), so
they follow the *target* language like the subtitles themselves — not the
GUI language of the control panel.
"""

from __future__ import annotations

import re
from functools import lru_cache

from config import STATUS_MESSAGES_PATH
from utils.json_helpers import load_json
from utils.settings import load_settings

# Last-resort fallbacks if the translations file is missing or incomplete
_DEFAULTS = {
    "connection_error": "[⚠️ Connection error]",
    "translation_unavailable": "[⚠️ Translation unavailable]",
    "invalid_api_key": "[⚠️ Invalid API key]",
    "api_credits_exhausted": "[⚠️ API credit used up]",
    "app_stopped": "[⏸️ Translation stopped]",
}

# Substring/status-code fingerprints of provider auth and quota errors.
# String matching over the exception type + text is deliberate: it covers
# every provider SDK (OpenAI/Gemini/Anthropic/Deepgram) without importing
# any of them. Checked against lowercased text.
_INVALID_KEY_PATTERNS = (
    "api key not valid",  # Gemini (a 400, not a 401)
    "api_key_invalid",
    "invalid api key",
    "incorrect api key",  # OpenAI
    "invalid x-api-key",  # Anthropic
    "authentication",  # AuthenticationError type names
    "unauthenticated",
    "unauthorized",
    "invalid credentials",  # Deepgram
    "permission denied",
    "permission_denied",
)
_NO_CREDIT_PATTERNS = (
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",  # Gemini free tier
    "credit balance",  # Anthropic (a 400, not a 429)
    "billing",
    "resource_exhausted",
    "resource exhausted",
    "quota",
    "rate limit",
    "ratelimit",  # RateLimitError type names
)
# HTTP 401 is an authentication failure.  A bare 403 is not: providers also
# use it for model/project permissions, regional restrictions and policy
# denials.  Those stay generic unless their message carries one of the
# explicit credential fingerprints above.
_INVALID_KEY_CODES = ("401",)
_NO_CREDIT_CODES = ("402", "429")


def classify_error(exc: BaseException) -> str:
    """Map a pipeline exception to the audience-facing status-message key.

    A persistent 429 after the retry/backoff chain is treated as exhausted
    quota — steady low-rate subtitle traffic doesn't sustain pure rate
    limits. Anything unrecognized stays the generic connection error.
    """
    text = f"{type(exc).__name__} {exc}".lower()

    def _has_code(codes: tuple[str, ...]) -> bool:
        return any(re.search(rf"\b{code}\b", text) for code in codes)

    if any(p in text for p in _INVALID_KEY_PATTERNS) or _has_code(_INVALID_KEY_CODES):
        return "invalid_api_key"
    if any(p in text for p in _NO_CREDIT_PATTERNS) or _has_code(_NO_CREDIT_CODES):
        return "api_credits_exhausted"
    return "connection_error"


@lru_cache(maxsize=1)
def _load_messages() -> dict:
    return load_json(STATUS_MESSAGES_PATH)


def get_user_message(key: str) -> str:
    """Get a subtitle-facing status message in the current target language.

    Args:
        key: Message key (e.g. "connection_error").

    Returns:
        Localized message; falls back to English, then to a built-in default.
    """
    translations = _load_messages().get(key) or {}
    target_language = load_settings().target_language
    return (
        translations.get(target_language)
        or translations.get("English")
        or _DEFAULTS.get(key, key)
    )

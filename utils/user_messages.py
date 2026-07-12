"""Status messages shown on the subtitle window, localized to the target language.

These are audience-facing (e.g. connection errors during a live session), so
they follow the *target* language like the subtitles themselves — not the
GUI language of the control panel.
"""

from __future__ import annotations

from functools import lru_cache

from config import STATUS_MESSAGES_PATH
from utils.json_helpers import load_json
from utils.settings import load_settings

# Last-resort fallbacks if the translations file is missing or incomplete
_DEFAULTS = {
    "connection_error": "[⚠️ Connection error]",
    "translation_unavailable": "[⚠️ Translation unavailable]",
}


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

"""
Dictionary-based matching for Quran and Athan phrases.

Automatically loads translation files based on target language:
- data/translations/quran/{lang_code}.json
- data/translations/athan/{lang_code}.json

For target=Arabic, returns the canonical Arabic phrase (key) from the dictionary.
For other targets, returns the translation if available, otherwise None.
"""

from __future__ import annotations

import os
import re
from difflib import SequenceMatcher
from functools import lru_cache

from config import ATHAN_TRANSLATIONS_DIR, QURAN_TRANSLATIONS_DIR
from utils.json_helpers import load_json
from utils.logging import log
from utils.settings import get_target_language_code, load_settings

# Type aliases for clarity
ArabicText = str
Translation = str


def _get_translation_file_path(translations_dir: str, lang_code: str) -> str | None:
    """Get the path to a translation file if it exists."""
    file_path = os.path.join(translations_dir, f"{lang_code}.json")
    if os.path.exists(file_path):
        return file_path
    return None


def _list_available_languages(translations_dir: str) -> list[str]:
    """List available language codes in a translations directory."""
    if not os.path.exists(translations_dir):
        return []
    return [
        f[:-5]  # Remove .json extension
        for f in os.listdir(translations_dir)
        if f.endswith(".json")
    ]


@lru_cache(maxsize=10)
def _load_dictionary(
    translations_dir: str, lang_code: str
) -> dict[ArabicText, Translation]:
    """
    Load a dictionary for a specific language, cached to avoid repeated disk reads.

    Args:
        translations_dir: Path to the translations directory (quran or athan).
        lang_code: ISO language code (e.g., 'de', 'en', 'ar').

    Returns:
        Dictionary mapping Arabic text to translation, or empty dict if not found.
    """
    file_path = _get_translation_file_path(translations_dir, lang_code)
    if file_path is None:
        log(
            f"No translation file found for '{lang_code}' in {translations_dir}",
            level="DEBUG",
        )
        return {}

    data = load_json(file_path)
    log(f"Loaded {len(data)} entries from {file_path}", level="DEBUG")
    return data


def get_quran_dict(lang_code: str | None = None) -> dict[ArabicText, Translation]:
    """
    Get the Quran dictionary for a specific language.

    Args:
        lang_code: ISO language code. If None, uses current target language from settings.

    Returns:
        Dictionary mapping Arabic verses to translations.
    """
    if lang_code is None:
        lang_code = get_target_language_code(load_settings().target_language) or "de"
    return _load_dictionary(QURAN_TRANSLATIONS_DIR, lang_code)


def get_athan_dict(lang_code: str | None = None) -> dict[ArabicText, Translation]:
    """
    Get the Athan dictionary for a specific language.

    Args:
        lang_code: ISO language code. If None, uses current target language from settings.

    Returns:
        Dictionary mapping Arabic Athan phrases to translations.
    """
    if lang_code is None:
        lang_code = get_target_language_code(load_settings().target_language) or "de"
    return _load_dictionary(ATHAN_TRANSLATIONS_DIR, lang_code)


def get_available_quran_languages() -> list[str]:
    """Get list of available language codes for Quran translations."""
    return _list_available_languages(QURAN_TRANSLATIONS_DIR)


def get_available_athan_languages() -> list[str]:
    """Get list of available language codes for Athan translations."""
    return _list_available_languages(ATHAN_TRANSLATIONS_DIR)


def has_quran_translation(lang_code: str) -> bool:
    """Check if a Quran translation exists for the given language."""
    return _get_translation_file_path(QURAN_TRANSLATIONS_DIR, lang_code) is not None


def has_athan_translation(lang_code: str) -> bool:
    """Check if an Athan translation exists for the given language."""
    return _get_translation_file_path(ATHAN_TRANSLATIONS_DIR, lang_code) is not None


# For backward compatibility: load default dictionaries (German)
# These are used by rag.py for embeddings (Arabic keys are language-agnostic)
quran_dict: dict[ArabicText, Translation] = get_quran_dict("de")
athan_dict: dict[ArabicText, Translation] = get_athan_dict("de")

log(f"Available Quran languages: {get_available_quran_languages()}", level="INFO")
log(f"Available Athan languages: {get_available_athan_languages()}", level="INFO")


def normalize_arabic(text: str) -> str:
    """
    Normalize Arabic text for matching:
    - Remove harakat (diacritics)
    - Normalize alef variants
    - Normalize taa marbuta
    - Normalize whitespace
    """
    text = text.strip()

    # Remove harakat
    harakat = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
    text = harakat.sub("", text)

    # Normalize Alef
    text = text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")

    # Normalize taa marbuta
    text = text.replace("ة", "ه")

    # Normalize spaces
    text = re.sub(r"\s+", " ", text)

    return text


def fuzzy_match_athan(
    text: str, target_lang_code: str | None = None
) -> tuple[float, str | None, str | None]:
    """
    Find the best fuzzy match for text in the Athan dictionary.

    Args:
        text: Arabic text to match against the Athan dictionary.
        target_lang_code: Target language code. If None, uses current settings.
                         For 'ar', returns the canonical Arabic phrase.

    Returns:
        Tuple of (score, translation, arabic_phrase).
        Score is 0.0-1.0, higher is better.
        Translation and phrase are None if dictionary is empty.
        For Arabic target, translation == arabic_phrase (canonical form).
    """
    if target_lang_code is None:
        target_lang_code = (
            get_target_language_code(load_settings().target_language) or "de"
        )

    # Use the pre-loaded German dictionary for Arabic keys (language-agnostic matching)
    athan_keys_dict = athan_dict

    text_norm = normalize_arabic(text)
    best_score: float = 0.0
    best_ar: str | None = None

    # Find best matching Arabic phrase
    for ar in athan_keys_dict.keys():
        ar_norm = normalize_arabic(ar)
        score = SequenceMatcher(None, ar_norm, text_norm).ratio()

        if score > best_score:
            best_score = score
            best_ar = ar

    if best_ar is None:
        return 0.0, None, None

    # For Arabic target, return the canonical Arabic phrase
    if target_lang_code == "ar":
        return best_score, best_ar, best_ar

    # For other languages, get the translation if available
    target_dict = get_athan_dict(target_lang_code)
    if target_dict and best_ar in target_dict:
        return best_score, target_dict[best_ar], best_ar

    # Fallback: return German translation (for GPT to adapt)
    return best_score, athan_dict.get(best_ar), best_ar

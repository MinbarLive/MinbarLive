"""
Script to build the Quran dictionaries (Arabic → translations).

This fetches all 114 suras from public APIs and writes one language-specific
JSON file per requested translation. The Arabic is fetched once per sura and
shared by every language in the run.

APIs used:
- Arabic text (without diacritics): https://quranapi.pages.dev
- Translations: https://quranenc.com (keys: https://quranenc.com/api/v1/translations/list
  — some working keys, e.g. russian_rwwad, are served but not listed there)

Usage (from inside notebooks/):
    python build_quran_dict.py de=german_bubenheim
    python build_quran_dict.py fr=french_rashid es=spanish_garcia ...

Output:
    ../data/translations/quran/{code}.json for every code=key pair

The file name is the app's target-language code (``zh-hans``, not ``zh``).

Keys currently in use:
    de german_bubenheim · en english_hilali_khan · tr turkish_rwwad
    sq albanian_nahi · bs bosnian_rwwad · see data/translations/quran/SOURCES.md
    for the rest and why each was chosen.

Verify before shipping: a sura count that matches is NOT proof the text is
aligned. tamil_omar returns sura 111's text under sura 112's metadata, so a
shifted source passes every structural check and would show the wrong verse as
"verified". Spot-check a sample of verses against the Arabic.
"""

import json
import os
import re
import sys
import time

import requests

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------

OUTPUT_DIR = "../data/translations/quran"

# API endpoints
API_ARABIC = "https://quranapi.pages.dev/api/{sura}.json"
API_TRANSLATION = "https://quranenc.com/api/v1/translation/sura/{key}/{sura}"

# Rate limiting, to be respectful to the APIs
DELAY_BETWEEN_REQUESTS = 0.3  # seconds
RETRIES = 4

# Footnote markers ("[1]", Bengali "[১]") and a leading verse number ("1. ").
# Both are reading aids in a printed translation and noise in a subtitle.
_FOOTNOTE = re.compile(r"\s*\[\d+\]")
_LEADING_NUMBER = re.compile(r"^\d+\.\s+")


# -------------------------------------------------------------------
# API FUNCTIONS
# -------------------------------------------------------------------


def _get_json(url: str) -> dict:
    """GET with retries. Raises after the last attempt — never skips silently."""
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.get(url, timeout=30)
            resp.raise_for_status()
            time.sleep(DELAY_BETWEEN_REQUESTS)
            return resp.json()
        except requests.RequestException as e:
            if attempt == RETRIES:
                raise
            print(f"  {url}: {e} — retry {attempt}/{RETRIES - 1} in {5 * attempt}s")
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def get_arabic_ayahs(sura: int) -> list[str]:
    """Fetch Arabic verses (without diacritics) for a sura."""
    # 'arabic2' contains text without tashkil (diacritical marks)
    return _get_json(API_ARABIC.format(sura=sura))["arabic2"]


def get_translations(key: str, sura: int) -> list[str]:
    """Fetch one translation of a sura, cleaned for display."""
    result = _get_json(API_TRANSLATION.format(key=key, sura=sura))["result"]
    return [clean(item["translation"]) for item in result]


def clean(text: str) -> str:
    text = _FOOTNOTE.sub("", text)
    text = _LEADING_NUMBER.sub("", text.strip())
    return " ".join(text.split())


# -------------------------------------------------------------------
# MAIN BUILD FUNCTION
# -------------------------------------------------------------------


def build_quran_mappings(keys: dict[str, str]) -> dict[str, dict[str, str]]:
    """
    Build one mapping per language: Arabic verse (no diacritics) → translation
    with its reference, e.g. "Translation text (2:255)".
    """
    mappings: dict[str, dict[str, str]] = {code: {} for code in keys}

    for sura in range(1, 115):  # 114 suras
        arabic_verses = get_arabic_ayahs(sura)
        for code, key in keys.items():
            translated = get_translations(key, sura)
            if len(arabic_verses) != len(translated):
                raise SystemExit(
                    f"{code} ({key}): sura {sura} has {len(arabic_verses)} Arabic "
                    f"vs {len(translated)} translated verses — not writing it."
                )
            for verse_number, (arabic, text) in enumerate(
                zip(arabic_verses, translated, strict=True), start=1
            ):
                if not text:
                    raise SystemExit(f"{code} ({key}): empty {sura}:{verse_number}")
                # Skip duplicates (some verses repeat across suras)
                if arabic not in mappings[code]:
                    mappings[code][arabic] = f"{text} ({sura}:{verse_number})"
        print(
            f"Sura {sura}/114 done ({len(arabic_verses)} verses, {len(keys)} languages)"
        )

    return mappings


def main():
    pairs = [arg.split("=", 1) for arg in sys.argv[1:]]
    if not pairs or any(len(p) != 2 or not all(p) for p in pairs):
        raise SystemExit("usage: python build_quran_dict.py code=quranenc_key ...")
    keys = dict(pairs)

    print(f"Building Quran dictionaries for {', '.join(keys)}")
    print("-" * 50)

    mappings = build_quran_mappings(keys)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for code, mapping in mappings.items():
        path = os.path.join(OUTPUT_DIR, f"{code}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        print(f"Created {path} with {len(mapping)} verses.")


if __name__ == "__main__":
    main()

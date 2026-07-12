"""
Script to build the Quran dictionary (Arabic → translations).

This fetches all 114 suras from public APIs and creates language-specific JSON files.
Run this if you need to rebuild the dictionary or adapt it for other languages.

APIs used:
- Arabic text (without diacritics): https://quranapi.pages.dev
- German translation (Bubenheim & Elyas): https://quranenc.com

Usage:
    python build_quran_dict.py

Output:
    ../data/translations/quran/de.json  (German)
    To add other languages, modify the API endpoint and output path.

Current translation keys already used:
    german_bubenheim, english_hilali_khan, turkish_rwwad, albanian_nahi, bosnian_rwwad
"""

import requests
import json
import time
import os

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------

language = "en"
translation_key = "english_hilali_khan"

# Output path for German translations (change for other languages)
OUTPUT_PATH = "../data/translations/quran/" + language + ".json"

# API endpoints
API_ARABIC = "https://quranapi.pages.dev/api/{sura}.json"
API_TRANSLATION = (
    "https://quranenc.com/api/v1/translation/sura/" + translation_key + "/{sura}"
)

# Rate limiting
DELAY_BETWEEN_SURAS = 1  # seconds


# -------------------------------------------------------------------
# API FUNCTIONS
# -------------------------------------------------------------------


def get_arabic_ayahs(sura: int) -> list[str]:
    """Fetch Arabic verses (without diacritics) for a sura."""
    url = API_ARABIC.format(sura=sura)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    # 'arabic2' contains text without tashkil (diacritical marks)
    return data["arabic2"]


def get_german_translations(sura: int) -> list[str]:
    """Fetch German translations (Bubenheim & Elyas) for a sura."""
    url = API_TRANSLATION.format(sura=sura)
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return [item["translation"] for item in data["result"]]


# -------------------------------------------------------------------
# MAIN BUILD FUNCTION
# -------------------------------------------------------------------


def build_quran_mapping() -> dict[str, str]:
    """
    Build a dictionary mapping Arabic verses to German translations.

    Returns:
        Dict with Arabic verse (no diacritics) as key,
        German translation with reference (e.g., "Translation text (2:255)") as value.
    """
    mapping: dict[str, str] = {}

    for sura in range(1, 115):  # 114 suras
        try:
            arabic_verses = get_arabic_ayahs(sura)
            german_verses = get_german_translations(sura)

            if len(arabic_verses) != len(german_verses):
                print(
                    f"Warning: Sura {sura} has {len(arabic_verses)} Arabic vs "
                    f"{len(german_verses)} German verses."
                )

            for verse_number, (arabic, german) in enumerate(
                zip(arabic_verses, german_verses), start=1
            ):
                # Skip duplicates (some verses repeat across suras)
                if arabic in mapping:
                    continue

                # Add verse reference to translation
                german_with_ref = f"{german} ({sura}:{verse_number})"
                mapping[arabic] = german_with_ref

            print(f"Sura {sura}/114 done ({len(arabic_verses)} verses)")

        except requests.RequestException as e:
            print(f"Error fetching sura {sura}: {e}")
            print("Waiting 5 seconds before retry...")
            time.sleep(5)
            continue

        # Rate limiting to be respectful to the APIs
        time.sleep(DELAY_BETWEEN_SURAS)

    return mapping


def main():
    print("Building Quran dictionary...")
    print(f"Output: {OUTPUT_PATH}")
    print("-" * 50)

    quran_map = build_quran_mapping()

    # Ensure output directory exists
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    # Save to JSON
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(quran_map, f, ensure_ascii=False, indent=2)

    print("-" * 50)
    print(f"Done! Created {OUTPUT_PATH} with {len(quran_map)} verses.")


if __name__ == "__main__":
    main()

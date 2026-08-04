"""GUI string loading — shared by both GUI trees.

Moved out of ``gui/app_gui.py`` (which imports Tk) so the Qt tree can load the
same ``data/translations/gui/*.json`` files without dragging CustomTkinter in.
Imports no GUI toolkit.

English is always the base layer: a translation file that is missing a key
falls back to the English string rather than showing a raw key.
"""

import os

from config import GUI_TRANSLATIONS_DIR
from utils.json_helpers import load_json
from utils.settings import GUI_LANGUAGE_CODES


def load_gui_translations(language: str) -> dict:
    en_path = os.path.join(GUI_TRANSLATIONS_DIR, "en.json")
    try:
        base = load_json(en_path)
    except Exception:
        base = {}

    if language == "en" or language not in GUI_LANGUAGE_CODES:
        return base

    path = os.path.join(GUI_TRANSLATIONS_DIR, f"{language}.json")
    try:
        return {**base, **load_json(path)}
    except Exception:
        return base

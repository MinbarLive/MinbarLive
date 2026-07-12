"""JSON file utilities for safe reading."""

from __future__ import annotations

import json
import os
from typing import Any

from utils.logging import log


def load_json(path: str, default: Any = None) -> dict | list:
    """
    Load a JSON file and return its contents.

    Args:
        path: Path to the JSON file.
        default: Default value if file doesn't exist or is invalid.
                 Defaults to empty dict.

    Returns:
        Parsed JSON content, or default value on error.
    """
    if default is None:
        default = {}

    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content:
                log(f"JSON file is empty: {path}", level="WARNING")
                return default
            return json.loads(content)
    except json.JSONDecodeError as e:
        log(f"Invalid JSON in {path}: {e}", level="ERROR")
        return default
    except PermissionError as e:
        log(f"Permission denied reading {path}: {e}", level="ERROR")
        return default
    except Exception as e:
        log(f"Error reading JSON file {path}: {e}", level="ERROR")
        return default

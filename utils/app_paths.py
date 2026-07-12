"""App data paths for per-user persistent storage."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_app_data_dir(app_name: str = "MinbarLive") -> Path:
    """Return a per-user writable directory for app data.

    Uses platform-appropriate locations:
    - Windows: %APPDATA%/MinbarLive
    - macOS: ~/Library/Application Support/MinbarLive
    - Linux: ~/.local/share/MinbarLive (XDG standard)
    """
    if sys.platform == "win32":
        # Windows: use APPDATA
        appdata = os.getenv("APPDATA")
        if appdata:
            return Path(appdata) / app_name
        # Fallback for Windows if APPDATA not set
        return Path.home() / "AppData" / "Roaming" / app_name

    elif sys.platform == "darwin":
        # macOS: use Application Support
        return Path.home() / "Library" / "Application Support" / app_name

    else:
        # Linux/Unix: use XDG_DATA_HOME or fallback to ~/.local/share
        xdg_data = os.getenv("XDG_DATA_HOME")
        if xdg_data:
            return Path(xdg_data) / app_name
        return Path.home() / ".local" / "share" / app_name

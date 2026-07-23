"""PyInstaller runtime hook: make the bundled PortAudio discoverable on Linux.

sounddevice locates PortAudio at import time via
``ctypes.util.find_library("portaudio")``, which on Linux only searches the
system library cache (ldconfig) — never ``LD_LIBRARY_PATH`` or the PyInstaller
onefile extraction directory. The spec bundles ``libportaudio.so.2`` into that
directory, but find_library cannot see it, so on a machine without the system
``libportaudio2`` package sounddevice raises ``OSError: PortAudio library not
found`` and the app cannot start.

This hook runs before any application module (and before sounddevice binds
``find_library``), so pointing find_library at the bundled copy here makes the
Linux binary self-contained. Windows and macOS are untouched — their
sounddevice wheels ship PortAudio inside the package.
"""

import glob
import os
import sys

if sys.platform.startswith("linux") and hasattr(sys, "_MEIPASS"):
    import ctypes.util

    _orig_find_library = ctypes.util.find_library

    def _find_library(name):
        if name == "portaudio":
            base = sys._MEIPASS
            for candidate in ("libportaudio.so.2", "libportaudio.so"):
                path = os.path.join(base, candidate)
                if os.path.exists(path):
                    return path
            matches = glob.glob(os.path.join(base, "libportaudio.so*"))
            if matches:
                return matches[0]
        return _orig_find_library(name)

    ctypes.util.find_library = _find_library

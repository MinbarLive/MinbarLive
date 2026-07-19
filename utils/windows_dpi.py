"""Early Windows DPI-awareness bootstrap.

This module must stay free of Tk/CustomTkinter imports.  Windows only accepts
process DPI awareness reliably before the first HWND is created; importing a
GUI toolkit here would make the call too late.
"""

from __future__ import annotations

import ctypes
import sys

# CustomTkinter 5.2.2 deliberately uses Per-Monitor v1.  Tk handles the v2
# non-client resize notification itself while CustomTkinter simultaneously
# recalculates every widget, which makes a window jump/grow while it is dragged
# between monitors.  PMv1 keeps coordinates physical without that duplicate
# title-bar resize path.
_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE = ctypes.c_void_p(-3)
_PROCESS_PER_MONITOR_DPI_AWARE = 2


def _thread_is_per_monitor_aware(user32: object) -> bool:
    """Return whether the calling thread is already per-monitor aware."""

    try:
        get_context = user32.GetThreadDpiAwarenessContext
        get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        get_context.argtypes = []
        get_context.restype = ctypes.c_void_p
        get_awareness.argtypes = [ctypes.c_void_p]
        get_awareness.restype = ctypes.c_int
        return get_awareness(get_context()) == _PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        return False


def enable_windows_dpi_awareness() -> bool:
    """Enable the best DPI mode supported by the current Windows version.

    ``PerMonitor`` keeps ``screeninfo``, Tk and native ``SetWindowPos`` calls in
    the same physical coordinate space on mixed-DPI setups and matches the DPI
    mode supported by CustomTkinter 5.2.2.  The fallbacks cover older Windows
    versions.  The function is intentionally best-effort because tests and
    embedded launchers may have configured DPI awareness before importing the
    application.
    """

    if sys.platform != "win32":
        return False

    try:
        user32 = ctypes.windll.user32
    except (AttributeError, OSError):
        return False

    if _thread_is_per_monitor_aware(user32):
        return True

    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE):
            return True
        # ERROR_ACCESS_DENIED commonly means a manifest or host configured the
        # process already.  Treat an existing per-monitor context as success.
        if _thread_is_per_monitor_aware(user32):
            return True
    except (AttributeError, OSError):
        # SetProcessDpiAwarenessContext arrived with Windows 10.  On older
        # systems continue with the Windows 8.1 API below.
        pass

    try:
        shcore = ctypes.windll.shcore
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        if set_awareness(_PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return True
    except (AttributeError, OSError):
        pass

    try:
        set_legacy = user32.SetProcessDPIAware
        set_legacy.argtypes = []
        set_legacy.restype = ctypes.c_bool
        return bool(set_legacy())
    except (AttributeError, OSError):
        return False

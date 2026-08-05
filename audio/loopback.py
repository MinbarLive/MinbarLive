"""Registry for WASAPI loopback devices (populated by gui/device_list.py).

Synthetic negative device indices (-1, -2, …) are assigned to soundcard
Speaker objects so that the controller can look them up at stream-open time
without importing GUI code.

Also home to the guard that decides whether calling into soundcard at all is
safe on this machine — see soundcard_usable().
"""
from __future__ import annotations

import os
import sys
from typing import Any

# Maps synthetic negative index → soundcard Speaker object.
# Rebuilt on every get_input_devices() call.
_registry: dict[int, Any] = {}


def register(fake_idx: int, speaker: Any) -> None:
    _registry[fake_idx] = speaker


def clear() -> None:
    _registry.clear()


def get_speaker(device_idx: int) -> Any | None:
    """Return the soundcard Speaker for a loopback device index, or None."""
    return _registry.get(device_idx)


def _pulse_server_available() -> bool:
    """Linux only: whether a PulseAudio/PipeWire-pulse server can be reached.

    Checks the same places libpulse itself does, cheaply and without
    connecting: an explicit PULSE_SERVER, then the per-user socket that both
    PulseAudio and pipewire-pulse expose. Kept separate from
    soundcard_usable() so it is testable on any platform.
    """
    if os.environ.get("PULSE_SERVER"):
        return True
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir and os.path.exists(os.path.join(runtime_dir, "pulse", "native")):
        return True
    getuid = getattr(os, "getuid", None)  # absent on Windows
    if getuid is None:
        return False
    return os.path.exists(f"/run/user/{getuid()}/pulse/native")


def soundcard_usable() -> bool:
    """Whether calling into soundcard can be done without killing the process.

    On Linux soundcard talks to PulseAudio through libpulse, and its only
    guard against an unreachable server is an `assert` in the _PulseAudio
    constructor. MinbarLive.spec freezes with optimize=1 (`python -O`), which
    STRIPS asserts — so in the packaged app the failed connection goes
    unnoticed, and the first API call hands a NULL `pa_operation *` to
    pa_operation_unref(). libpulse answers with a C-level assertion and
    abort(). SIGABRT is not an exception: no `except` at any call site can
    catch it, and the whole app dies on launch.

    So look for a server BEFORE touching the library. A machine with no
    PulseAudio or PipeWire is a supported configuration — gui/device_list.py
    deliberately keeps showing raw devices on a pure-ALSA box — and it must
    degrade exactly the way a missing soundcard does, not crash.

    Reproducible only in a frozen build: from source the assert is live and
    raises catchably, which is why this survived every source run and only
    surfaced once the AppImage started bundling soundcard's cffi header.
    """
    if not sys.platform.startswith("linux"):
        return True
    return _pulse_server_available()

"""Registry for WASAPI loopback devices (populated by gui/device_list.py).

Synthetic negative device indices (-1, -2, …) are assigned to soundcard
Speaker objects so that the controller can look them up at stream-open time
without importing GUI code.
"""
from __future__ import annotations

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

"""Shared input-device enumeration for the control panel and the setup wizard."""

from __future__ import annotations

import sounddevice as sd

from config import FS

# Host API quality priority — lower value = better quality.
# Windows WDM-KS is intentionally excluded: it exposes devices using
# internal path-based identifiers (e.g. "Input (@System32\\driv...") that
# are not human-readable and cannot be reliably deduplicated against their
# WASAPI counterparts.  Every WDM-KS device is also available via WASAPI.
_HOSTAPI_PRIORITY = {
    "Windows WASAPI": 0,
    "Windows DirectSound": 1,
    "MME": 2,
}
_SKIP_HOSTAPIS = {"Windows WDM-KS"}


def get_input_devices() -> tuple[list[str], list[str], list[int]]:
    """Enumerate usable input devices.

    Returns:
        (display_names, base_names, device_indices) — display names are
        numbered for dropdowns; base names are the raw device names used for
        persistence. Raises nothing: on enumeration failure all lists are
        empty (callers decide how to surface that).
    """
    display_names: list[str] = []
    base_names: list[str] = []
    indices: list[int] = []
    try:
        devices = sd.query_devices()
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []

        # Collect all input-capable devices with quality metadata
        candidates: list[tuple[int, int, int, str]] = []  # (priority, idx, ch, name)
        for idx, device in enumerate(devices):
            ch = device.get("max_input_channels", 0)
            if ch <= 0:
                continue
            name = str(device.get("name", f"Device {idx}")).strip()
            hostapi_idx = device.get("hostapi", 0)
            try:
                hostapi_name = hostapis[hostapi_idx]["name"]
            except (IndexError, KeyError, TypeError):
                hostapi_name = ""
            if hostapi_name in _SKIP_HOSTAPIS:
                continue
            priority = _HOSTAPI_PRIORITY.get(hostapi_name, 99)
            candidates.append((priority, idx, ch, name))

        # Sort: best API first, then most channels
        candidates.sort(key=lambda c: (c[0], -c[2]))

        # Deduplicate: Windows MME truncates device names to ~31 chars, so the
        # same physical device can appear under multiple APIs with slightly
        # different name lengths.  Match by the shorter of the two name prefixes
        # when both are at least 20 characters.
        seen: list[str] = []
        for _priority, idx, _ch, name in candidates:
            is_dup = False
            for s in seen:
                ml = min(len(name), len(s))
                if ml >= 20 and name[:ml].lower() == s[:ml].lower():
                    is_dup = True
                    break
            if not is_dup:
                try:
                    sd.check_input_settings(device=idx, channels=1, samplerate=FS)
                except Exception:
                    continue
                seen.append(name)
                num = len(seen)
                display_names.append(f"{num}. {name}")
                base_names.append(name)
                indices.append(idx)
    except Exception:
        pass
    return display_names, base_names, indices

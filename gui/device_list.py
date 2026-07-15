"""Shared input-device enumeration for the control panel and the setup wizard."""

from __future__ import annotations

import sounddevice as sd

from audio.loopback import clear as _loopback_clear
from audio.loopback import register as _loopback_register
from config import FS

# Sample rates to try when validating a device.  Many devices (headsets,
# USB audio, speaker loopback) only advertise 44.1/48 kHz natively; Windows
# WASAPI resamples to FS at stream-open time, so any of these passing means
# the device is usable.
_CHECK_RATES = (FS,)


def _is_usable_input(device_idx: int) -> bool:
    """Return True if the device can be opened as a mono input at FS."""
    try:
        sd.check_input_settings(device=device_idx, channels=1, samplerate=FS)
        return True
    except Exception:
        return False


# Windows generic audio mapper entries that appear under MME/DirectSound as
# virtual aliases for the current default device — not real selectable
# hardware.  Filtered out by name prefix (case-insensitive).
_SKIP_NAME_PREFIXES = (
    "microsoft sound mapper",
    "primary sound capture driver",
    "primary sound driver",
)

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


def get_input_devices() -> tuple[list[str], list[str], list[int], list[bool]]:
    """Enumerate usable input devices.

    Returns:
        (display_names, base_names, device_indices, loopback_flags) — display
        names are numbered for dropdowns; base names are the raw device names
        used for persistence; loopback_flags is always all-False (loopback
        capture is not supported by sounddevice). Raises nothing: on
        enumeration failure all lists are empty.
    """
    display_names: list[str] = []
    base_names: list[str] = []
    indices: list[int] = []
    loopback_flags: list[bool] = []
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
            name_lower = name.lower()
            if any(name_lower.startswith(p) for p in _SKIP_NAME_PREFIXES):
                continue  # fake Windows audio mapper entry
            is_dup = False
            for s in seen:
                ml = min(len(name), len(s))
                if ml >= 20 and name[:ml].lower() == s[:ml].lower():
                    is_dup = True
                    break
            if not is_dup:
                if not _is_usable_input(idx):
                    continue
                seen.append(name)
                num = len(seen)
                display_names.append(f"{num}. {name}")
                base_names.append(name)
                indices.append(idx)
                loopback_flags.append(False)

        # Loopback devices: capture whatever is playing through an output
        # device (speakers, headphones) via WASAPI loopback.  Requires the
        # soundcard library; silently skipped if not installed.
        # Known soundcard limitation: recording a single channel on Windows
        # WASAPI produces garbage — the capture loops always use channels=2
        # and mix to mono.
        _loopback_clear()
        try:
            import soundcard as sc  # noqa: PLC0415 (lazy import is intentional)

            fake_idx = -1
            for speaker in sc.all_speakers():
                name = str(getattr(speaker, "name", "")).strip()
                if not name:
                    continue
                if any(name.lower().startswith(p) for p in _SKIP_NAME_PREFIXES):
                    continue
                if name in seen:
                    continue
                _loopback_register(fake_idx, speaker)
                seen.append(name)
                num = len(seen)
                display_names.append(f"{num}. {name} (Loopback)")
                base_names.append(f"{name} (Loopback)")
                indices.append(fake_idx)
                loopback_flags.append(True)
                fake_idx -= 1
        except ImportError:
            pass
        except Exception:
            pass

    except Exception:
        pass
    return display_names, base_names, indices, loopback_flags

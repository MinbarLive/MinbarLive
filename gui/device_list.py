"""Shared input-device enumeration for the control panel and the setup wizard."""

from __future__ import annotations

import sounddevice as sd

from audio.device_support import input_extra_settings
from audio.loopback import clear as _loopback_clear
from audio.loopback import register as _loopback_register
from config import FS


def _is_usable_input(device_idx: int, hostapi_name: str = "") -> bool:
    """Return True if the device can be opened as a mono input at FS."""
    try:
        kwargs = {}
        extra_settings = input_extra_settings(
            sd,
            device_index=device_idx,
            hostapi_name=hostapi_name,
        )
        if extra_settings is not None:
            kwargs["extra_settings"] = extra_settings
        sd.check_input_settings(
            device=device_idx,
            channels=1,
            samplerate=FS,
            **kwargs,
        )
        return True
    except Exception:
        return False


# Windows generic audio mapper entries that appear under MME/DirectSound as
# virtual aliases for the current default device — not real selectable
# hardware.  Filtered out by name prefix (case-insensitive).
_SKIP_NAME_PREFIXES = (
    "microsoft sound mapper",
    "microsoft soundmapper",
    "primary sound capture driver",
    "primary sound driver",
    "primärer soundaufnahmetreiber",
    "primärer soundtreiber",
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


def find_input_device_position(
    saved_name: str | None,
    base_names: list[str],
) -> int | None:
    """Resolve a persisted physical-device name after PortAudio reindexing."""

    if not saved_name:
        return None
    try:
        return base_names.index(saved_name)
    except ValueError:
        pass

    saved = " ".join(saved_name.casefold().split())
    for index, name in enumerate(base_names):
        candidate = " ".join(name.casefold().split())
        shorter = min(len(saved), len(candidate))
        if shorter >= 20 and saved[:shorter] == candidate[:shorter]:
            return index
    return None


def get_input_devices() -> tuple[list[str], list[str], list[int], list[bool]]:
    """Enumerate usable input devices.

    Lists sounddevice input devices first, then any WASAPI loopback outputs
    (see audio/loopback.py) so system audio can be captured like a mic.

    Returns:
        (display_names, base_names, device_indices, loopback_flags) — display
        names are numbered for dropdowns; base names are the raw device names
        used for persistence; loopback_flags marks the entries that are
        loopback captures rather than real inputs (their device index is the
        synthetic negative one registered in audio/loopback.py). Raises
        nothing: on enumeration failure all lists are empty.
    """
    display_names: list[str] = []
    base_names: list[str] = []
    indices: list[int] = []
    loopback_flags: list[bool] = []
    # Never leave stale synthetic loopback indices behind when PortAudio
    # enumeration itself fails.
    _loopback_clear()
    try:
        devices = sd.query_devices()
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = []

        # Collect all input-capable devices with quality metadata
        candidates: list[tuple[int, int, int, str, str]] = []
        # (priority, idx, channels, device name, host API name)
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
            candidates.append((priority, idx, ch, name, hostapi_name))

        # Sort: best API first, then most channels
        candidates.sort(key=lambda c: (c[0], -c[2]))

        # Deduplicate: Windows MME truncates device names to ~31 chars, so the
        # same physical device can appear under multiple APIs with slightly
        # different name lengths.  Match by the shorter of the two name prefixes
        # when both are at least 20 characters.
        seen: list[str] = []
        for _priority, idx, _ch, name, hostapi_name in candidates:
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
                if not _is_usable_input(idx, hostapi_name):
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

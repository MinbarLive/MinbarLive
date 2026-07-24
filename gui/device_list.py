"""Shared input-device enumeration for the control panel and the setup wizard."""

from __future__ import annotations

import sys

import sounddevice as sd

from audio.device_support import usable_input_samplerate
from audio.loopback import clear as _loopback_clear
from audio.loopback import register as _loopback_register
from config import FS


def _is_usable_input(device_idx: int, hostapi_name: str = "") -> bool:
    """Return True if the device can be opened as a mono input at some rate.

    Prefers the pipeline rate FS but accepts any rate the device supports: the
    capture loop opens the device at its native rate and resamples to FS
    (audio/resampler.py).  Without this, Linux/PipeWire named sources — which
    JACK exposes only at 48 kHz — would all be dropped.  The probe passes the
    same WASAPI extra settings the capture loops use, so a 48 kHz-only WASAPI
    endpoint is still recognised via the system mixer conversion."""
    return (
        usable_input_samplerate(
            sd,
            device_index=device_idx,
            requested=FS,
            channels=1,
            hostapi_name=hostapi_name,
        )
        is not None
    )


# Windows generic audio mapper entries that appear under MME/DirectSound as
# virtual aliases for the current default device — not real selectable
# hardware.  Filtered out by name prefix (case-insensitive).  Windows localizes
# these names, so the German variants are listed too (twin list:
# _GENERIC_INPUT_PREFIXES in audio/device_support.py).
_SKIP_NAME_PREFIXES = (
    "microsoft sound mapper",
    "microsoft soundmapper",
    "primary sound capture driver",
    "primary sound driver",
    "primärer soundaufnahmetreiber",
    "primärer soundtreiber",
)

# Generic PulseAudio/PipeWire routing endpoints to keep on Linux even though
# they are not physical microphones — "default" follows the system default
# source, "pipewire"/"pulse" go through the server (both resample). Matched by
# exact lowercased name.
_LINUX_GENERIC_INPUTS = frozenset({"default", "pipewire", "pulse"})


def _linux_real_source_names() -> set[str] | None:
    """Names of genuine PulseAudio/PipeWire capture sources, or None.

    On Linux the JACK host API also lists output monitors, application streams
    and duplicate raw endpoints as input-capable devices; PulseAudio knows
    which are real microphones.  Returns None when that cannot be determined
    (soundcard missing, no PulseAudio) so the caller does no filtering and a
    pure-ALSA machine still shows its raw devices."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        import soundcard as sc  # noqa: PLC0415 (lazy import is intentional)

        names = {
            str(m.name).strip()
            for m in sc.all_microphones(include_loopback=False)
            if str(getattr(m, "name", "")).strip()
        }
        return names or None
    except Exception:
        return None

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
    """Resolve a persisted device name against the current enumeration.

    PortAudio reindexes devices between runs and MME truncates names to ~31
    characters, so an exact match can fail for a device that is still present.
    Falls back to the same ≥20-character prefix rule get_input_devices() uses
    for deduplication.  Returns None when the device is really gone."""

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
    # Cleared before the first failure point: a failed enumeration must not
    # leave the previous run's synthetic loopback indices registered.
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

        # On Linux, keep only genuine capture sources (plus the generic routing
        # aliases); None means "couldn't tell" → keep everything as before.
        real_source_names = _linux_real_source_names()

        # Deduplicate: Windows MME truncates device names to ~31 chars, so the
        # same physical device can appear under multiple APIs with slightly
        # different name lengths.  Match by the shorter of the two name prefixes
        # when both are at least 20 characters.
        seen: list[str] = []
        for _priority, idx, _ch, name, hostapi_name in candidates:
            name_lower = name.lower()
            if any(name_lower.startswith(p) for p in _SKIP_NAME_PREFIXES):
                continue  # fake Windows audio mapper entry
            if (
                real_source_names is not None
                and name not in real_source_names
                and name_lower not in _LINUX_GENERIC_INPUTS
            ):
                continue  # JACK output monitor / app stream, not a real mic
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

            # Windows/WASAPI exposes each output as a loopback whose id matches
            # the speaker id, so speakers are enumerated directly.  On Linux the
            # loopback lives on a separate PulseAudio/PipeWire "Monitor of …"
            # source whose id carries a `.monitor` suffix; enumerate those so the
            # capture path's get_microphone(id, include_loopback=True) resolves
            # them (the speaker id alone raises "no soundcard with id …").
            if sys.platform.startswith("linux"):
                loopback_sources = [
                    m
                    for m in sc.all_microphones(include_loopback=True)
                    if getattr(m, "isloopback", False)
                ]
            else:
                loopback_sources = sc.all_speakers()

            fake_idx = -1
            for speaker in loopback_sources:
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

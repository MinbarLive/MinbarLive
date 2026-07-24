"""Host-API-specific settings for microphone capture.

Windows microphones commonly expose a 48 kHz shared-mode WASAPI endpoint
even though MinbarLive's pipelines consume 16 or 24 kHz PCM.  PortAudio only
offers those pipeline rates when WASAPI's system mixer conversion is enabled;
without it the otherwise preferred WASAPI endpoint is discarded and device
selection falls back to legacy DirectSound.
"""

from __future__ import annotations

from typing import Any

WASAPI_HOSTAPI_NAME = "Windows WASAPI"
_SKIP_HOSTAPIS = {"Windows WDM-KS"}
_HOSTAPI_FALLBACK_PRIORITY = {
    "Windows WASAPI": 0,
    "MME": 1,
    "Windows DirectSound": 2,
}
_GENERIC_INPUT_PREFIXES = (
    "microsoft sound mapper",
    "microsoft soundmapper",
    "primary sound capture driver",
    "primary sound driver",
    "primärer soundaufnahmetreiber",
    "primärer soundtreiber",
)


class AudioInputError(RuntimeError):
    """The selected physical audio source could not be opened."""


def input_extra_settings(
    sounddevice_module: Any,
    *,
    device_index: int | None = None,
    hostapi_name: str | None = None,
) -> Any | None:
    """Return the extra settings required to open an input endpoint.

    ``sounddevice`` is passed in rather than imported here so GUI/controller
    tests can keep using their lightweight fake modules.  All lookup failures
    intentionally degrade to ``None``; non-WASAPI platforms need no special
    settings.
    """

    if hostapi_name is None and device_index is not None and device_index >= 0:
        try:
            device = sounddevice_module.query_devices(device_index)
            hostapi_index = int(device.get("hostapi", -1))
            hostapis = sounddevice_module.query_hostapis()
            if 0 <= hostapi_index < len(hostapis):
                hostapi_name = str(hostapis[hostapi_index].get("name", ""))
        except Exception:
            hostapi_name = None

    if hostapi_name != WASAPI_HOSTAPI_NAME:
        return None

    try:
        return sounddevice_module.WasapiSettings(auto_convert=True)
    except (AttributeError, TypeError):
        return None


# Rates to try when a device rejects the pipeline rate, best (highest quality,
# most widely native) first.  A device opened at one of these is resampled to
# the pipeline rate by the capture loop (audio/resampler.py).
_FALLBACK_SAMPLERATES = (48000, 44100, 32000, 24000, 22050, 16000)


def usable_input_samplerate(
    sounddevice_module: Any,
    *,
    device_index: int,
    requested: int,
    channels: int = 1,
    hostapi_name: str | None = None,
) -> int | None:
    """Return a samplerate the device can be opened at, or ``None``.

    ``requested`` (the pipeline rate) is tried first so devices that support it
    natively are opened without conversion.  Otherwise the device's native rate
    and a few common rates are tried — Linux/PipeWire exposes named sources
    (Bluetooth, internal mics) only through JACK at their native rate, so
    probing solely at the pipeline rate would hide every one of them.  The
    capture loop resamples whatever rate this returns down to ``requested``.
    """
    rates = [requested]
    try:
        native = int(round(sounddevice_module.query_devices(device_index)["default_samplerate"]))
        if native > 0:
            rates.append(native)
    except Exception:
        pass
    rates.extend(_FALLBACK_SAMPLERATES)

    seen: set[int] = set()
    for rate in rates:
        if rate in seen:
            continue
        seen.add(rate)
        try:
            kwargs: dict[str, Any] = {}
            extra_settings = input_extra_settings(
                sounddevice_module,
                device_index=device_index,
                hostapi_name=hostapi_name,
            )
            if extra_settings is not None:
                kwargs["extra_settings"] = extra_settings
            sounddevice_module.check_input_settings(
                device=device_index, channels=channels, samplerate=rate, **kwargs
            )
            return rate
        except Exception:
            continue
    return None


def input_stream_kwargs(
    sounddevice_module: Any,
    *,
    device_index: int,
) -> dict[str, Any]:
    """Build optional ``InputStream`` keyword arguments for an endpoint."""

    settings = input_extra_settings(
        sounddevice_module,
        device_index=device_index,
    )
    return {"extra_settings": settings} if settings is not None else {}


def _same_physical_device(first: str, second: str) -> bool:
    first_normalized = " ".join(first.casefold().split())
    second_normalized = " ".join(second.casefold().split())
    if first_normalized == second_normalized:
        return True
    shorter = min(len(first_normalized), len(second_normalized))
    return (
        shorter >= 20
        and first_normalized[:shorter] == second_normalized[:shorter]
    )


def input_device_candidates(
    sounddevice_module: Any,
    *,
    device_index: int,
    samplerate: int,
    channels: int = 1,
    dtype: str | None = None,
) -> list[int]:
    """Return compatible host-API aliases for one physical microphone.

    PortAudio exposes the same Windows endpoint through WASAPI, MME and
    DirectSound.  Indices are process-local and the preferred backend can
    fail transiently after a USB headset restart.  The selected index remains
    first; fallbacks are restricted to matching device names, with WDM-KS and
    generic Windows mapper entries excluded.
    """

    if device_index < 0:
        return [device_index]

    try:
        devices = sounddevice_module.query_devices()
        hostapis = sounddevice_module.query_hostapis()
        selected = devices[device_index]
        selected_name = str(selected.get("name", "")).strip()
    except Exception:
        return [device_index]

    aliases: list[tuple[int, int]] = []
    for index, device in enumerate(devices):
        if index == device_index or int(device.get("max_input_channels", 0)) <= 0:
            continue
        name = str(device.get("name", "")).strip()
        if not name or not _same_physical_device(selected_name, name):
            continue
        if any(name.casefold().startswith(p) for p in _GENERIC_INPUT_PREFIXES):
            continue
        try:
            hostapi_index = int(device.get("hostapi", -1))
            if not 0 <= hostapi_index < len(hostapis):
                continue
            hostapi_name = str(hostapis[hostapi_index].get("name", ""))
        except (IndexError, KeyError, TypeError, ValueError):
            continue
        if hostapi_name in _SKIP_HOSTAPIS:
            continue

        check_kwargs: dict[str, Any] = {
            "device": index,
            "channels": channels,
            "samplerate": samplerate,
        }
        if dtype is not None:
            check_kwargs["dtype"] = dtype
        extra_settings = input_extra_settings(
            sounddevice_module,
            device_index=index,
            hostapi_name=hostapi_name,
        )
        if extra_settings is not None:
            check_kwargs["extra_settings"] = extra_settings
        try:
            sounddevice_module.check_input_settings(**check_kwargs)
        except Exception:
            continue
        aliases.append((_HOSTAPI_FALLBACK_PRIORITY.get(hostapi_name, 99), index))

    aliases.sort(key=lambda item: (item[0], item[1]))
    return [device_index, *(index for _priority, index in aliases)]

"""Tests for Windows microphone enumeration and host-API settings."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from audio.device_support import input_device_candidates, input_stream_kwargs
from audio.loopback import get_speaker, register
from gui import device_list
from gui.device_list import find_input_device_position


class _FakeWasapiSettings:
    def __init__(self, *, auto_convert: bool = False):
        self.auto_convert = auto_convert


def _patch_soundcard_without_speakers(monkeypatch) -> None:
    monkeypatch.setitem(
        sys.modules,
        "soundcard",
        SimpleNamespace(
            all_speakers=lambda: [],
            all_microphones=lambda include_loopback=False: [],
        ),
    )


def test_wasapi_auto_convert_keeps_native_48k_mic_and_wins_dedup(monkeypatch):
    devices = [
        {
            "name": "Microphone (Jabra Evolve2 40 SE)",
            "hostapi": 1,
            "max_input_channels": 1,
        },
        {
            "name": "Microphone (Jabra Evolve2 40 SE)",
            "hostapi": 0,
            "max_input_channels": 1,
        },
        {
            "name": "Microphone (Jabra Evolve2 40 SE",
            "hostapi": 2,
            "max_input_channels": 1,
        },
    ]
    hostapis = [
        {"name": "Windows WASAPI"},
        {"name": "Windows DirectSound"},
        {"name": "MME"},
    ]
    checks = []

    monkeypatch.setattr(device_list.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(device_list.sd, "query_hostapis", lambda: hostapis)
    monkeypatch.setattr(device_list.sd, "WasapiSettings", _FakeWasapiSettings)

    def check_input_settings(**kwargs):
        checks.append(kwargs)
        if kwargs["device"] == 1 and not getattr(
            kwargs.get("extra_settings"), "auto_convert", False
        ):
            raise RuntimeError("native endpoint only supports 48 kHz")

    monkeypatch.setattr(
        device_list.sd,
        "check_input_settings",
        check_input_settings,
    )
    _patch_soundcard_without_speakers(monkeypatch)

    display_names, base_names, indices, loopback = device_list.get_input_devices()

    assert display_names == ["1. Microphone (Jabra Evolve2 40 SE)"]
    assert base_names == ["Microphone (Jabra Evolve2 40 SE)"]
    assert indices == [1]
    assert loopback == [False]
    wasapi_check = next(item for item in checks if item["device"] == 1)
    assert wasapi_check["extra_settings"].auto_convert is True


def test_input_stream_kwargs_adds_auto_convert_only_for_wasapi():
    fake_sd = SimpleNamespace(
        query_devices=lambda index: {"hostapi": index},
        query_hostapis=lambda: [
            {"name": "Windows WASAPI"},
            {"name": "Windows DirectSound"},
        ],
        WasapiSettings=_FakeWasapiSettings,
    )

    wasapi = input_stream_kwargs(fake_sd, device_index=0)
    directsound = input_stream_kwargs(fake_sd, device_index=1)

    assert wasapi["extra_settings"].auto_convert is True
    assert directsound == {}


def test_saved_device_name_survives_mme_truncation_and_reindexing():
    assert (
        find_input_device_position(
            "Microphone (Jabra Evolve2 40 SE)",
            ["Other microphone", "Microphone (Jabra Evolve2 40 SE"],
        )
        == 1
    )
    assert find_input_device_position("Missing microphone", ["Other microphone"]) is None


def test_fallbacks_stay_on_same_mic_and_exclude_wdm_ks():
    devices = [
        {
            "name": "Microphone (Jabra Evolve2 40 SE",
            "hostapi": 1,
            "max_input_channels": 1,
        },
        {"name": "Unrelated microphone", "hostapi": 1, "max_input_channels": 1},
        *(
            {"name": f"Output {index}", "hostapi": 1, "max_input_channels": 0}
            for index in range(2, 9)
        ),
        {
            "name": "Microphone (Jabra Evolve2 40 SE)",
            "hostapi": 2,
            "max_input_channels": 1,
        },
        *(
            {"name": f"Output {index}", "hostapi": 1, "max_input_channels": 0}
            for index in range(10, 21)
        ),
        {
            "name": "Microphone (Jabra Evolve2 40 SE)",
            "hostapi": 0,
            "max_input_channels": 1,
        },
        *(
            {"name": f"Output {index}", "hostapi": 1, "max_input_channels": 0}
            for index in range(22, 30)
        ),
        {
            "name": "Microphone (Jabra Evolve2 40 SE)",
            "hostapi": 3,
            "max_input_channels": 1,
        },
    ]
    fake_sd = SimpleNamespace(
        query_devices=lambda: devices,
        query_hostapis=lambda: [
            {"name": "Windows WASAPI"},
            {"name": "MME"},
            {"name": "Windows DirectSound"},
            {"name": "Windows WDM-KS"},
        ],
        WasapiSettings=_FakeWasapiSettings,
        check_input_settings=lambda **kwargs: None,
    )

    candidates = input_device_candidates(
        fake_sd,
        device_index=21,
        samplerate=24000,
        dtype="int16",
    )

    assert candidates == [21, 0, 9]
    assert 1 not in candidates  # unrelated physical microphone
    assert 30 not in candidates  # WDM-KS is never an explicit fallback


def test_localized_windows_mapper_entries_are_filtered(monkeypatch):
    devices = [
        {
            "name": "Primärer Soundaufnahmetreiber",
            "hostapi": 0,
            "max_input_channels": 1,
        },
        {
            "name": "Microsoft Soundmapper - Input",
            "hostapi": 1,
            "max_input_channels": 1,
        },
        {
            "name": "Microphone (Jabra Evolve2 40 SE)",
            "hostapi": 2,
            "max_input_channels": 1,
        },
    ]
    hostapis = [
        {"name": "Windows DirectSound"},
        {"name": "MME"},
        {"name": "Windows WASAPI"},
    ]
    monkeypatch.setattr(device_list.sd, "query_devices", lambda: devices)
    monkeypatch.setattr(device_list.sd, "query_hostapis", lambda: hostapis)
    monkeypatch.setattr(device_list.sd, "WasapiSettings", _FakeWasapiSettings)
    monkeypatch.setattr(
        device_list.sd,
        "check_input_settings",
        lambda **kwargs: None,
    )
    _patch_soundcard_without_speakers(monkeypatch)

    _, base_names, indices, _ = device_list.get_input_devices()

    assert base_names == ["Microphone (Jabra Evolve2 40 SE)"]
    assert indices == [2]


def test_enumeration_failure_clears_stale_loopback_registry(monkeypatch):
    register(-1, object())
    monkeypatch.setattr(
        device_list.sd,
        "query_devices",
        lambda: (_ for _ in ()).throw(RuntimeError("PortAudio unavailable")),
    )

    assert device_list.get_input_devices() == ([], [], [], [])
    assert get_speaker(-1) is None


def _patch_single_microphone(monkeypatch) -> None:
    monkeypatch.setattr(
        device_list.sd,
        "query_devices",
        lambda: [
            {"name": "MacBook Pro Microphone", "hostapi": 0, "max_input_channels": 1}
        ],
    )
    monkeypatch.setattr(
        device_list.sd, "query_hostapis", lambda: [{"name": "Core Audio"}]
    )
    monkeypatch.setattr(
        device_list.sd, "check_input_settings", lambda **kwargs: None
    )


def test_loopback_speakers_are_not_listed_where_the_platform_has_none(monkeypatch):
    """macOS: soundcard's CoreAudio backend cannot record loopback, so every
    speaker offered here failed at open time with "no device with id <n>"."""
    _patch_single_microphone(monkeypatch)
    monkeypatch.setitem(
        sys.modules,
        "soundcard",
        SimpleNamespace(
            all_speakers=lambda: [SimpleNamespace(name="MacBook Pro Speakers", id=72)],
            all_microphones=lambda include_loopback=False: [],
        ),
    )
    monkeypatch.setattr(device_list, "_LOOPBACK_SUPPORTED", False)

    display_names, base_names, indices, loopback = device_list.get_input_devices()

    assert display_names == ["1. MacBook Pro Microphone"]
    assert base_names == ["MacBook Pro Microphone"]
    assert indices == [0]
    assert loopback == [False]


def test_loopback_speakers_are_listed_where_the_platform_supports_them(monkeypatch):
    _patch_single_microphone(monkeypatch)
    # Serves both enumeration paths so the test is platform-independent:
    # Windows reads all_speakers(), Linux the loopback-flagged entries of
    # all_microphones(include_loopback=True). _linux_real_source_names() asks
    # for the plain list, which stays empty -> no source filtering.
    loopback = SimpleNamespace(name="Speakers (Realtek)", id="{0.0}", isloopback=True)
    monkeypatch.setitem(
        sys.modules,
        "soundcard",
        SimpleNamespace(
            all_speakers=lambda: [loopback],
            all_microphones=lambda include_loopback=False: (
                [loopback] if include_loopback else []
            ),
        ),
    )
    monkeypatch.setattr(device_list, "_LOOPBACK_SUPPORTED", True)

    display_names, _, indices, loopback = device_list.get_input_devices()

    assert display_names[1] == "2. Speakers (Realtek) (Loopback)"
    assert indices[1] == -1
    assert loopback == [False, True]


def test_loopback_support_is_reported_per_platform():
    """The GUI asks this to decide whether to explain the missing entries."""
    assert device_list.loopback_supported() == (sys.platform != "darwin")

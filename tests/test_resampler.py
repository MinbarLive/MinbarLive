"""Tests for the streaming capture resampler and input-rate selection."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from audio.device_support import usable_input_samplerate
from audio.resampler import StreamResampler


def test_passthrough_is_identity_and_zero_copy():
    rs = StreamResampler(16000, 16000, 1)
    x = np.arange(100, dtype=np.float32)
    out = rs.process(x)
    assert out is x  # equal rates: no work, same array


def test_downsample_produces_expected_rate_and_length():
    rs = StreamResampler(48000, 16000, 1)
    x = np.zeros(48000, dtype=np.float32)
    out = rs.process(x)
    # ~1 s of 48 kHz -> ~1 s of 16 kHz, minus a few tail samples that need
    # future input (carried to the next block in a live stream).
    assert 15900 <= out.shape[0] <= 16000
    assert out.dtype == np.float32


def test_streaming_matches_single_shot_regardless_of_block_sizes():
    """State carried between blocks must make chunked == whole-signal."""
    t = np.arange(48000) / 48000
    x = (0.4 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)

    whole = StreamResampler(48000, 16000, 1).process(x)

    chunked = StreamResampler(48000, 16000, 1)
    parts = []
    i = 0
    for size in ([100, 517, 2048, 33] * 100):
        if i >= len(x):
            break
        parts.append(chunked.process(x[i : i + size]))
        i += size
    joined = np.concatenate(parts)

    m = min(len(whole), len(joined))
    assert np.allclose(whole[:m], joined[:m], atol=1e-6)


def test_downsample_preserves_a_pure_tone_cleanly():
    src, dst = 48000, 16000
    t = np.arange(src) / src
    x = (0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32)  # 1 kHz, well below Nyquist
    out = StreamResampler(src, dst, 1).process(x)
    # steady-state amplitude preserved (skip filter warm-up/tail edges)
    core = out[500:-500]
    assert 0.45 < np.abs(core).max() < 0.55
    assert np.sqrt(np.mean(core**2)) > 0.30  # ~0.5/sqrt(2)


def test_int16_dtype_and_levels_preserved():
    rs = StreamResampler(48000, 16000, 1)
    t = np.arange(48000) / 48000
    x = (np.sin(2 * np.pi * 300 * t) * 10000).astype(np.int16)
    out = rs.process(x)
    assert out.dtype == np.int16
    assert out.ndim == 1
    assert 9000 < int(np.abs(out).max()) <= 11000


def test_stereo_shape_preserved():
    rs = StreamResampler(48000, 16000, 2)
    x = np.zeros((4800, 2), dtype=np.float32)
    out = rs.process(x)
    assert out.ndim == 2 and out.shape[1] == 2


def _fake_sd(*, ok_rates, native=48000):
    def check(**kwargs):
        if kwargs["samplerate"] not in ok_rates:
            raise RuntimeError("unsupported rate")

    return SimpleNamespace(
        query_devices=lambda index: {"default_samplerate": native},
        query_hostapis=lambda: [{"name": "JACK Audio Connection Kit"}],
        check_input_settings=check,
    )


def test_usable_rate_prefers_requested_when_supported():
    sd = _fake_sd(ok_rates={16000, 48000})
    assert usable_input_samplerate(sd, device_index=0, requested=16000) == 16000


def test_usable_rate_falls_back_to_native_when_requested_rejected():
    # JACK/Bluetooth: only the native 48 kHz is accepted.
    sd = _fake_sd(ok_rates={48000})
    assert usable_input_samplerate(sd, device_index=0, requested=16000) == 48000


def test_usable_rate_returns_none_when_nothing_works():
    sd = _fake_sd(ok_rates=set())
    assert usable_input_samplerate(sd, device_index=0, requested=16000) is None

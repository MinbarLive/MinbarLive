"""DSP and controller-routing tests for the local input-level meter."""

from __future__ import annotations

import queue
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

import app_controller
from app_controller import AppController
from audio.level_meter import DBFS_FLOOR, AudioLevelMeter
from utils.settings import PIPELINE_MODE_STREAMING


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestAudioLevelMeter:
    def test_float_pcm_reports_rms_peak_and_dbfs(self):
        meter = AudioLevelMeter()

        meter.observe(np.array([0.5, -0.5], dtype=np.float32))
        level = meter.snapshot()

        assert level.rms == pytest.approx(0.5)
        assert level.peak == pytest.approx(0.5)
        assert level.rms_dbfs == pytest.approx(-6.0206, abs=0.001)
        assert level.peak_dbfs == pytest.approx(-6.0206, abs=0.001)
        assert level.clipping_ratio == 0.0
        assert level.is_stale is False

    def test_integer_pcm_is_normalized_and_clipping_is_counted(self):
        meter = AudioLevelMeter()

        meter.observe(np.array([-32768, 0, 32767], dtype=np.int16))
        level = meter.snapshot()

        assert level.peak == pytest.approx(1.0)
        assert level.peak_dbfs == pytest.approx(0.0)
        assert level.clipping_ratio == pytest.approx(2 / 3)
        assert level.clipped is True

    def test_release_is_smoothed_and_peak_is_held_then_decays(self):
        clock = FakeClock()
        meter = AudioLevelMeter(clock=clock, stale_after_seconds=2.0)
        meter.observe(np.full(100, 0.8, dtype=np.float32))

        clock.advance(0.1)
        meter.observe(np.full(100, 0.1, dtype=np.float32))
        released = meter.snapshot()

        assert 0.1 < released.rms < 0.8
        assert released.peak == pytest.approx(0.1)
        assert released.peak_hold == pytest.approx(0.8)

        clock.advance(0.7)
        decayed = meter.snapshot()
        assert 0.0 < decayed.peak_hold < 0.8

    def test_stale_capture_and_explicit_reset_publish_digital_silence(self):
        clock = FakeClock()
        meter = AudioLevelMeter(clock=clock, stale_after_seconds=0.5)
        meter.observe(np.ones(16, dtype=np.float32))

        clock.advance(0.51)
        stale = meter.snapshot()
        assert stale.is_stale is True
        assert stale.rms_dbfs == DBFS_FLOOR
        assert stale.peak_dbfs == DBFS_FLOOR
        assert stale.clipping_ratio == 0.0

        meter.observe(np.full(16, 0.25, dtype=np.float32))
        meter.reset()
        assert meter.snapshot().is_stale is True


def test_sounddevice_segmented_and_streaming_callbacks_observe_mono_pcm():
    controller = AppController()
    segmented = np.full((64, 1), 0.25, dtype=np.float32)

    controller._segmented_audio_callback(segmented, 64, None, None)
    assert controller.get_input_level().rms_dbfs == pytest.approx(-12.0412, abs=0.01)

    controller.reset_input_level()
    controller._streaming_capture_rate = 24000
    streaming = np.full((64, 1), 16384, dtype=np.int16)
    controller._streaming_audio_callback(streaming, 64, None, None)

    assert controller.get_input_level().rms_dbfs == pytest.approx(-6.0206, abs=0.01)
    assert controller._streaming_feed_queue.get_nowait() == streaming[:, 0].tobytes()


class _OneBlockRecorder:
    def __init__(self, stop_event, value: float) -> None:
        self._stop_event = stop_event
        self._value = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def record(self, numframes: int):
        self._stop_event.set()
        return np.full((numframes, 2), self._value, dtype=np.float32)


class _FakeLoopbackMicrophone:
    def __init__(self, stop_event, value: float) -> None:
        self._stop_event = stop_event
        self._value = value

    def recorder(self, **kwargs):
        return _OneBlockRecorder(self._stop_event, self._value)


def test_loopback_segmented_and_streaming_paths_observe_mono_pcm(monkeypatch):
    controller = AppController()
    speaker = SimpleNamespace(id="speaker-id", name="Test speakers")
    startup_result = queue.Queue()

    fake_soundcard = SimpleNamespace(
        get_microphone=lambda **kwargs: _FakeLoopbackMicrophone(
            controller._input_stop_event, 0.2
        )
    )
    monkeypatch.setitem(sys.modules, "soundcard", fake_soundcard)
    controller._loopback_segmented_loop(
        9001, speaker, startup_result=startup_result
    )

    assert startup_result.get_nowait() is None
    assert controller.get_input_level().rms_dbfs == pytest.approx(-13.9794, abs=0.01)

    controller.reset_input_level()
    controller._input_stop_event = app_controller.threading.Event()
    fake_soundcard.get_microphone = lambda **kwargs: _FakeLoopbackMicrophone(
        controller._input_stop_event, 0.4
    )
    controller._loopback_streaming_loop(
        9001, speaker, 24000, startup_result=startup_result
    )

    assert startup_result.get_nowait() is None
    assert controller.get_input_level().rms_dbfs == pytest.approx(-7.9588, abs=0.01)
    assert controller._streaming_feed_queue.get_nowait()


class _PreviewInputStream:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.callback = kwargs["callback"]
        self.closed = False
        self.__class__.instances.append(self)

    def start(self):
        block = np.full((160, 1), 0.3, dtype=np.float32)
        self.callback(block, len(block), None, None)
        return self

    def close(self) -> None:
        self.closed = True


def test_preview_is_local_and_live_start_releases_it(monkeypatch, tmp_path):
    _PreviewInputStream.instances.clear()
    context_manager = MagicMock()
    settings = SimpleNamespace(
        ai_provider="test",
        pipeline_mode=PIPELINE_MODE_STREAMING,
        transcription_provider="test-realtime",
    )
    monkeypatch.setattr(
        app_controller, "sd", SimpleNamespace(InputStream=_PreviewInputStream)
    )
    monkeypatch.setattr(app_controller, "AUDIO_DIR", str(tmp_path))
    monkeypatch.setattr(app_controller, "load_settings", lambda: settings)
    monkeypatch.setattr(
        app_controller, "get_translation_model_chain", lambda: ["test-model"]
    )
    monkeypatch.setattr(
        app_controller, "get_context_manager", lambda: context_manager
    )

    controller = AppController()
    controller.start_input_level_test(4)
    assert controller.is_input_level_test_running() is True
    assert controller.get_input_level().rms_dbfs == pytest.approx(-10.4576, abs=0.01)

    started = []

    def fake_start_streaming(device, loaded_settings):
        assert controller.is_input_level_test_running() is False
        assert _PreviewInputStream.instances[0].closed is True
        started.append((device, loaded_settings))

    monkeypatch.setattr(controller, "_start_streaming_threads", fake_start_streaming)
    controller.start(input_device=4)

    assert started == [(4, settings)]
    assert controller.get_input_level().is_stale is True
    controller.stop()


def test_preview_rejects_parallel_live_session():
    controller = AppController()
    controller._running = True

    with pytest.raises(RuntimeError, match="live session"):
        controller.start_input_level_test(1)

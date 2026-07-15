"""Tests for the voice-activity noise gate (audio/vad.py).

Two layers:

* Real-webrtcvad tests: stationary noise (hiss, hum) must be classified as
  non-speech — the exact failure mode the filter exists for. These skip when
  webrtcvad is not installed (the app degrades to pass-through then anyway).
* FakeVad tests: the threshold/state-machine logic of has_speech and
  StreamNoiseGate, deterministic and independent of webrtcvad's model.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import audio.vad as vad_mod
from audio.vad import StreamNoiseGate, has_speech
from config import FS


def _hiss(seconds=5.0, amp=0.01, rate=FS):
    rng = np.random.default_rng(42)
    return rng.normal(0, amp, int(rate * seconds)).astype(np.float32)


class FakeVad:
    """Scripted per-frame decision: every frame follows .speech."""

    def __init__(self, speech=True):
        self.speech = speech

    def is_speech(self, data, rate):
        return self.speech


@pytest.fixture
def fake_vad(monkeypatch):
    fake = FakeVad()
    monkeypatch.setattr(vad_mod, "_new_vad", lambda: fake)
    return fake


class TestHasSpeechRealVad:
    """Against the real webrtcvad — the noise cases the filter targets."""

    @pytest.fixture(autouse=True)
    def _require_webrtcvad(self):
        pytest.importorskip("webrtcvad")

    def test_hiss_is_not_speech(self):
        assert has_speech(_hiss()) is False

    def test_hum_and_hiss_is_not_speech(self):
        """Mains hum + hiss: the classic muted-mixer-channel noise floor.
        Well above SILENCE_THRESHOLD, so only the VAD gate can catch it."""
        n = FS * 5
        hum = 0.05 * np.sin(2 * np.pi * 50 * np.arange(n) / FS)
        audio = (hum + _hiss(5.0)).astype(np.float32)
        assert has_speech(audio) is False

    def test_pure_silence_is_not_speech(self):
        assert has_speech(np.zeros(FS * 2, dtype=np.float32)) is False


class TestHasSpeechLogic:
    def test_speech_frames_pass(self, fake_vad):
        fake_vad.speech = True
        assert has_speech(_hiss()) is True

    def test_all_nonspeech_frames_fail(self, fake_vad):
        fake_vad.speech = False
        assert has_speech(_hiss()) is False

    def test_unavailable_vad_passes_everything(self, monkeypatch):
        monkeypatch.setattr(vad_mod, "_new_vad", lambda: None)
        assert has_speech(_hiss()) is True

    def test_unsupported_rate_passes(self, fake_vad):
        fake_vad.speech = False
        assert has_speech(_hiss(rate=22050), sample_rate=22050) is True

    def test_shorter_than_one_frame_passes(self, fake_vad):
        fake_vad.speech = False
        assert has_speech(np.zeros(100, dtype=np.float32)) is True

    def test_vad_exception_passes(self, monkeypatch):
        class Broken:
            def is_speech(self, data, rate):
                raise RuntimeError("boom")

        monkeypatch.setattr(vad_mod, "_new_vad", lambda: Broken())
        assert has_speech(_hiss()) is True


class RecordingVad:
    """FakeVad that records the PCM frames it is asked to judge."""

    def __init__(self, speech=True):
        self.speech = speech
        self.frames = []

    def is_speech(self, data, rate):
        self.frames.append(np.frombuffer(data, dtype=np.int16))
        return self.speech


class TestDecisionBoost:
    """Quiet-mic fix: the VAD judges a boosted copy of quiet audio; the
    audio actually fed onward is never modified (user-confirmed defect:
    a low-gain mic was classified all-non-speech and the gate starved the
    streaming pipeline)."""

    def _quiet_chunk(self, peak=100, n=3200):  # ~-50 dBFS peak, 200 ms
        rng = np.random.default_rng(7)
        return (
            rng.normal(0, peak / 4, n).clip(-peak, peak).astype(np.int16)
        )

    def test_gate_judges_boosted_copy_but_output_is_unchanged(self, monkeypatch):
        rec = RecordingVad(speech=True)
        monkeypatch.setattr(vad_mod, "_new_vad", lambda: rec)
        gate = StreamNoiseGate(FS)
        quiet = self._quiet_chunk()
        chunk = quiet.tobytes()
        assert gate.process(chunk) == chunk  # fed audio byte-identical
        seen = np.concatenate(rec.frames)
        assert np.abs(seen).max() > np.abs(quiet).max() * 4  # decision boosted

    def test_boost_targets_decision_peak(self):
        pcm = np.full(4800, 40, dtype=np.int16)  # ≈ -58 dBFS
        boosted = vad_mod._boost_for_decision(pcm)
        target = vad_mod.VAD_DECISION_TARGET_PEAK * 32768
        assert np.abs(boosted).max() <= target + 1
        assert np.abs(boosted).max() == 40 * vad_mod.VAD_DECISION_MAX_BOOST

    def test_loud_audio_is_not_boosted(self):
        pcm = (np.sin(np.linspace(0, 100, 4800)) * 16000).astype(np.int16)
        assert vad_mod._boost_for_decision(pcm) is pcm

    def test_silence_stays_silent(self):
        pcm = np.zeros(4800, dtype=np.int16)
        assert vad_mod._boost_for_decision(pcm) is pcm


class TestDecisionBoostRealVad:
    """The boost must never turn a quiet noise floor into a false gate-open:
    the target peak (-30.5 dBFS) sits below every measured noise flip
    (hiss -14, 50 Hz hum+hiss -20, 60 Hz hum+hiss ~-25 dBFS)."""

    @pytest.fixture(autouse=True)
    def _require_webrtcvad(self):
        pytest.importorskip("webrtcvad")

    def test_quiet_hiss_still_not_speech_after_boost(self):
        assert has_speech(_hiss(amp=0.0005)) is False

    def test_quiet_hum_hiss_still_not_speech_after_boost(self):
        n = FS * 5
        hum = 0.002 * np.sin(2 * np.pi * 50 * np.arange(n) / FS)
        audio = (hum + _hiss(5.0, amp=0.0005)).astype(np.float32)
        assert has_speech(audio) is False


class TestToVadRate:
    def test_supported_rate_unchanged(self):
        pcm = np.arange(480, dtype=np.int16)
        out, rate = vad_mod._to_vad_rate(pcm, 16000)
        assert rate == 16000
        assert out is pcm

    def test_24k_decimated_to_8k(self):
        """The OpenAI Realtime capture rate maps onto a supported one."""
        pcm = np.arange(24000, dtype=np.int16)
        out, rate = vad_mod._to_vad_rate(pcm, 24000)
        assert rate == 8000
        assert out.size == 8000

    def test_unsupported_rate_returns_none(self):
        out, rate = vad_mod._to_vad_rate(np.zeros(100, dtype=np.int16), 22050)
        assert out is None


class TestStreamNoiseGate:
    """State machine, driven by FakeVad (webrtcvad-independent)."""

    CHUNK = np.full(3200, 1000, dtype=np.int16).tobytes()  # 200 ms at FS

    def _run_until_zeroed(self, gate, max_chunks=40):
        """Feed non-speech chunks until the gate closes; returns how many
        passed through before the first zeroed chunk."""
        for i in range(max_chunks):
            out = gate.process(self.CHUNK)
            if out == bytes(len(self.CHUNK)):
                return i
            assert out == self.CHUNK
        pytest.fail("gate never closed on sustained non-speech")

    def test_speech_passes_through(self, fake_vad):
        gate = StreamNoiseGate(FS)
        for _ in range(20):
            assert gate.process(self.CHUNK) == self.CHUNK

    def test_sustained_nonspeech_gets_zeroed_after_hangover(self, fake_vad):
        gate = StreamNoiseGate(FS)
        fake_vad.speech = False
        passed = self._run_until_zeroed(gate)
        # The hangover (2 s = 10 chunks) must pass through before zeroing —
        # normal speech pauses are never touched.
        assert passed >= 10
        # Once closed, it stays closed on further non-speech.
        assert gate.process(self.CHUNK) == bytes(len(self.CHUNK))

    def test_gate_reopens_immediately_on_speech(self, fake_vad):
        gate = StreamNoiseGate(FS)
        fake_vad.speech = False
        self._run_until_zeroed(gate)
        # The chunk containing the speech onset passes through IN FULL —
        # the decision is made before forwarding.
        fake_vad.speech = True
        assert gate.process(self.CHUNK) == self.CHUNK

    def test_passthrough_when_vad_unavailable(self, monkeypatch):
        monkeypatch.setattr(vad_mod, "_new_vad", lambda: None)
        gate = StreamNoiseGate(FS)
        for _ in range(30):
            assert gate.process(self.CHUNK) == self.CHUNK

    def test_malformed_chunks_pass_through(self, fake_vad):
        gate = StreamNoiseGate(FS)
        assert gate.process(b"") == b""
        assert gate.process(b"abc") == b"abc"  # odd length, not int16 PCM

    def test_vad_exception_disables_gate(self, monkeypatch):
        class Broken:
            def is_speech(self, data, rate):
                raise RuntimeError("boom")

        monkeypatch.setattr(vad_mod, "_new_vad", lambda: Broken())
        gate = StreamNoiseGate(FS)
        assert gate.process(self.CHUNK) == self.CHUNK
        assert gate._vad is None  # disabled for the session, not retried

    def test_24k_gate_preserves_chunk_length(self, fake_vad):
        """OpenAI Realtime feeds 24 kHz PCM; zeroed output must match the
        input length exactly (the engine's timing depends on it)."""
        gate = StreamNoiseGate(24000)
        fake_vad.speech = False
        chunk = np.full(4800, 1000, dtype=np.int16).tobytes()  # 200 ms at 24k
        for _ in range(40):
            out = gate.process(chunk)
            assert len(out) == len(chunk)
            if out == bytes(len(chunk)):
                return
        pytest.fail("24k gate never closed on sustained non-speech")

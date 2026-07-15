"""Controller-level tests for the segmented pipeline (_process_audio).

The pytest suite historically never exercised the live segmented path — the
STT-logic consolidation into translation/stt.py and the semantic stale-buffer
flush were the trigger to add these. They drive AppController._process_audio
directly with faked providers and a temp audio dir: WAV file → transcription
(fallback chain) → optional Arabic re-pass → strategy → translate_text →
translation_queue, plus the idle flush of a stale semantic buffer.
"""

import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import scipy.io.wavfile as wavfile

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import app_controller
import translation.stt
from app_controller import AppController
from config import FS
from translation.buffering import ChunkBasedStrategy, SemanticBufferingStrategy
from utils.settings import Settings


def _wait_for(predicate, timeout=3.0, interval=0.02):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeTranscription:
    """Records calls; Arabic-language passes return Arabic script."""

    def __init__(self, fail_models=()):
        self.calls = []
        self.fail_models = set(fail_models)

    def transcribe(self, audio, *, model, language=None, prompt=None):
        self.calls.append((model, language))
        if model in self.fail_models:
            raise RuntimeError(f"{model} down")
        return "نص عربي" if language == "ar" else "metin"


class ScriptedProvider:
    """Returns a scripted sequence of transcriptions, one per call — for
    exercising cross-segment behaviour (overlap dedup, fragment gate)."""

    def __init__(self, texts):
        self.texts = list(texts)
        self.calls = []

    def transcribe(self, audio, *, model, language=None, prompt=None):
        self.calls.append((model, language))
        return self.texts.pop(0) if self.texts else ""


@pytest.fixture
def segmented_env(monkeypatch, tmp_path):
    """An AppController's _process_audio wired to fakes and a temp audio dir."""
    audio_dir = tmp_path / "audio"
    audio_dir.mkdir()
    provider = FakeTranscription()
    context_mgr = MagicMock()
    context_mgr.get_context.return_value = ""
    settings = Settings(source_language="Turkish", target_language="German")
    translations = {"fn": lambda text: f"DE:{text}"}
    translate_calls = []

    def fake_translate(text, context="", arabic_text=""):
        translate_calls.append((text, context, arabic_text))
        return translations["fn"](text)

    monkeypatch.setattr(app_controller, "AUDIO_DIR", str(audio_dir))
    monkeypatch.setattr(app_controller, "is_silence", lambda a: False)
    # The fixture WAVs are tones, not speech — pin the VAD gate open so the
    # real webrtcvad classification can't drop them (tests override this
    # to exercise the gate itself).
    monkeypatch.setattr(app_controller, "has_speech", lambda a: True)
    monkeypatch.setattr(
        app_controller, "get_transcription_provider", lambda: provider
    )
    monkeypatch.setattr(
        app_controller, "get_transcription_model_chain", lambda: ["m1", "m2"]
    )
    monkeypatch.setattr(app_controller, "get_context_manager", lambda: context_mgr)
    monkeypatch.setattr(
        app_controller, "load_settings", lambda use_cache=True: settings
    )
    monkeypatch.setattr(app_controller, "translate_text", fake_translate)
    monkeypatch.setattr(
        app_controller, "log_transcription_and_translation", lambda *a, **k: None
    )
    monkeypatch.setattr(app_controller, "get_user_message", lambda key: f"MSG:{key}")
    monkeypatch.setattr(
        translation.stt, "retry_with_backoff", lambda fn, **kw: fn()
    )

    controller = AppController()
    controller.strategy = ChunkBasedStrategy()
    thread_box = {}

    def add_wav(name="a.wav"):
        # Stage outside the watched dir, then move in — the processor loop
        # must never see a half-written file.
        tone = 0.3 * np.sin(
            2 * np.pi * 440 * np.arange(int(FS * 0.5), dtype=np.float32) / FS
        )
        staged = tmp_path / f"staged_{name}"
        wavfile.write(str(staged), FS, (tone * 32767).astype(np.int16))
        os.replace(str(staged), str(audio_dir / name))

    def start():
        t = threading.Thread(target=controller._process_audio, daemon=True)
        thread_box["t"] = t
        t.start()
        return t

    env = SimpleNamespace(
        controller=controller,
        provider=provider,
        settings=settings,
        translations=translations,
        translate_calls=translate_calls,
        add_wav=add_wav,
        start=start,
        audio_dir=audio_dir,
    )
    yield env
    controller.stop_event.set()
    if "t" in thread_box:
        thread_box["t"].join(timeout=2.0)
        # A leaked loop would outlive the monkeypatches and hit the real
        # AUDIO_DIR/providers (the Gemini-Live flake lesson) — fail loudly.
        assert not thread_box["t"].is_alive(), "_process_audio thread leaked"


class TestSegmentedPipeline:
    def test_segment_flows_to_translation_queue(self, segmented_env):
        env = segmented_env
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert env.controller.translation_queue.get_nowait() == ("DE:metin", "metin")
        # The processed file is consumed.
        assert _wait_for(lambda: not any(env.audio_dir.iterdir()))

    def test_leftover_thread_not_rearmed_by_new_session_event(self, segmented_env):
        """stop() joins with a timeout, so a processor thread busy in an API
        call can outlive it; the next start() then REPLACES self.stop_event.
        The leftover thread must exit on its own captured event instead of
        being re-armed by the fresh one — a re-armed zombie ran inside a
        streaming session (strategy=None) and double-processed audio
        (observed live 2026-07-15)."""
        env = segmented_env
        t = env.start()
        env.add_wav()  # prove the loop is running before swapping the event
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        old_event = env.controller.stop_event
        env.controller.stop_event = threading.Event()  # what start() does
        old_event.set()  # what the old session's stop() did
        t.join(timeout=2.0)
        assert not t.is_alive(), "old thread was re-armed by the new event"

    def test_unchanged_translation_suppresses_source_line(self, segmented_env):
        """Bilingual guard: a pass-through translation must not render the
        same text twice (source line is dropped)."""
        env = segmented_env
        env.translations["fn"] = lambda text: text
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert env.controller.translation_queue.get_nowait() == ("metin", None)

    def test_model_fallback_chain(self, segmented_env):
        env = segmented_env
        env.provider.fail_models = {"m1"}
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert [c[0] for c in env.provider.calls][:2] == ["m1", "m2"]

    def test_all_models_failed_skips_segment(self, segmented_env):
        env = segmented_env
        env.provider.fail_models = {"m1", "m2"}
        env.add_wav()
        env.start()
        # File is deleted without producing a subtitle...
        assert _wait_for(lambda: not any(env.audio_dir.iterdir()))
        assert env.controller.translation_queue.empty()
        # ...and the loop survives: a later good segment still flows.
        env.provider.fail_models = set()
        env.add_wav("b.wav")
        assert _wait_for(lambda: not env.controller.translation_queue.empty())

    def test_arabic_re_pass_feeds_translator(self, segmented_env):
        """Non-Arabic source in Islamic mode: the same audio is transcribed a
        second time with an Arabic hint for the Quran/Athan matchers."""
        env = segmented_env
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert ("m1", "ar") in env.provider.calls
        assert env.translate_calls[0][2] == "نص عربي"

    def test_arabic_re_pass_skipped_when_islamic_mode_off(self, segmented_env):
        env = segmented_env
        env.settings.islamic_mode = False
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert ("m1", "ar") not in env.provider.calls
        assert env.translate_calls[0][2] == ""

    def test_stale_semantic_buffer_flushes_during_silence(self, segmented_env):
        """The semantic timeout used to fire only inside add_segment — during
        pure silence no segments arrive, so an incomplete buffered sentence
        sat until speech resumed or stop. The idle flush must emit it."""
        env = segmented_env
        env.controller.strategy = SemanticBufferingStrategy(
            max_chunks=5, max_seconds=1.0
        )
        env.add_wav()
        env.start()
        # "metin" is incomplete (no sentence end, < 18 words): buffered, and
        # no further segments ever arrive.
        assert _wait_for(lambda: len(env.provider.calls) >= 1)
        assert env.controller.translation_queue.empty()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert env.controller.translation_queue.get_nowait() == ("DE:metin", "metin")

    def test_stale_flush_error_shows_subtitle_and_survives(self, segmented_env):
        env = segmented_env
        env.controller.strategy = SemanticBufferingStrategy(
            max_chunks=5, max_seconds=0.3
        )

        def boom(text):
            raise RuntimeError("api down")

        env.translations["fn"] = boom
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert env.controller.translation_queue.get_nowait() == (
            "MSG:connection_error",
            None,
        )
        # Thread survives the flush error: a later segment still flows.
        env.translations["fn"] = lambda text: f"DE:{text}"
        env.add_wav("b.wav")
        assert _wait_for(lambda: not env.controller.translation_queue.empty())

    def test_noise_filter_skips_non_speech_segment(self, segmented_env, monkeypatch):
        """Loud non-speech (static/hum from a muted mixer channel) must be
        deleted without an STT call — the hallucinated-subtitles fix."""
        env = segmented_env
        monkeypatch.setattr(app_controller, "has_speech", lambda a: False)
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not os.listdir(env.audio_dir))
        assert env.provider.calls == []
        assert env.controller.translation_queue.empty()

    def test_noise_filter_off_processes_non_speech(self, segmented_env, monkeypatch):
        """The settings checkbox bypasses the gate entirely."""
        env = segmented_env
        env.settings.noise_filter = False
        monkeypatch.setattr(app_controller, "has_speech", lambda a: False)
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not env.controller.translation_queue.empty())
        assert env.controller.translation_queue.get_nowait() == ("DE:metin", "metin")

    def test_overlap_prefix_stripped_across_segments(self, segmented_env, monkeypatch):
        """The 3s overlap repeats the previous segment's tail at the head of
        the next — it must be stripped before translation (the visible
        boundary duplicate)."""
        env = segmented_env
        env.settings.source_language = "Arabic"  # primary is ar; no re-pass
        scripted = ScriptedProvider(["الله اكبر الله", "الله رب العالمين"])
        monkeypatch.setattr(
            app_controller, "get_transcription_provider", lambda: scripted
        )
        env.add_wav("a.wav")
        env.add_wav("b.wav")
        env.start()
        assert _wait_for(lambda: env.controller.translation_queue.qsize() >= 2)
        first = env.controller.translation_queue.get_nowait()
        second = env.controller.translation_queue.get_nowait()
        assert first == ("DE:الله اكبر الله", "الله اكبر الله")
        # Leading "الله" (the overlap) is gone from the second segment.
        assert second == ("DE:رب العالمين", "رب العالمين")

    def test_fragment_segment_skipped(self, segmented_env, monkeypatch):
        """A sub-word transcription ("م") is deleted without a GPT call."""
        env = segmented_env
        env.settings.source_language = "Arabic"
        scripted = ScriptedProvider(["م"])
        monkeypatch.setattr(
            app_controller, "get_transcription_provider", lambda: scripted
        )
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not any(env.audio_dir.iterdir()))
        assert env.controller.translation_queue.empty()

    def test_empty_translation_suppressed(self, segmented_env):
        """GPT judging the input unintelligible returns "" — no blank
        subtitle is queued."""
        env = segmented_env
        env.translations["fn"] = lambda text: ""
        env.add_wav()
        env.start()
        assert _wait_for(lambda: not any(env.audio_dir.iterdir()))
        assert env.controller.translation_queue.empty()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

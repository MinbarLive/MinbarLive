"""Controller-level tests for pipeline_mode="streaming" (P7).

These drive AppController's streaming path end to end with a faked Deepgram
provider and faked audio input: transcript callbacks → utterance
accumulation → translate_text → translation_queue, plus the start/stop
lifecycle guarantees (validation before side effects, stale-queue draining,
final flush on stop, forced flush for continuous speech, error recovery).
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import app_controller
from app_controller import AppController, _StreamingUtteranceSession
from utils.settings import PIPELINE_MODE_STREAMING, Settings


def _wait_for(predicate, timeout=2.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeStreamHandle:
    def __init__(self):
        self.fed = []
        self.closed = False

    def feed(self, pcm_bytes):
        self.fed.append(pcm_bytes)

    def close(self):
        self.closed = True


class FakeStreamingProvider:
    def __init__(self):
        self.handle = FakeStreamHandle()  # most recent handle
        self.open_count = 0
        self.opened_with = None
        self.on_transcript = None
        self.on_utterance_end = None
        self.on_error = None

    def open_stream(
        self, *, model, language, on_transcript, on_utterance_end, on_error
    ):
        self.open_count += 1
        if self.open_count > 1:
            # A reconnect gets a fresh handle, like a real re-opened socket.
            self.handle = FakeStreamHandle()
        self.opened_with = {"model": model, "language": language}
        self.on_transcript = on_transcript
        self.on_utterance_end = on_utterance_end
        self.on_error = on_error
        return self.handle


class FakeInputStream:
    """Stands in for sounddevice.InputStream (never opens a device)."""

    def __init__(self, **kwargs):
        pass

    def start(self):
        return self

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def streaming_env(monkeypatch):
    """An AppController wired to fakes for everything external."""
    provider = FakeStreamingProvider()
    context_mgr = MagicMock()
    context_mgr.get_context.return_value = ""
    settings = Settings(
        source_language="Arabic",
        target_language="German",
        pipeline_mode=PIPELINE_MODE_STREAMING,
    )

    monkeypatch.setattr(
        app_controller, "sd", SimpleNamespace(InputStream=FakeInputStream)
    )
    monkeypatch.setattr(
        app_controller, "get_streaming_transcription_provider", lambda: provider
    )
    monkeypatch.setattr(app_controller, "has_usable_key", lambda p: True)
    monkeypatch.setattr(app_controller, "get_context_manager", lambda: context_mgr)
    monkeypatch.setattr(
        app_controller, "load_settings", lambda use_cache=True: settings
    )
    monkeypatch.setattr(
        app_controller,
        "translate_text",
        lambda text, context="", arabic_text="": f"XX:{text}",
    )
    monkeypatch.setattr(
        app_controller, "log_transcription_and_translation", lambda *a, **k: None
    )
    monkeypatch.setattr(app_controller, "get_user_message", lambda key: f"MSG:{key}")
    # Coalescing holds short utterances up to COALESCE_HOLD_SECONDS; compress
    # it so tests that emit a single short utterance flush in milliseconds
    # (the coalescing-specific tests override it back up).
    monkeypatch.setattr(app_controller, "STREAMING_COALESCE_HOLD_SECONDS", 0.05)

    env = SimpleNamespace(
        controller=AppController(),
        provider=provider,
        context_mgr=context_mgr,
        settings=settings,
    )
    yield env
    env.controller.stop(timeout=1.0)


class TestStreamingUtteranceSession:
    def test_take_joins_and_resets(self):
        s = _StreamingUtteranceSession()
        s.add_final("a")
        s.add_final("b")
        assert s.has_pending()
        text, _rev = s.take_and_reset()
        assert text == "a b"
        assert not s.has_pending()
        text, _rev = s.take_and_reset()
        assert text == ""

    def test_age_zero_when_empty(self):
        s = _StreamingUtteranceSession()
        assert s.seconds_since_first_part() == 0.0

    def test_age_measured_from_first_part_not_last(self):
        """Continuous speech keeps adding finals; the forced-flush clock must
        run from the FIRST part or it would never fire."""
        s = _StreamingUtteranceSession()
        s.add_final("first")
        time.sleep(0.05)
        s.add_final("second")
        assert s.seconds_since_first_part() >= 0.05

    def test_take_resets_age(self):
        s = _StreamingUtteranceSession()
        s.add_final("first")
        s.take_and_reset()
        assert s.seconds_since_first_part() == 0.0


class TestSessionLiveText:
    def test_interim_publishes_live_text(self):
        s = _StreamingUtteranceSession()
        s.set_interim("bismi")
        assert s.get_live_state() == ("bismi", False)

    def test_interim_corrects_itself(self):
        """Each interim replaces the previous hypothesis (self-correction)."""
        s = _StreamingUtteranceSession()
        s.set_interim("bismi")
        s.set_interim("bismillah ar-rahman")
        assert s.get_live_state() == ("bismillah ar-rahman", False)

    def test_final_absorbs_interim_and_joins_parts(self):
        s = _StreamingUtteranceSession()
        s.set_interim("part one draft")
        s.add_final("part one")
        s.set_interim("part two dra")
        assert s.get_live_state() == ("part one part two dra", False)

    def test_take_keeps_live_text_settled_until_cleared(self):
        """The finished source must stay visible during the translation call,
        marked settled so the GUI recolors it in place ("finished")."""
        s = _StreamingUtteranceSession()
        s.add_final("settled text")
        _text, rev = s.take_and_reset()
        assert s.get_live_state() == ("settled text", True)
        s.clear_live_if_unchanged(rev)
        assert s.get_live_state() == ("", False)

    def test_new_speech_resets_settled(self):
        """A pipelined next utterance takes over the line as in-progress."""
        s = _StreamingUtteranceSession()
        s.add_final("first utterance")
        s.take_and_reset()
        s.set_interim("second utter")
        assert s.get_live_state() == ("second utter", False)

    def test_clear_skipped_when_newer_speech_arrived(self):
        """A pipelined next utterance must not be blanked when the previous
        utterance's translation lands."""
        s = _StreamingUtteranceSession()
        s.add_final("first utterance")
        _text, rev = s.take_and_reset()
        s.set_interim("second utter")  # newer speech during translation
        s.clear_live_if_unchanged(rev)
        assert s.get_live_state() == ("second utter", False)

    def test_clear_live_is_unconditional(self):
        s = _StreamingUtteranceSession()
        s.set_interim("anything")
        s.clear_live()
        assert s.get_live_state() == ("", False)


class TestStreamingStartValidation:
    def test_automatic_source_rejected_before_side_effects(self, streaming_env):
        streaming_env.settings.source_language = "Automatic"
        with pytest.raises(ValueError):
            streaming_env.controller.start(input_device=0)
        # A failed start must leave nothing behind — especially no running
        # context-manager thread (each leak would double summarization calls)
        streaming_env.context_mgr.start.assert_not_called()
        assert streaming_env.controller._running is False

    def test_missing_engine_key_rejected_before_side_effects(
        self, streaming_env, monkeypatch
    ):
        monkeypatch.setattr(app_controller, "has_usable_key", lambda p: False)
        with pytest.raises(ValueError, match="Gemini API key"):
            streaming_env.controller.start(input_device=0)
        streaming_env.context_mgr.start.assert_not_called()
        assert streaming_env.controller._running is False

    def test_failed_start_can_be_retried_cleanly(self, streaming_env, monkeypatch):
        monkeypatch.setattr(app_controller, "has_usable_key", lambda p: False)
        with pytest.raises(ValueError):
            streaming_env.controller.start(input_device=0)
        monkeypatch.setattr(app_controller, "has_usable_key", lambda p: True)
        streaming_env.controller.start(input_device=0)
        assert streaming_env.controller._running is True
        assert streaming_env.context_mgr.start.call_count == 1

    def test_provider_startup_error_is_synchronous_and_leaves_no_state(
        self, streaming_env, monkeypatch
    ):
        def reject_startup(**_kwargs):
            raise RuntimeError("invalid_api_key")

        monkeypatch.setattr(streaming_env.provider, "open_stream", reject_startup)

        with pytest.raises(RuntimeError, match="invalid_api_key"):
            streaming_env.controller.start(input_device=0)

        streaming_env.context_mgr.start.assert_not_called()
        assert streaming_env.controller._running is False
        assert streaming_env.controller._streaming_handle is None
        assert streaming_env.controller._streaming_session is None


class TestStreamingPipeline:
    def _start(self, env):
        env.controller.start(input_device=0)
        assert env.provider.on_transcript is not None
        return env.controller, env.provider

    def test_utterance_flows_to_translation_queue(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_transcript("interim text", False)  # interim: ignored
        provider.on_transcript("part one", True)
        provider.on_transcript("part two", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        translation, source = controller.translation_queue.get_nowait()
        assert translation == "XX:part one part two"
        assert source == "part one part two"
        streaming_env.context_mgr.add_transcription.assert_called_once()

    def test_same_language_mode_emits_no_source(self, streaming_env):
        streaming_env.settings.target_language = "Arabic"
        controller, provider = self._start(streaming_env)
        provider.on_transcript("some arabic", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        _translation, source = controller.translation_queue.get_nowait()
        assert source is None

    def test_identical_translation_emits_no_source(self, streaming_env, monkeypatch):
        # Per-segment bypasses ("Automatic" source + Arabic-script text +
        # Arabic target) and code-switching pass-through return the input
        # unchanged even though the language *names* differ — bilingual mode
        # must not render the same line twice.
        monkeypatch.setattr(
            app_controller,
            "translate_text",
            lambda text, context="", arabic_text="": text,
        )
        controller, provider = self._start(streaming_env)
        provider.on_transcript("unchanged text", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        translation, source = controller.translation_queue.get_nowait()
        assert translation == "unchanged text"
        assert source is None

    def test_stream_opened_with_language_and_model(self, streaming_env):
        _controller, provider = self._start(streaming_env)
        # Default engine (gemini_realtime) with the default transcription
        # model — passed through as-is.
        assert provider.opened_with == {
            "model": "gemini-2.5-flash-native-audio-latest",
            "language": "ar",
        }

    def test_stream_opens_with_selected_deepgram_model(self, streaming_env):
        streaming_env.settings.transcription_provider = "deepgram"
        streaming_env.settings.transcription_model = "nova-2"
        _controller, provider = self._start(streaming_env)
        assert provider.opened_with == {"model": "nova-2", "language": "ar"}

    def test_stale_queues_drained_on_start(self, streaming_env):
        controller = streaming_env.controller
        controller._streaming_utterance_queue.put("stale from last session")
        controller._streaming_feed_queue.put(b"stale-audio")
        controller.translation_queue.put(("stale subtitle", None))
        self._start(streaming_env)
        assert controller._streaming_utterance_queue.empty()
        assert controller._streaming_feed_queue.empty()
        assert controller.translation_queue.empty()

    def test_stop_flushes_pending_text(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_transcript("unflushed tail", True)  # no utterance-end
        controller.stop(timeout=2.0)
        assert not controller.translation_queue.empty()
        translation, source = controller.translation_queue.get_nowait()
        assert translation == "XX:unflushed tail"
        assert source == "unflushed tail"

    def test_stop_closes_handle_and_clears_state(self, streaming_env):
        controller, provider = self._start(streaming_env)
        controller.stop(timeout=2.0)
        assert provider.handle.closed is True
        assert controller._streaming_handle is None
        assert controller._streaming_session is None
        assert controller._running is False

    def test_forced_flush_caps_continuous_speech(self, streaming_env, monkeypatch):
        """Speech without pauses never produces an utterance-end; the
        max-utterance cap must flush anyway."""
        monkeypatch.setattr(app_controller, "STREAMING_MAX_UTTERANCE_SECONDS", 0.3)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("continuous speech", True)
        assert _wait_for(lambda: not controller.translation_queue.empty(), timeout=3.0)
        translation, _source = controller.translation_queue.get_nowait()
        assert translation == "XX:continuous speech"

    def test_translate_error_shows_message_and_keeps_thread_alive(
        self, streaming_env, monkeypatch
    ):
        calls = {"n": 0}

        def flaky(text, context="", arabic_text=""):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return f"XX:{text}"

        monkeypatch.setattr(app_controller, "translate_text", flaky)
        controller, provider = self._start(streaming_env)

        provider.on_transcript("first", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        message, source = controller.translation_queue.get_nowait()
        assert message == "MSG:connection_error"
        assert source is None
        assert _wait_for(lambda: not controller.error_queue.empty())
        assert controller.error_queue.get_nowait() == "translation_error:boom"

        # The processor thread must survive and handle the next utterance
        provider.on_transcript("second", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        translation, _source = controller.translation_queue.get_nowait()
        assert translation == "XX:second"

    def test_stream_error_shows_connection_message(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_error(RuntimeError("socket dropped"))
        assert _wait_for(lambda: not controller.translation_queue.empty())
        message, source = controller.translation_queue.get_nowait()
        assert message == "MSG:connection_error"
        assert source is None
        assert controller.error_queue.get_nowait() == (
            "transcription_error:socket dropped"
        )

    def test_audio_chunks_reach_the_stream(self, streaming_env):
        controller, provider = self._start(streaming_env)
        controller._streaming_feed_queue.put(b"chunk-1")
        assert _wait_for(lambda: provider.handle.fed == [b"chunk-1"])


class TestStreamingEngineSelection:
    """The streaming engine follows transcription_provider (Deepgram or
    OpenAI Realtime); key checks, model resolution and the capture sample
    rate are all engine-specific."""

    def test_openai_realtime_resolves_openai_model(self, streaming_env):
        streaming_env.settings.transcription_provider = "openai_realtime"
        streaming_env.controller.start(input_device=0)
        assert streaming_env.provider.opened_with == {
            "model": "gpt-4o-transcribe",
            "language": "ar",
        }

    def test_openai_realtime_rejects_stale_deepgram_model(self, streaming_env):
        streaming_env.settings.transcription_provider = "openai_realtime"
        streaming_env.settings.transcription_model = "nova-3"
        streaming_env.controller.start(input_device=0)
        assert streaming_env.provider.opened_with["model"] == "gpt-4o-transcribe"

    def test_gemini_realtime_resolves_gemini_model(self, streaming_env):
        streaming_env.settings.transcription_provider = "gemini_realtime"
        streaming_env.controller.start(input_device=0)
        assert streaming_env.provider.opened_with == {
            "model": "gemini-2.5-flash-native-audio-latest",
            "language": "ar",
        }

    def test_key_check_follows_engine(self, streaming_env, monkeypatch):
        """Each realtime engine must demand its own provider's key."""
        checked = []
        monkeypatch.setattr(
            app_controller, "has_usable_key", lambda p: checked.append(p) or True
        )
        streaming_env.settings.transcription_provider = "openai_realtime"
        streaming_env.controller.start(input_device=0)
        assert checked == ["openai"]
        streaming_env.controller.stop(timeout=1.0)

        streaming_env.settings.transcription_provider = "gemini_realtime"
        streaming_env.controller.start(input_device=0)
        assert checked == ["openai", "gemini"]

    def test_missing_openai_key_rejected_before_side_effects(
        self, streaming_env, monkeypatch
    ):
        monkeypatch.setattr(app_controller, "has_usable_key", lambda p: False)
        streaming_env.settings.transcription_provider = "openai_realtime"
        with pytest.raises(ValueError, match="OpenAI API key"):
            streaming_env.controller.start(input_device=0)
        streaming_env.context_mgr.start.assert_not_called()

    def test_capture_rate_follows_engine(self, streaming_env, monkeypatch):
        """The OpenAI Realtime API only accepts 24 kHz PCM; Deepgram keeps
        the pipeline-wide 16 kHz FS."""
        rates = []

        class RecordingInputStream(FakeInputStream):
            def __init__(self, **kwargs):
                rates.append(kwargs.get("samplerate"))

        monkeypatch.setattr(
            app_controller, "sd", SimpleNamespace(InputStream=RecordingInputStream)
        )
        streaming_env.settings.transcription_provider = "openai_realtime"
        streaming_env.controller.start(input_device=0)
        assert _wait_for(lambda: rates == [24000])
        streaming_env.controller.stop(timeout=1.0)

        streaming_env.settings.transcription_provider = "deepgram"
        streaming_env.controller.start(input_device=0)
        assert _wait_for(lambda: rates == [24000, app_controller.FS])

    def test_wasapi_auto_convert_reaches_the_opened_stream(
        self, streaming_env, monkeypatch
    ):
        opened_with = []

        class FakeWasapiSettings:
            def __init__(self, *, auto_convert=False):
                self.auto_convert = auto_convert

        class RecordingInputStream(FakeInputStream):
            def __init__(self, **kwargs):
                opened_with.append(kwargs)

        fake_sd = SimpleNamespace(
            InputStream=RecordingInputStream,
            WasapiSettings=FakeWasapiSettings,
            query_devices=lambda index: {"hostapi": 0},
            query_hostapis=lambda: [{"name": "Windows WASAPI"}],
        )
        monkeypatch.setattr(app_controller, "sd", fake_sd)
        streaming_env.settings.transcription_provider = "openai_realtime"

        streaming_env.controller.start(input_device=21)

        assert len(opened_with) == 1
        assert opened_with[0]["samplerate"] == 24000
        assert opened_with[0]["extra_settings"].auto_convert is True

    def test_audio_open_failure_rolls_back_before_live_state(
        self, streaming_env, monkeypatch
    ):
        instances = []

        class FailingInputStream(FakeInputStream):
            def __init__(self, **kwargs):
                self.closed = False
                instances.append(self)

            def start(self):
                raise RuntimeError("microphone open failed")

            def close(self):
                self.closed = True

        monkeypatch.setattr(
            app_controller,
            "sd",
            SimpleNamespace(InputStream=FailingInputStream),
        )

        with pytest.raises(RuntimeError, match="microphone open failed"):
            streaming_env.controller.start(input_device=9)

        assert streaming_env.controller._running is False
        assert streaming_env.controller._input_thread is None
        assert streaming_env.provider.handle.closed is True
        streaming_env.context_mgr.start.assert_not_called()
        assert len(instances) == app_controller.INPUT_STREAM_OPEN_ATTEMPTS
        assert all(instance.closed for instance in instances)

    def test_transient_audio_start_failure_is_closed_and_retried(
        self, streaming_env, monkeypatch
    ):
        instances = []

        class FlakyInputStream(FakeInputStream):
            def __init__(self, **kwargs):
                self.closed = False
                instances.append(self)

            def start(self):
                if len(instances) == 1:
                    raise RuntimeError("WdmSyncIoctl element not found")
                return self

            def close(self):
                self.closed = True

        monkeypatch.setattr(
            app_controller,
            "sd",
            SimpleNamespace(InputStream=FlakyInputStream),
        )

        streaming_env.controller.start(input_device=21)

        assert len(instances) == 2
        assert instances[0].closed is True
        assert instances[1].closed is False
        assert streaming_env.provider.open_count == 1
        streaming_env.context_mgr.start.assert_called_once()

        streaming_env.controller.stop(timeout=1.0)
        assert instances[1].closed is True


class TestLiveTranscript:
    """get_live_transcript() feeds the subtitle window's live line (Realtime
    mode) as (text, settled): interims appear immediately as in-progress,
    the settled text survives (recolored "finished") until its translation
    is emitted, then clears."""

    def _start(self, env):
        env.controller.start(input_device=0)
        assert env.provider.on_transcript is not None
        return env.controller, env.provider

    def test_empty_when_never_started(self, streaming_env):
        assert streaming_env.controller.get_live_transcript() == ("", False)

    def test_interim_visible_immediately(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_transcript("in-progress hypo", False)
        assert controller.get_live_transcript() == ("in-progress hypo", False)

    def test_finals_and_interim_joined(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_transcript("part one", True)
        provider.on_transcript("part two dra", False)
        assert controller.get_live_transcript() == ("part one part two dra", False)

    def test_settled_during_translation_then_cleared(self, streaming_env, monkeypatch):
        import threading

        release = threading.Event()

        def slow_translate(text, context="", arabic_text=""):
            release.wait(timeout=2.0)
            return f"XX:{text}"

        monkeypatch.setattr(app_controller, "translate_text", slow_translate)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("some speech", True)
        provider.on_utterance_end()
        # While the translation is in flight the line reads settled (the GUI
        # turns it to the primary color in place — "finished").
        assert controller.get_live_transcript() == ("some speech", True)
        release.set()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        assert _wait_for(lambda: controller.get_live_transcript() == ("", False))

    def test_newer_speech_survives_translation_clear(self, streaming_env, monkeypatch):
        import threading

        release = threading.Event()

        def slow_translate(text, context="", arabic_text=""):
            release.wait(timeout=2.0)
            return f"XX:{text}"

        monkeypatch.setattr(app_controller, "translate_text", slow_translate)
        controller, provider = self._start(streaming_env)

        provider.on_transcript("first utterance", True)
        provider.on_utterance_end()
        # While the (slow) translation runs, the next utterance starts
        provider.on_transcript("second utter", False)
        release.set()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        # The live line must still show the newer speech, not be blanked
        assert controller.get_live_transcript() == ("second utter", False)

    def test_cleared_after_error_subtitle(self, streaming_env, monkeypatch):
        def boom(text, context="", arabic_text=""):
            raise RuntimeError("boom")

        monkeypatch.setattr(app_controller, "translate_text", boom)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("doomed speech", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        assert _wait_for(lambda: controller.get_live_transcript() == ("", False))

    def test_stream_error_clears_live_text(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_transcript("mid-sentence hypo", False)
        provider.on_error(RuntimeError("socket dropped"))
        assert controller.get_live_transcript() == ("", False)

    def test_empty_again_after_stop(self, streaming_env):
        controller, provider = self._start(streaming_env)
        provider.on_transcript("tail", False)
        controller.stop(timeout=2.0)
        assert controller.get_live_transcript() == ("", False)


class TestStreamingCoalescing:
    """Short utterances (rhetorical-pause fragments) are held and merged so
    GPT translates a whole clause, not "Sack."/"Das Licht." in isolation —
    and one merged call replaces several full-prompt ones."""

    def _start(self, env):
        env.controller.start(input_device=0)
        assert env.provider.on_transcript is not None
        return env.controller, env.provider

    def test_short_utterances_merge_into_one_call(self, streaming_env, monkeypatch):
        # Long hold so the first waits for the second; low min-words so their
        # merge crosses the flush threshold.
        monkeypatch.setattr(app_controller, "STREAMING_COALESCE_HOLD_SECONDS", 5.0)
        monkeypatch.setattr(app_controller, "STREAMING_COALESCE_MIN_WORDS", 4)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("alpha beta", True)
        provider.on_utterance_end()  # 2 words: held
        provider.on_transcript("gamma delta", True)
        provider.on_utterance_end()  # merged -> 4 words -> flush
        assert _wait_for(lambda: not controller.translation_queue.empty())
        translation, source = controller.translation_queue.get_nowait()
        assert translation == "XX:alpha beta gamma delta"
        assert source == "alpha beta gamma delta"
        time.sleep(0.1)
        assert controller.translation_queue.empty()  # ONE call, not two
        streaming_env.context_mgr.add_transcription.assert_called_once()

    def test_trailing_short_utterance_flushes_after_hold(
        self, streaming_env, monkeypatch
    ):
        monkeypatch.setattr(app_controller, "STREAMING_COALESCE_HOLD_SECONDS", 0.05)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("lonely clause", True)
        provider.on_utterance_end()  # 2 words, no follow-up
        assert _wait_for(lambda: not controller.translation_queue.empty(), timeout=2.0)
        assert controller.translation_queue.get_nowait()[0] == "XX:lonely clause"

    def test_long_utterance_flushes_immediately(self, streaming_env, monkeypatch):
        # Hold long enough that only an immediate (>= min-words) flush can pass.
        monkeypatch.setattr(app_controller, "STREAMING_COALESCE_HOLD_SECONDS", 30.0)
        monkeypatch.setattr(app_controller, "STREAMING_COALESCE_MIN_WORDS", 3)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("one two three four", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty(), timeout=1.0)
        assert controller.translation_queue.get_nowait()[0] == "XX:one two three four"

    def test_fragment_utterance_dropped_not_translated(
        self, streaming_env, monkeypatch
    ):
        monkeypatch.setattr(app_controller, "STREAMING_COALESCE_HOLD_SECONDS", 0.05)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("م", True)  # sub-word fragment
        provider.on_utterance_end()
        time.sleep(0.4)  # past the hold
        assert controller.translation_queue.empty()  # never went to GPT
        assert controller.get_live_transcript() == ("", False)  # live line cleared


class TestStreamingReconnect:
    """A dead streaming connection reconnects with backoff instead of
    staying dead until Stop → Start (the former Phase 1 limitation)."""

    def _start(self, env, monkeypatch):
        # Real backoff is 1s+ — compress it so tests run in milliseconds.
        monkeypatch.setattr(app_controller, "STREAMING_RECONNECT_BASE_SECONDS", 0.02)
        monkeypatch.setattr(app_controller, "STREAMING_RECONNECT_MAX_SECONDS", 0.1)
        env.controller.start(input_device=0)
        assert env.provider.on_transcript is not None
        return env.controller, env.provider

    def test_reconnects_after_stream_error(self, streaming_env, monkeypatch):
        controller, provider = self._start(streaming_env, monkeypatch)
        first_handle = provider.handle
        provider.on_error(RuntimeError("stream ended by server"))
        assert _wait_for(lambda: provider.open_count == 2)
        assert first_handle.closed is True
        # The feeder routes to the fresh handle.
        assert _wait_for(lambda: controller._streaming_handle is provider.handle)
        controller._streaming_feed_queue.put(b"\x01\x00")
        assert _wait_for(lambda: provider.handle.fed == [b"\x01\x00"])

    def test_invalid_api_key_is_terminal_without_reconnect_or_audience_message(
        self, streaming_env, monkeypatch
    ):
        controller, provider = self._start(streaming_env, monkeypatch)
        first_handle = provider.handle
        callback = provider.on_error

        callback(RuntimeError("HTTP 401 invalid_api_key"))

        assert controller.error_queue.get_nowait() == (
            "fatal_transcription_error:invalid_api_key"
        )
        assert first_handle.closed is True
        assert controller.translation_queue.empty()
        time.sleep(0.15)
        assert provider.open_count == 1

        # A close can trigger a second callback from the same socket. The
        # terminal event is idempotent and must not stack in the GUI queue.
        callback(RuntimeError("HTTP 401 invalid_api_key"))
        assert controller.error_queue.empty()

    def test_bare_403_remains_transient(self, streaming_env, monkeypatch):
        controller, provider = self._start(streaming_env, monkeypatch)

        provider.on_error(RuntimeError("HTTP 403 model access denied"))

        assert _wait_for(lambda: provider.open_count == 2)
        assert controller.error_queue.get_nowait() == (
            "transcription_error:HTTP 403 model access denied"
        )
        assert controller.translation_queue.get_nowait() == (
            "MSG:connection_error",
            None,
        )

    def test_one_error_subtitle_per_outage(self, streaming_env, monkeypatch):
        """A disconnect can fire several error callbacks and retries — the
        audience sees ONE connection-error message until the stream proves
        alive again."""
        controller, provider = self._start(streaming_env, monkeypatch)
        cb = provider.on_error
        cb(RuntimeError("first error"))
        cb(RuntimeError("duplicate error from the same disconnect"))
        assert _wait_for(lambda: not controller.translation_queue.empty())
        assert controller.translation_queue.get_nowait() == (
            "MSG:connection_error",
            None,
        )
        time.sleep(0.1)
        assert controller.translation_queue.empty()

        # Proof of life ends the outage; the NEXT disconnect messages again.
        assert _wait_for(lambda: provider.open_count >= 2)
        provider.on_transcript("back alive", False)
        provider.on_error(RuntimeError("second outage"))
        assert _wait_for(lambda: not controller.translation_queue.empty())
        assert controller.translation_queue.get_nowait() == (
            "MSG:connection_error",
            None,
        )

    def test_stale_generation_error_ignored(self, streaming_env, monkeypatch):
        """A late callback from an already-replaced connection must not tear
        down the healthy new one."""
        controller, provider = self._start(streaming_env, monkeypatch)
        stale_cb = provider.on_error
        stale_cb(RuntimeError("stream ended by server"))
        assert _wait_for(lambda: provider.open_count == 2)
        stale_cb(RuntimeError("late duplicate from the dead connection"))
        time.sleep(0.15)  # would be enough for another (wrong) reconnect
        assert provider.open_count == 2
        assert controller._streaming_handle is provider.handle

    def test_no_reconnect_after_stop(self, streaming_env, monkeypatch):
        controller, provider = self._start(streaming_env, monkeypatch)
        cb = provider.on_error
        controller.stop(timeout=2.0)
        cb(RuntimeError("stream ended by server"))
        time.sleep(0.15)
        assert provider.open_count == 1

    def test_backoff_grows_and_resets_on_transcript(self, streaming_env, monkeypatch):
        controller, provider = self._start(streaming_env, monkeypatch)
        base = app_controller.STREAMING_RECONNECT_BASE_SECONDS
        provider.on_error(RuntimeError("stream ended by server"))
        assert _wait_for(lambda: provider.open_count == 2)
        assert controller._streaming_backoff > base
        provider.on_transcript("healthy again", False)
        assert controller._streaming_backoff == base


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

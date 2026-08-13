"""Controller-level tests for pipeline_mode="streaming" (P7).

These drive AppController's streaming path end to end with a faked Deepgram
provider and faked audio input: transcript callbacks → utterance
accumulation → translate_text → translation_queue, plus the start/stop
lifecycle guarantees (validation before side effects, no state carried between
sessions, final flush on stop, forced flush for continuous speech, error
recovery).

The runtime half lives in ``streaming_session.py`` (issue #48): the controller
builds one ``StreamingSession`` per ``start()`` and reaches it as
``controller._streaming``. Tests that pin an internal invariant poke the
session that owns the state rather than the controller.
"""

import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import app_controller
import streaming_session as streaming_session_module
from app_controller import AppController
from providers import resolve_streaming_transcription_model
from streaming_session import StreamingSession, UtteranceSession
from utils.settings import (
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    PIPELINE_MODE_STREAMING,
    Settings,
)


# 10 s, not the 2 s this started at, and it is not a latency assertion: the
# predicate is polled every 10 ms and returns the moment it is true, so a
# passing test costs exactly the same either way. The budget only has to cover
# the worst stall a loaded runner can impose on a cross-thread hand-off — and
# every path measured here runs through `log()`, which opens, appends to and
# closes the day's log file under one process-wide lock, synchronously, per
# line. test_translate_error_shows_message_and_keeps_thread_alive is the one
# with TWO such writes before its queue put (the INFO "Translation started"
# and then the ERROR branch), and it is the one that failed on 2 of 3
# consecutive hosted-Windows runs while passing 15/15 locally.
def _wait_for(predicate, timeout=10.0, interval=0.01):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class FakeStreamHandle:
    def __init__(self, *, can_commit=False):
        self.fed = []
        self.closed = False
        self.commit_count = 0
        self._can_commit = can_commit

    def feed(self, pcm_bytes):
        self.fed.append(pcm_bytes)

    def close(self):
        self.closed = True

    def commit_turn(self):
        # Default False mirrors Deepgram/Gemini, whose turns cannot outrun
        # their own endpointing; the OpenAI-shaped case opts in.
        if not self._can_commit:
            return False
        self.commit_count += 1
        return True


class FakeStreamingProvider:
    def __init__(self, *, can_commit=False):
        self._can_commit = can_commit
        self.handle = FakeStreamHandle(can_commit=can_commit)  # most recent handle
        self.open_count = 0
        self.opened_with = None
        self.on_transcript = None
        self.on_utterance_end = None
        self.on_error = None
        self.on_speech_activity = None

    def open_stream(
        self,
        *,
        model,
        language,
        on_transcript,
        on_utterance_end,
        on_error,
        on_speech_activity=None,
    ):
        self.open_count += 1
        if self.open_count > 1:
            # A reconnect gets a fresh handle, like a real re-opened socket.
            self.handle = FakeStreamHandle(can_commit=self._can_commit)
        self.opened_with = {"model": model, "language": language}
        self.on_transcript = on_transcript
        self.on_utterance_end = on_utterance_end
        self.on_error = on_error
        self.on_speech_activity = on_speech_activity
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
    # The streaming workers live in their own module and import these names
    # directly, so both modules need the fake.
    monkeypatch.setattr(
        streaming_session_module, "load_settings", lambda use_cache=True: settings
    )
    monkeypatch.setattr(
        streaming_session_module, "get_user_message", lambda key: f"MSG:{key}"
    )
    # Coalescing holds short utterances up to COALESCE_HOLD_SECONDS; compress
    # it so tests that emit a single short utterance flush in milliseconds
    # (the coalescing-specific tests override it back up).
    monkeypatch.setattr(
        streaming_session_module, "STREAMING_COALESCE_HOLD_SECONDS", 0.05
    )

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
        s = UtteranceSession()
        s.add_final("a")
        s.add_final("b")
        assert s.has_pending()
        text, _rev = s.take_and_reset()
        assert text == "a b"
        assert not s.has_pending()
        text, _rev = s.take_and_reset()
        assert text == ""

    def test_age_zero_when_empty(self):
        s = UtteranceSession()
        assert s.seconds_since_first_part() == 0.0

    def test_age_measured_from_first_part_not_last(self):
        """Continuous speech keeps adding finals; the forced-flush clock must
        run from the FIRST part or it would never fire."""
        s = UtteranceSession()
        s.add_final("first")
        time.sleep(0.05)
        s.add_final("second")
        assert s.seconds_since_first_part() >= 0.05

    def test_take_resets_age(self):
        s = UtteranceSession()
        s.add_final("first")
        s.take_and_reset()
        assert s.seconds_since_first_part() == 0.0


class TestSessionLiveText:
    def test_interim_publishes_live_text(self):
        s = UtteranceSession()
        s.set_interim("bismi")
        assert s.get_live_state() == ("bismi", False)

    def test_interim_corrects_itself(self):
        """Each interim replaces the previous hypothesis (self-correction)."""
        s = UtteranceSession()
        s.set_interim("bismi")
        s.set_interim("bismillah ar-rahman")
        assert s.get_live_state() == ("bismillah ar-rahman", False)

    def test_final_absorbs_interim_and_joins_parts(self):
        s = UtteranceSession()
        s.set_interim("part one draft")
        s.add_final("part one")
        s.set_interim("part two dra")
        assert s.get_live_state() == ("part one part two dra", False)

    def test_take_keeps_live_text_settled_until_cleared(self):
        """The finished source must stay visible during the translation call,
        marked settled so the GUI recolors it in place ("finished")."""
        s = UtteranceSession()
        s.add_final("settled text")
        _text, rev = s.take_and_reset()
        assert s.get_live_state() == ("settled text", True)
        s.clear_live_if_unchanged(rev)
        assert s.get_live_state() == ("", False)

    def test_new_speech_resets_settled(self):
        """A pipelined next utterance takes over the line as in-progress."""
        s = UtteranceSession()
        s.add_final("first utterance")
        s.take_and_reset()
        s.set_interim("second utter")
        assert s.get_live_state() == ("second utter", False)

    def test_clear_skipped_when_newer_speech_arrived(self):
        """A pipelined next utterance must not be blanked when the previous
        utterance's translation lands."""
        s = UtteranceSession()
        s.add_final("first utterance")
        _text, rev = s.take_and_reset()
        s.set_interim("second utter")  # newer speech during translation
        s.clear_live_if_unchanged(rev)
        assert s.get_live_state() == ("second utter", False)

    def test_clear_live_is_unconditional(self):
        s = UtteranceSession()
        s.set_interim("anything")
        s.clear_live()
        assert s.get_live_state() == ("", False)


class TestIntraTurnSentenceFlush:
    """Issue #26: a pauseless speaker produces one very long server-VAD turn,
    and translating only at its end shows nothing for a minute and then a wall
    of text. With ``sentence_flush`` on, finished sentences leave the running
    interim; the turn's final transcript then contributes only its tail.
    """

    @staticmethod
    def _session() -> UtteranceSession:
        return UtteranceSession(sentence_flush=True)

    def test_off_by_default_so_the_other_engines_are_unaffected(self):
        """Deepgram gets finals mid-turn (the 12 s cap already works) and its
        interims are revised hypotheses; Gemini Live cuts long turns in the
        provider. Neither may take this path."""
        s = UtteranceSession()
        assert s.set_interim("erster Satz. zweiter")[0] == ""
        s.add_final("erster Satz. zweiter Satz.")
        assert s.take_and_reset()[0] == "erster Satz. zweiter Satz."

    def test_only_openai_realtime_enables_it(self):
        assert "openai_realtime" in streaming_session_module._SENTENCE_FLUSH_PROVIDERS
        assert "deepgram" not in streaming_session_module._SENTENCE_FLUSH_PROVIDERS
        assert (
            "gemini_realtime" not in streaming_session_module._SENTENCE_FLUSH_PROVIDERS
        )

    def test_unfinished_interim_flushes_nothing(self):
        s = self._session()
        assert s.set_interim("der Redner spricht noch")[0] == ""

    def test_sentence_end_flushes_that_sentence_only(self):
        s = self._session()
        ready, _rev = s.set_interim("erster Satz. zweiter Sa")
        assert ready == "erster Satz."

    def test_arabic_question_mark_ends_a_sentence(self):
        s = self._session()
        ready, _rev = s.set_interim("أين الكتاب؟ وقال")
        assert ready == "أين الكتاب؟"

    def test_flush_cuts_at_the_last_terminator_not_the_first(self):
        """Several sentences arriving between two checks leave as one call."""
        s = self._session()
        ready, _rev = s.set_interim("eins. zwei. drei un")
        assert ready == "eins. zwei."

    def test_a_sentence_is_never_flushed_twice(self):
        s = self._session()
        s.set_interim("erster Satz. zweiter")
        assert s.set_interim("erster Satz. zweiter Satz noch")[0] == ""
        assert s.set_interim("erster Satz. zweiter Satz.")[0] == "zweiter Satz."

    def test_final_contributes_only_the_untranslated_tail(self):
        s = self._session()
        assert s.set_interim("erster Satz. der Rest")[0] == "erster Satz."
        s.add_final("erster Satz. der Rest davon")
        assert s.take_and_reset()[0] == "der Rest davon"

    def test_fully_flushed_turn_contributes_no_final(self):
        s = self._session()
        assert s.set_interim("alles gesagt.")[0] == "alles gesagt."
        s.add_final("alles gesagt.")
        assert s.take_and_reset()[0] == ""

    def test_fully_flushed_turn_leaves_the_settled_line_up(self):
        """Its last translation is still in flight — blanking the source here
        would take it off screen a second before the subtitle arrives."""
        s = self._session()
        _ready, rev = s.set_interim("alles gesagt.")
        s.add_final("alles gesagt.")
        s.take_and_reset()
        assert s.get_live_state() == ("alles gesagt.", True)
        s.clear_live_if_unchanged(rev)
        assert s.get_live_state() == ("", False)

    def test_next_turn_starts_from_zero(self):
        s = self._session()
        s.set_interim("erster Satz.")
        s.add_final("erster Satz.")
        s.take_and_reset()
        assert s.set_interim("zweiter Satz.")[0] == "zweiter Satz."

    def test_next_turn_may_repeat_the_previous_words(self):
        """A khutbah repeats phrases. Unless the emitted record is cleared at
        the turn boundary the repeat reads as a continuation of the last turn
        and its words are swallowed — the prefix guard cannot catch this one,
        because the text really does continue the prefix."""
        s = self._session()
        assert s.set_interim("الحمد لله.")[0] == "الحمد لله."
        s.add_final("الحمد لله.")
        s.take_and_reset()
        assert s.set_interim("الحمد لله. وبعد")[0] == "الحمد لله."

    def test_unpunctuated_speech_flushes_on_the_word_count_rule(self):
        """gpt-4o-transcribe usually punctuates; when it does not, the turn
        must not still grow without bound.

        All but the final word: that one is still being revised (see
        test_a_growing_tail_word_is_not_re_flushed)."""
        s = self._session()
        assert s.set_interim(" ".join(f"w{i}" for i in range(17)))[0] == ""
        long_text = " ".join(f"w{i}" for i in range(18))
        assert s.set_interim(long_text)[0] == " ".join(f"w{i}" for i in range(17))

    def test_a_growing_tail_word_is_not_re_flushed(self):
        """Live 2026-08-13: a reciter with no pauses put the same two ayat on
        screen three times inside 400 ms.

        Deltas arrive mid-word, so an interim ends on a fragment that the next
        message completes: يشعر → يشعرون → يشعرون. Emitting that fragment made
        each following interim fail the prefix check in _remainder_locked,
        which resets the record and re-flushes the WHOLE turn.
        """
        s = self._session()
        base = " ".join(f"w{i}" for i in range(17))
        first, _rev = s.set_interim(f"{base} yash")
        assert first == base  # the fragment is held back
        assert s.set_interim(f"{base} yashurun")[0] == ""  # revision: nothing re-sent
        assert s.set_interim(f"{base} yashurun.")[0] == "yashurun."  # terminator: tail
        s.add_final(f"{base} yashurun.")
        assert s.take_and_reset()[0] == ""  # every word left exactly once

    def test_the_held_back_word_still_leaves_when_the_turn_ends(self):
        """Holding a word back must not lose it if the turn ends right there —
        a missing word is a hole in the khutbah, which is the whole reason
        _remainder_locked errs toward duplication in the first place."""
        s = self._session()
        long_text = " ".join(f"w{i}" for i in range(18))
        assert s.set_interim(long_text)[0] == " ".join(f"w{i}" for i in range(17))
        s.add_final(long_text)
        assert s.take_and_reset()[0] == "w17"

    def test_word_count_rule_measures_the_untranslated_remainder(self):
        """Not the whole turn — otherwise every interim after the first flush
        would re-fire it and cut mid-clause."""
        s = self._session()
        assert s.set_interim("kurzer Satz.")[0] == "kurzer Satz."
        continued = "kurzer Satz. " + " ".join(f"w{i}" for i in range(5))
        assert s.set_interim(continued)[0] == ""

    def test_text_that_stops_continuing_the_prefix_is_translated_whole(self):
        """A new conversation item can open before the old one completes, and a
        completed transcript need not be the verbatim concatenation of its
        deltas. Losing a sentence is worse than repeating one."""
        s = self._session()
        assert s.set_interim("erster Satz. zweiter")[0] == "erster Satz."
        s.add_final("etwas ganz anderes")
        assert s.take_and_reset()[0] == "etwas ganz anderes"

    def test_live_line_keeps_the_whole_turn_while_it_runs(self):
        """It renders only its last wrapped row, so trimming the flushed part
        would show the same words and risk a blank row."""
        s = self._session()
        s.set_interim("erster Satz. zweiter Sa")
        assert s.get_live_state() == ("erster Satz. zweiter Sa", False)

    def test_flush_revision_loses_compare_and_clear_to_newer_speech(self):
        """The speaker talks on while the flushed sentence is translated: its
        clear must not blank the live line."""
        s = self._session()
        _ready, rev = s.set_interim("erster Satz. zwei")
        s.set_interim("erster Satz. zweiter Satz noch")
        s.clear_live_if_unchanged(rev)
        assert s.get_live_state() == ("erster Satz. zweiter Satz noch", False)


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
        # Names the key the DEFAULT engine needs, whichever engine that is.
        with pytest.raises(ValueError, match="API key"):
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
        # A session that never opened is never assigned to the controller.
        assert streaming_env.controller._streaming is None


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
        # Default engine with its default transcription model — passed
        # through as-is. Derived, not hardcoded, so flipping the default
        # engine does not break this.
        assert provider.opened_with == {
            "model": resolve_streaming_transcription_model(
                DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER, ""
            ),
            "language": "ar",
        }

    def test_stream_opens_with_selected_deepgram_model(self, streaming_env):
        streaming_env.settings.transcription_provider = "deepgram"
        streaming_env.settings.transcription_model = "nova-2"
        _controller, provider = self._start(streaming_env)
        assert provider.opened_with == {"model": "nova-2", "language": "ar"}

    def test_stale_state_is_not_carried_into_the_next_session(self, streaming_env):
        """An utterance flushed right as the previous session stopped must not
        be replayed into this one (possibly under a different language pair).
        The subtitle queue lives on the controller and is drained; the streaming
        queues die with the session that owned them."""
        controller = streaming_env.controller
        self._start(streaming_env)
        first = controller._streaming
        first._utterance_queue.put(("stale from last session", 0))
        first._feed_queue.put(b"stale-audio")
        controller.stop(timeout=2.0)

        controller.translation_queue.put(("stale subtitle", None))
        self._start(streaming_env)

        assert controller._streaming is not first
        assert controller._streaming._utterance_queue.empty()
        assert controller._streaming._feed_queue.empty()
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
        assert controller._streaming is None
        assert controller._running is False

    def test_forced_flush_caps_continuous_speech(self, streaming_env, monkeypatch):
        """Speech without pauses never produces an utterance-end; the
        max-utterance cap must flush anyway."""
        monkeypatch.setattr(streaming_session_module, "STREAMING_MAX_UTTERANCE_SECONDS", 0.3)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("continuous speech", True)
        assert _wait_for(lambda: not controller.translation_queue.empty())
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
        controller._streaming._feed_queue.put(b"chunk-1")
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
        monkeypatch.setattr(streaming_session_module, "STREAMING_COALESCE_HOLD_SECONDS", 5.0)
        monkeypatch.setattr(streaming_session_module, "STREAMING_COALESCE_MIN_WORDS", 4)
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
        monkeypatch.setattr(streaming_session_module, "STREAMING_COALESCE_HOLD_SECONDS", 0.05)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("lonely clause", True)
        provider.on_utterance_end()  # 2 words, no follow-up
        assert _wait_for(lambda: not controller.translation_queue.empty())
        assert controller.translation_queue.get_nowait()[0] == "XX:lonely clause"

    def test_long_utterance_flushes_immediately(self, streaming_env, monkeypatch):
        # Hold long enough that only an immediate (>= min-words) flush can pass.
        monkeypatch.setattr(streaming_session_module, "STREAMING_COALESCE_HOLD_SECONDS", 30.0)
        monkeypatch.setattr(streaming_session_module, "STREAMING_COALESCE_MIN_WORDS", 3)
        controller, provider = self._start(streaming_env)
        provider.on_transcript("one two three four", True)
        provider.on_utterance_end()
        # Kept explicit and well under the 30 s hold — here the budget IS the
        # assertion ("immediately, not after the hold"), so it cannot inherit
        # the default. 5 s only widens the room for a stalled runner.
        assert _wait_for(lambda: not controller.translation_queue.empty(), timeout=5.0)
        assert controller.translation_queue.get_nowait()[0] == "XX:one two three four"

    def test_fragment_utterance_dropped_not_translated(
        self, streaming_env, monkeypatch
    ):
        monkeypatch.setattr(streaming_session_module, "STREAMING_COALESCE_HOLD_SECONDS", 0.05)
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
        monkeypatch.setattr(streaming_session_module, "STREAMING_RECONNECT_BASE_SECONDS", 0.02)
        monkeypatch.setattr(streaming_session_module, "STREAMING_RECONNECT_MAX_SECONDS", 0.1)
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
        assert _wait_for(lambda: controller._streaming._handle is provider.handle)
        controller._streaming._feed_queue.put(b"\x01\x00")
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
        assert controller._streaming._handle is provider.handle

    def test_no_reconnect_after_stop(self, streaming_env, monkeypatch):
        controller, provider = self._start(streaming_env, monkeypatch)
        cb = provider.on_error
        controller.stop(timeout=2.0)
        cb(RuntimeError("stream ended by server"))
        time.sleep(0.15)
        assert provider.open_count == 1

    def test_device_change_during_an_outage_keeps_the_streaming_capture_path(
        self, streaming_env, monkeypatch
    ):
        """While an outage is being recovered the handle is None but the
        session is very much alive. Choosing the capture thread by handle
        started the *segmented* one here — filling a ring buffer nobody reads,
        so the reconnected stream stayed silent for the rest of the session."""
        controller, _provider = self._start(streaming_env, monkeypatch)
        started = []
        monkeypatch.setattr(
            controller,
            "_start_confirmed_input_thread",
            lambda target, args, **kwargs: started.append(target),
        )
        controller._streaming._handle = None  # mid-swap: the connection is down

        assert controller.change_input_device(1) is True
        assert started == [controller._streaming_input_stream_thread]

    def test_backoff_grows_and_resets_on_transcript(self, streaming_env, monkeypatch):
        controller, provider = self._start(streaming_env, monkeypatch)
        base = streaming_session_module.STREAMING_RECONNECT_BASE_SECONDS
        provider.on_error(RuntimeError("stream ended by server"))
        assert _wait_for(lambda: provider.open_count == 2)
        assert controller._streaming._backoff > base
        provider.on_transcript("healthy again", False)
        assert controller._streaming._backoff == base


class _CountingHandle:
    """A fake stream handle that records every open and close, so a test can
    assert that no opened connection is left dangling."""

    def __init__(self, opened: list, closed: list, lock: threading.Lock):
        self._closed = closed
        self._lock = lock
        with lock:
            opened.append(self)

    def feed(self, pcm_bytes: bytes) -> None:
        pass

    def close(self) -> None:
        with self._lock:
            self._closed.append(self)


def _bare_session(provider_id: str = "fake") -> StreamingSession:
    """A StreamingSession with no controller behind it, for the lock tests."""
    return StreamingSession(
        FakeStreamingProvider(),
        provider_id=provider_id,
        model="fake-model",
        language="ar",
        capture_rate=16000,
        stop_event=threading.Event(),
        stop_input=lambda: None,
        translation_queue=queue.Queue(),
        error_queue=queue.Queue(),
        translate=lambda text: None,
        on_activity=lambda: None,
    )


class TestIntraTurnFlushReachesTheQueue:
    """The session end of issue #26: an interim that finishes a sentence has to
    reach the same queue an ended utterance does, or nothing translates it."""

    def test_openai_session_queues_a_sentence_mid_turn(self):
        """The whole point of #26: a translation without an utterance end."""
        session = _bare_session(provider_id="openai_realtime")
        session._on_transcript("erster Satz. zweiter Sa", False)
        assert session._utterance_queue.get_nowait()[0] == "erster Satz."

    def test_deepgram_session_queues_nothing_mid_turn(self):
        session = _bare_session(provider_id="deepgram")
        session._on_transcript("erster Satz. zweiter Sa", False)
        assert session._utterance_queue.empty()

    def test_the_turn_end_still_queues_the_tail(self):
        session = _bare_session(provider_id="openai_realtime")
        session._on_transcript("erster Satz. der Rest", False)
        assert session._utterance_queue.get_nowait()[0] == "erster Satz."
        session._on_transcript("erster Satz. der Rest davon", True)
        session._on_utterance_end()
        assert session._utterance_queue.get_nowait()[0] == "der Rest davon"


class TestStreamingConnectionRaces:
    """The streaming connection handle is mutated from several threads (the
    reconnect supervisor, the stall watchdog, the terminal-error teardown and
    stop()). These drive those paths concurrently and assert the lock keeps a
    replaced connection from being orphaned — open and billed, feeding nothing.
    """

    def test_concurrent_swaps_leave_no_orphaned_connection(self):
        """Many threads enter _swap_connection at once (the supervisor and the
        watchdog can fire together). Every replaced connection must be closed
        and exactly the last one opened stays live."""
        session = _bare_session()
        opened: list = []
        closed: list = []
        record_lock = threading.Lock()

        def connect():
            handle = _CountingHandle(opened, closed, record_lock)
            # A real open_stream() does network I/O and releases the GIL; the
            # sleep reproduces that window so a missing lock would actually let
            # two swaps interleave and orphan a connection (without it the
            # critical section is too short to preempt under CPython).
            time.sleep(0.001)
            return handle

        session._connect = connect
        session._handle = connect()  # the initial live connection

        n = 24
        ready = threading.Barrier(n)

        def worker():
            ready.wait()  # release all threads together to force the overlap
            session._swap_connection("test")

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        # n swaps + 1 initial handle opened; the live one stays open and every
        # other opened connection was closed exactly once — none leaked.
        assert len(opened) == n + 1
        assert session._handle in opened
        survivors = [h for h in opened if h not in closed]
        assert survivors == [session._handle]
        assert len(closed) == n

    def test_stop_closes_a_connection_opened_during_shutdown(self, streaming_env):
        """Bug: a reconnect blocked in a slow open_stream() can finish opening a
        socket while stop() is tearing down. stop() takes the same lock before
        joining, so it captures and closes that just-opened handle instead of
        nulling the reference and leaking it."""
        controller = streaming_env.controller
        controller.start(input_device=0)
        session = controller._streaming

        entered = threading.Event()
        release = threading.Event()
        reconnect_opened: list[FakeStreamHandle] = []

        def slow_connect():
            entered.set()  # we are inside connect now, holding the streaming lock
            release.wait(timeout=2.0)
            handle = FakeStreamHandle()
            reconnect_opened.append(handle)
            return handle

        session._connect = slow_connect

        swap = threading.Thread(target=lambda: session._swap_connection("dead"))
        swap.start()
        assert entered.wait(timeout=2.0)  # reconnect is mid-open, holding the lock

        stopper = threading.Thread(target=lambda: controller.stop(timeout=2.0))
        stopper.start()
        time.sleep(0.05)  # let stop() reach the lock and block on the reconnect
        release.set()  # the reconnect finishes opening its socket

        swap.join(timeout=3.0)
        stopper.join(timeout=5.0)

        # The socket the reconnect opened during shutdown was closed, not leaked.
        assert len(reconnect_opened) == 1
        assert reconnect_opened[0].closed is True
        assert session._handle is None
        assert controller._streaming is None
        assert controller._running is False

    def test_feeder_survives_a_feed_error_from_a_closed_handle(self, streaming_env):
        """A swap/stop can close the live handle in the window between the
        feeder reading it and calling feed(); feed() on a closed connection then
        raises. The feeder must drop that chunk and keep running — a raised
        exception here would kill the thread and silence the pipeline for the
        rest of the session."""
        controller = streaming_env.controller
        controller.start(input_device=0)

        fed_attempted = threading.Event()

        class _RaisingHandle:
            def feed(self, pcm_bytes):
                fed_attempted.set()
                raise RuntimeError("connection already closed")

            def close(self):
                pass

        # The live handle now rejects every feed, as a just-closed socket would.
        controller._streaming._handle = _RaisingHandle()
        controller._streaming._feed_queue.put(b"doomed-chunk")
        assert fed_attempted.wait(timeout=2.0)  # the feeder hit the error path

        # A healthy handle takes over (as a reconnect would install one). The
        # feeder is still alive and routes the next chunk to it.
        fresh = FakeStreamHandle()
        controller._streaming._handle = fresh
        controller._streaming._feed_queue.put(b"good-chunk")
        assert _wait_for(lambda: fresh.fed == [b"good-chunk"])


class TestStallWatchdog:
    """The silent stall reconnect must only fire for a genuinely stuck
    session: armed by fed speech, cancelled by proof of life, and timed to a
    noise-gate pause. Observed live 2026-07-24: the unconditional version
    reconnected during every >15s quiet stretch, and because the feeder drops
    audio while the handle is down, speech resuming into the swap window lost
    its first words."""

    def _start(self, streaming_env, monkeypatch, *, timeout=0.3, grace=0.4):
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_TIMEOUT_SECONDS", timeout
        )
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_MIN_SPEECH_SECONDS", 1.0
        )
        monkeypatch.setattr(streaming_session_module, "STREAMING_STALL_GRACE_SECONDS", grace)
        controller = streaming_env.controller
        controller.start(input_device=0)
        assert streaming_env.provider.open_count == 1
        return controller, streaming_env.provider

    def test_silence_only_never_reconnects(self, streaming_env, monkeypatch):
        """No transcripts because nothing transcribable was fed — a healthy
        connection is just as silent as a stuck one. The old watchdog
        reconnected here on every check cycle."""
        controller, provider = self._start(streaming_env, monkeypatch)
        controller._streaming._speech_fed_seconds = 0.0
        controller._streaming._last_activity = time.time() - 60  # way past timeout
        time.sleep(0.8)  # several watchdog polls at the shrunk timeout
        assert provider.open_count == 1

    def test_speech_with_no_transcripts_reconnects(self, streaming_env, monkeypatch):
        """The original stuck-session case stays covered: speech was fed, no
        transcript arrived, the gate reports a pause — swap immediately."""
        controller, provider = self._start(streaming_env, monkeypatch)
        controller._streaming._noise_gate = SimpleNamespace(is_zeroing=True)
        controller._streaming._speech_fed_seconds = 5.0
        controller._streaming._last_activity = time.time() - 60
        assert _wait_for(lambda: provider.open_count == 2)
        # Re-armed fresh: the counted speech died with the old connection.
        assert controller._streaming._speech_fed_seconds == 0.0

    def test_transcript_during_grace_cancels_the_swap(
        self, streaming_env, monkeypatch
    ):
        """Speech resumed just before the check: the engine is alive and its
        transcript merely in flight. The grace wait must catch it and keep
        the connection (the live-observed harm case)."""
        controller, provider = self._start(streaming_env, monkeypatch, grace=1.2)
        controller._streaming._noise_gate = SimpleNamespace(is_zeroing=False)  # speech flowing
        controller._streaming._speech_fed_seconds = 5.0
        controller._streaming._last_activity = time.time() - 60
        time.sleep(0.3)  # let the watchdog arm and enter the grace wait
        provider.on_transcript("proof of life", False)  # interim
        time.sleep(1.8)  # past where the swap would have happened
        assert provider.open_count == 1

    def test_grace_expiry_still_recovers_a_stuck_session(
        self, streaming_env, monkeypatch
    ):
        """Continuous speech with zero transcripts is the original bug — the
        gate never closes, so the swap must proceed once the grace runs out."""
        controller, provider = self._start(streaming_env, monkeypatch, grace=0.3)
        controller._streaming._noise_gate = SimpleNamespace(is_zeroing=False)
        controller._streaming._speech_fed_seconds = 5.0
        controller._streaming._last_activity = time.time() - 60
        assert _wait_for(lambda: provider.open_count == 2)

    def test_feeder_accumulates_speech_seconds(self, streaming_env, monkeypatch):
        """The feeder converts fed bytes to seconds at the capture rate, and a
        transcript resets the count."""
        # Production timeout: the watchdog must not fire mid-test.
        controller, provider = self._start(streaming_env, monkeypatch, timeout=15.0)
        assert controller._streaming._speech_fed_seconds == 0.0
        rate = controller._streaming.capture_rate
        controller._streaming._feed_queue.put(b"\x00\x00" * rate)  # 1 s of int16
        assert _wait_for(
            lambda: abs(controller._streaming._speech_fed_seconds - 1.0) < 1e-6
        )
        provider.on_transcript("text arrived", True)
        assert controller._streaming._speech_fed_seconds == 0.0


class TestStallWatchdogRespectsAnOpenTurn:
    """Measured live 2026-08-12: 53 stall reconnects, zero connection errors,
    and a 128 s hole in a khutbah where five consecutive reconnects each
    destroyed the turn the previous one was waiting on. A speaker the
    server-side VAD never hears pause produces one endless turn, and a turn
    yields no transcript until it commits — so "no transcript" was reading a
    working engine as a dead one. While a turn is open the fix commits it
    (keeping the audio) instead of reopening the connection (discarding it)."""

    def _start(
        self, streaming_env, monkeypatch, *, timeout=0.3, grace=0.4, commit=0.2
    ):
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_TIMEOUT_SECONDS", timeout
        )
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_TURN_COMMIT_SECONDS", commit
        )
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_MIN_SPEECH_SECONDS", 1.0
        )
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_GRACE_SECONDS", grace
        )
        controller = streaming_env.controller
        controller.start(input_device=0)
        return controller, streaming_env.provider

    def _stall_with_open_turn(self, controller, provider):
        """Drive the session into the exact live failure state: speech flowing,
        a turn open server-side, no transcript for longer than the timeout."""
        session = controller._streaming
        session._noise_gate = SimpleNamespace(is_zeroing=False)  # speech flowing
        provider.on_speech_activity(True)  # server VAD: turn opened
        session._speech_fed_seconds = 5.0
        session._last_activity = time.time() - 60

    def test_open_turn_is_committed_instead_of_reconnected(
        self, streaming_env, monkeypatch
    ):
        controller, provider = self._start(streaming_env, monkeypatch)
        controller._streaming._handle._can_commit = True
        self._stall_with_open_turn(controller, provider)
        assert _wait_for(lambda: provider.handle.commit_count == 1)
        # The whole point: the connection — and the buffered speech on it —
        # survives. The old watchdog reopened here and lost the turn.
        assert provider.open_count == 1
        assert not provider.handle.closed

    def test_provider_without_commit_still_reconnects(
        self, streaming_env, monkeypatch
    ):
        """Deepgram/Gemini report no speech activity and cannot commit; their
        recovery must stay exactly what it was."""
        controller, provider = self._start(streaming_env, monkeypatch)
        assert controller._streaming._handle._can_commit is False
        self._stall_with_open_turn(controller, provider)
        assert _wait_for(lambda: provider.open_count == 2)

    def test_a_commit_that_produces_nothing_escalates_to_a_swap(
        self, streaming_env, monkeypatch
    ):
        """A commit the engine never answers is the genuinely dead connection.
        Without this escalation the turn flag would disarm the watchdog for the
        rest of the session and the socket would never be recovered."""
        controller, provider = self._start(streaming_env, monkeypatch)
        controller._streaming._handle._can_commit = True
        self._stall_with_open_turn(controller, provider)
        assert _wait_for(lambda: provider.handle.commit_count == 1)
        # No transcript follows. Still stalled: now it really is stuck, so
        # recover the old way rather than commit into the void again.
        controller._streaming._speech_fed_seconds = 5.0
        controller._streaming._last_activity = time.time() - 60
        assert _wait_for(lambda: provider.open_count == 2)
        assert provider.handle.commit_count == 0  # the fresh handle, not re-committed

    def test_an_answered_commit_commits_again_instead_of_swapping(
        self, streaming_env, monkeypatch
    ):
        """Measured live 2026-08-13: clearing the turn flag on a *successful*
        commit turned every second stall into a swap, because a server VAD that
        never heard the speech stop never sends a fresh speech-started to set
        the flag again. Commit, 21 s, reconnect, 16 s, commit — five cycles
        40.4 s apart, half of them tearing down a connection that answered
        every commit within 0.6 s, at 2-3 ayat of lost recitation each."""
        controller, provider = self._start(streaming_env, monkeypatch)
        controller._streaming._handle._can_commit = True
        self._stall_with_open_turn(controller, provider)
        assert _wait_for(lambda: provider.handle.commit_count == 1)
        provider.on_transcript("the committed turn came back", True)
        # The same unbroken turn stalls again, with no new speech-started.
        controller._streaming._speech_fed_seconds = 5.0
        controller._streaming._last_activity = time.time() - 60
        assert _wait_for(lambda: provider.handle.commit_count == 2)
        assert provider.open_count == 1
        assert not provider.handle.closed

    def test_an_open_turn_commits_on_its_own_clock(self, streaming_env, monkeypatch):
        """Display latency and dead-socket detection are different questions,
        and were the same number until 2026-08-13. A turn is committed after
        TURN_COMMIT_SECONDS so subtitles appear; the connection is only given
        up on after the much longer stall timeout."""
        controller, provider = self._start(
            streaming_env, monkeypatch, timeout=30.0, commit=0.2
        )
        session = controller._streaming
        session._handle._can_commit = True
        session._noise_gate = SimpleNamespace(is_zeroing=False)
        provider.on_speech_activity(True)
        session._speech_fed_seconds = 5.0
        session._last_activity = time.time() - 1.0  # only the commit clock expired
        assert _wait_for(lambda: provider.handle.commit_count == 1)
        assert provider.open_count == 1

    def test_a_refused_commit_does_not_swap_before_the_stall_timeout(
        self, streaming_env, monkeypatch
    ):
        """The commit clock must not become a second, faster death sentence.
        Deepgram and Gemini refuse every commit; they may not be reconnected
        six seconds into a turn because of it."""
        controller, provider = self._start(
            streaming_env, monkeypatch, timeout=30.0, commit=0.2, grace=0.1
        )
        session = controller._streaming
        assert session._handle._can_commit is False
        session._noise_gate = SimpleNamespace(is_zeroing=False)
        provider.on_speech_activity(True)
        session._speech_fed_seconds = 5.0
        session._last_activity = time.time() - 1.0
        time.sleep(0.6)  # several poll cycles past the commit clock
        assert provider.open_count == 1
        # Past the stall timeout it recovers exactly as it always did.
        session._last_activity = time.time() - 60
        assert _wait_for(lambda: provider.open_count == 2)

    def test_speech_activity_is_proof_of_life(self, streaming_env, monkeypatch):
        """Both VAD edges restart the stall clock: a message from the engine is
        a message from the engine, whichever edge it reports."""
        controller, provider = self._start(
            streaming_env, monkeypatch, timeout=15.0, commit=15.0
        )
        session = controller._streaming
        for edge in (True, False):
            session._last_activity = time.time() - 60
            session._speech_fed_seconds = 5.0
            provider.on_speech_activity(edge)
            assert time.time() - session._last_activity < 1.0
            assert session._speech_fed_seconds == 0.0

    def test_a_reopened_connection_starts_with_no_turn_open(
        self, streaming_env, monkeypatch
    ):
        """The flag belongs to a connection, not to the session. A swap that
        inherited it would disarm the watchdog on the fresh socket."""
        controller, provider = self._start(streaming_env, monkeypatch)
        self._stall_with_open_turn(controller, provider)
        assert _wait_for(lambda: provider.open_count == 2)
        assert controller._streaming._turn_active is False


class TestSwapFlushesPendingText:
    def test_transcribed_text_leaves_before_the_connection_is_replaced(
        self, streaming_env, monkeypatch
    ):
        """Text the dying connection already transcribed must be translated as
        its own unit. Left in the accumulator it survives, but only to be
        welded onto the *next* connection's first turn — one subtitle built
        from speech on either side of an outage."""
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_TIMEOUT_SECONDS", 0.3
        )
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_MIN_SPEECH_SECONDS", 1.0
        )
        monkeypatch.setattr(
            streaming_session_module, "STREAMING_STALL_GRACE_SECONDS", 0.1
        )
        controller = streaming_env.controller
        controller.start(input_device=0)
        provider = streaming_env.provider

        provider.on_transcript("said before the outage", True)
        controller._streaming._noise_gate = SimpleNamespace(is_zeroing=True)
        controller._streaming._speech_fed_seconds = 5.0
        controller._streaming._last_activity = time.time() - 60

        assert _wait_for(lambda: provider.open_count == 2)
        assert _wait_for(lambda: not controller.translation_queue.empty())
        translation, _source = controller.translation_queue.get_nowait()
        assert translation == "XX:said before the outage"

        # And the new connection's first turn is its own subtitle, not a
        # concatenation with what came before the swap.
        provider.on_transcript("said after the outage", True)
        provider.on_utterance_end()
        assert _wait_for(lambda: not controller.translation_queue.empty())
        translation, _source = controller.translation_queue.get_nowait()
        assert translation == "XX:said after the outage"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

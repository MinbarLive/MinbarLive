"""Control-panel (gui/app_gui.py) drive-through tests.

Until now nothing in the suite imported ``gui.app_gui``, so a green run said
nothing about the control panel — a missing import or a broken dropdown
reached users instead. These tests build a *real* AppGUI on a real Tk root
with a fake controller and drive its handlers the way a click would.

Isolation (what the fixture neutralises and why):
- ``load_settings``/``save_settings`` — never read or write the user's real
  settings.json.
- ``get_stored_api_key``/``has_usable_key``/``resolve_provider_by_keys`` — no
  OS keychain access, no key dialog popping up mid-test, and no dependence on
  which keys happen to be stored on the machine running the suite.
- ``check_for_updates=False`` — no network thread at startup.
- ``hide_subtitle_on_stop=True`` — never opens the fullscreen overlay.

Note there is deliberately no ``update()`` pump: a manual pump loop crashes
natively inside Tcl here. ``update_idletasks()`` is enough to settle layout,
and handlers are invoked directly, which is what a callback would do anyway.
"""

import queue
import sys
import time
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    PIPELINE_MODE_SEGMENTED,
    PIPELINE_MODE_STREAMING,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    Settings,
)


def _build_with_tk_retry(build, attempts: int = 3):
    """Build a Tk root, retrying a failed interpreter start-up.

    Creating an interpreter makes Tk source ~30 .tcl files from its library
    directory, and those reads intermittently fail on Windows with
    ``couldn't read file "...\\button.tcl": no such file or directory`` for a
    file that is plainly there — real-time virus scanning of the DLLs pytest
    has just imported is the likely culprit. Measured 2026-07-21: 0 failures
    in 342 roots when this file runs alone, but 1 in 20 when the whole suite
    is collected first, i.e. in the window right after that import burst.
    That is also where every observed failure landed, since this file runs
    first. No application code has run at that point, so the failure says
    nothing about the code under test — but it did fail whole suite runs
    (~1 in 3) and would do the same to CI. A second failure is re-raised.
    """
    for attempt in range(attempts):
        try:
            return build()
        except tk.TclError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def _display_available() -> bool:
    try:
        # Not just a skip guard: a transient failure here would silently skip
        # every test in this file and still report the run green.
        root = _build_with_tk_retry(tk.Tk)
    except Exception:
        return False
    root.destroy()
    return True


# The control panel needs a real display; skip rather than fail on headless CI.
pytestmark = pytest.mark.skipif(
    not _display_available(), reason="no display available for GUI tests"
)


class FakeController:
    """Stands in for AppController: queues plus the methods the GUI polls."""

    def __init__(self):
        self.translation_queue = queue.Queue()
        self.error_queue = queue.Queue()
        self.started = 0
        self.stopped = 0
        self.restarted = 0

    def start(self, input_device=None):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def restart(self, input_device=None):
        self.restarted += 1

    def get_live_transcript(self):
        return ("", False)

    def seconds_since_last_activity(self):
        return 0.0

    def change_input_device(self, idx):
        return True

    # ── Input-level meter / mic test ──────────────────────────────────────
    level_test_device = None
    level_test_running = False
    level_test_starts = 0
    level_test_stops = 0
    level_test_error = None

    def get_input_level(self):
        return None

    def is_input_level_test_running(self):
        return self.level_test_running

    def start_input_level_test(self, input_device=None):
        self.level_test_starts += 1
        if self.level_test_error is not None:
            raise self.level_test_error
        self.level_test_running = True
        self.level_test_device = input_device

    def stop_input_level_test(self, timeout=1.0):
        self.level_test_stops += 1
        self.level_test_running = False


@pytest.fixture
def make_gui(monkeypatch):
    """Build a real AppGUI over a Settings object the test controls."""
    import gui.app_gui as app_gui
    import gui.control_state as control_state

    built = []

    def _make(**overrides):
        settings = Settings()
        settings.onboarding_completed = True
        settings.disclaimer_accepted = True
        settings.hide_subtitle_on_stop = True  # no fullscreen overlay
        settings.check_for_updates = False  # no network thread
        settings.auto_start = False
        settings.window_geometry = ""
        for key, value in overrides.items():
            setattr(settings, key, value)

        monkeypatch.setattr(app_gui, "load_settings", lambda *a, **k: settings)
        monkeypatch.setattr(app_gui, "save_settings", lambda *a, **k: None)
        # No keychain reads, and never open the key dialog during a test.
        monkeypatch.setattr(app_gui, "get_stored_api_key", lambda _p: None)
        monkeypatch.setattr(app_gui, "has_usable_key", lambda _p: True)
        monkeypatch.setattr(app_gui, "set_api_key", lambda _k: None)
        # The provider-default repair re-resolves from the stored keys; without
        # pinning this, results would depend on which keys the machine running
        # the suite happens to have. The rule lives in gui.control_state, so
        # that is where it must be patched.
        monkeypatch.setattr(
            control_state, "resolve_provider_by_keys", lambda **k: DEFAULT_AI_PROVIDER
        )

        controller = FakeController()
        gui = _build_with_tk_retry(lambda: app_gui.AppGUI(controller))
        gui.update_idletasks()
        built.append(gui)
        return gui, controller, settings

    yield _make

    for gui in built:
        try:
            gui.report_callback_exception = lambda *a: None
            # on_close() already cancels every after() and calls quit() then
            # destroy(). Destroying a second time here corrupts the Tcl
            # interpreter for the rest of the session — the next root then
            # fails with 'invalid command name "tcl_findLibrary"'.
            gui.on_close()
        except Exception:
            pass
    # Dead roots linger in CTk's class-level ScalingTracker and make the next
    # set_widget_scaling() walk dead canvases (see _clear_stale_scaling_windows).
    app_gui._clear_stale_scaling_windows(force=True)


class TestStartup:
    def test_builds_without_error(self, make_gui):
        gui, _controller, _settings = make_gui()
        assert gui.winfo_exists()
        assert gui.title().startswith("MinbarLive")

    def test_starts_in_the_stopped_state(self, make_gui):
        gui, controller, _ = make_gui()
        assert gui._running is False
        assert controller.started == 0

    def test_display_clamp_is_applied_to_the_widget_scale(self, make_gui):
        """The DPI clamp must run before the layout: _responsive_scale is the
        base 0.86 times the fit factor, so it can only ever shrink."""
        gui, _c, _s = make_gui()
        assert 0 < gui._responsive_scale <= 0.86

    def test_no_subtitle_window_when_hidden_on_stop(self, make_gui):
        gui, _c, _s = make_gui()
        assert gui.subtitle_window is None

    def test_a_failed_tk_start_up_is_retried(self):
        """Guards the retry itself — see _build_with_tk_retry for why Tk's own
        library sourcing intermittently fails here."""
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) == 1:
                raise tk.TclError("Can't find a usable tk.tcl")
            return "root"

        assert _build_with_tk_retry(flaky) == "root"
        assert len(attempts) == 2

    def test_a_persistent_tk_failure_still_raises(self):
        """Retrying must not turn a genuinely broken Tk into a green run."""

        def always_fails():
            raise tk.TclError("no display")

        with pytest.raises(tk.TclError):
            _build_with_tk_retry(always_fails, attempts=2)


class TestProviderSelection:
    """The provider/model/strategy cluster — the most intricate logic in the
    control panel and the part most likely to break silently."""

    def test_switching_provider_repopulates_models_and_resets_default(
        self, make_gui
    ):
        gui, _c, settings = make_gui(ai_provider="gemini")
        target = gui._provider_ids.index("openai")

        gui.provider_combo.set(gui.provider_combo._values[target])
        gui._on_provider_change()

        assert settings.ai_provider == "openai"
        from providers import get_default_model

        assert settings.translation_model == get_default_model("openai", "translation")
        assert gui._model_ids  # dropdown repopulated for the new provider

    def test_provider_model_ids_belong_to_the_selected_provider(self, make_gui):
        # An EXPLICIT non-default provider: "Use default" must be off, or the
        # startup repair legitimately resets it to the default provider.
        gui, _c, _settings = make_gui(
            ai_provider="gemini", use_default_translation_model=False
        )
        from providers import get_model_choices

        expected = [mid for _n, mid in get_model_choices("gemini", "translation")]
        assert gui._model_ids == expected

    def test_selecting_the_same_provider_is_a_no_op(self, make_gui):
        gui, _c, settings = make_gui(
            ai_provider="gemini", use_default_translation_model=False
        )
        settings.translation_model = "a-deliberately-odd-model"
        idx = gui._provider_ids.index("gemini")

        gui.provider_combo.set(gui.provider_combo._values[idx])
        gui._on_provider_change()

        # Unchanged: re-selecting the current provider must not reset the model.
        assert settings.translation_model == "a-deliberately-odd-model"


class TestControlStateIsWiredUp:
    """The rules themselves are covered headlessly in test_control_state.py.
    What still needs a real window is that the panel is actually wired to
    them — that the delegation and the startup repair happen at all.
    """

    def test_required_key_providers_delegates(self, make_gui):
        """A realtime engine must resolve to the key provider it
        authenticates with, or Start re-prompts for a key the user already
        has (the reported 'openai_realtime API Key' dialog)."""
        gui, _c, _s = make_gui(
            ai_provider="openai",
            use_default_translation_model=False,
            transcription_provider="openai_realtime",
        )
        assert gui._required_key_providers() == ["openai"]


class _FakeOverlay:
    """Stands in for a live SubtitleWindow (building a real fullscreen overlay
    in a test is deliberately avoided — see the module docstring)."""

    def __init__(self):
        self.always_on_top_calls: list[bool] = []

    def winfo_exists(self):
        return True

    def set_always_on_top(self, enabled):
        self.always_on_top_calls.append(enabled)

    def destroy(self):
        pass


def _topmost(gui) -> bool:
    gui.update_idletasks()
    return bool(int(gui.attributes("-topmost")))


def _wm_reflects_topmost(win) -> bool:
    """Whether this display honors the -topmost attribute on read-back.

    On X11 -topmost is _NET_WM_STATE_ABOVE, which a window manager has to
    apply; a bare X server (xvfb in CI, no WM) accepts the set silently but
    reports 0 when read. Windows, macOS and any real Linux desktop round-trip
    it. Used to run the read-back assertions only where they can hold, while
    the always-on-top *decision* is still checked on every platform.
    """
    win.update_idletasks()
    prev = bool(int(win.attributes("-topmost")))
    win.attributes("-topmost", True)
    win.update_idletasks()
    reflected = bool(int(win.attributes("-topmost")))
    win.attributes("-topmost", prev)
    return reflected


class TestAlwaysOnTop:
    """The control panel floats above the subtitle overlay only while that
    overlay is open, and only if always_on_top is on. The checkbox toggles
    both windows live."""

    def test_not_topmost_while_no_overlay_open(self, make_gui):
        # hide_subtitle_on_stop=True (fixture default) => no overlay at startup.
        gui, _c, _s = make_gui()
        assert gui.subtitle_window is None
        assert gui._control_window_should_be_topmost() is False
        assert _topmost(gui) is False

    def test_topmost_while_overlay_open(self, make_gui):
        gui, _c, _s = make_gui()
        gui.subtitle_window = _FakeOverlay()
        gui._apply_control_window_topmost()
        assert gui._control_window_should_be_topmost() is True
        if _wm_reflects_topmost(gui):
            assert _topmost(gui) is True

    def test_toggle_off_drops_both_windows(self, make_gui):
        gui, _c, settings = make_gui()
        overlay = _FakeOverlay()
        gui.subtitle_window = overlay

        gui.always_on_top_var.set(False)
        gui._on_always_on_top_change()

        assert settings.always_on_top is False
        assert overlay.always_on_top_calls == [False]  # overlay told to drop
        assert gui._control_window_should_be_topmost() is False
        assert _topmost(gui) is False

    def test_toggle_back_on_restores_topmost_with_overlay(self, make_gui):
        gui, _c, settings = make_gui(always_on_top=False)
        gui.subtitle_window = _FakeOverlay()

        gui.always_on_top_var.set(True)
        gui._on_always_on_top_change()

        assert settings.always_on_top is True
        assert gui._control_window_should_be_topmost() is True
        if _wm_reflects_topmost(gui):
            assert _topmost(gui) is True

    def test_off_never_topmost_even_with_overlay(self, make_gui):
        gui, _c, _s = make_gui(always_on_top=False)
        gui.subtitle_window = _FakeOverlay()
        gui._apply_control_window_topmost()
        assert gui._control_window_should_be_topmost() is False
        assert _topmost(gui) is False

    def test_effective_subtitle_mode_delegates(self, make_gui):
        gui, _c, settings = make_gui(
            subtitle_mode=SUBTITLE_MODE_REALTIME,
            pipeline_mode=PIPELINE_MODE_SEGMENTED,
            transcription_provider="gemini",
        )
        assert gui._effective_subtitle_mode() == SUBTITLE_MODE_CONTINUOUS
        assert settings.subtitle_mode == SUBTITLE_MODE_REALTIME

    def test_subtitle_mode_dropdown_offers_realtime_only_while_streaming(
        self, make_gui
    ):
        gui, _c, _s = make_gui(
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider=DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
        )
        assert SUBTITLE_MODE_REALTIME in gui._subtitle_mode_values

    def test_provider_repair_runs_at_startup(self, make_gui):
        """The repair happens in __init__ before any widget exists — that
        ordering is a property of the window, not of the rule."""
        gui, _c, settings = make_gui(
            ai_provider="anthropic", use_default_translation_model=True
        )
        assert settings.ai_provider == DEFAULT_AI_PROVIDER
        assert gui._saved_settings.ai_provider == DEFAULT_AI_PROVIDER


class TestStartStop:
    def test_starting_does_not_change_the_card_heights(self, make_gui):
        """The "stop to change" hint shares the strategy label's line. As its
        own row it grew the Translation-flow card by ~24px on every Start,
        which pushed the Advanced card below the left column's bottom edge."""
        gui, _controller, _s = make_gui()
        gui.update_idletasks()
        stopped = gui.language_card.winfo_reqheight()

        gui.on_start()
        gui.update_idletasks()
        assert gui.strategy_running_hint.winfo_ismapped()
        assert gui.language_card.winfo_reqheight() == stopped

        gui.on_stop()
        gui.update_idletasks()
        assert gui.language_card.winfo_reqheight() == stopped

    def test_start_then_stop_drives_the_controller(self, make_gui):
        gui, controller, _s = make_gui()
        gui.on_start()
        assert controller.started == 1
        assert gui._running is True

        gui.on_stop()
        assert controller.stopped >= 1
        assert gui._running is False

    def test_start_is_blocked_when_a_key_is_missing(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, controller, _s = make_gui()
        monkeypatch.setattr(app_gui, "has_usable_key", lambda _p: False)
        monkeypatch.setattr(gui, "_prompt_provider_key", lambda _p: None)

        gui.on_start()

        assert controller.started == 0, "must not start without a usable key"
        assert gui._running is False

    def test_escape_on_the_overlay_stops_but_does_not_close(self, make_gui):
        gui, controller, _s = make_gui()
        gui.on_start()

        gui._request_stop_from_subtitle()

        assert controller.stopped >= 1
        assert gui.winfo_exists(), "Esc must never close the app"

    def test_escape_while_idle_does_nothing(self, make_gui):
        gui, controller, _s = make_gui()
        gui._request_stop_from_subtitle()
        assert controller.stopped == 0


class TestLocalizationAndTheme:
    def test_gui_language_switch_reloads_texts(self, make_gui):
        """The language dropdown lives in the settings window, and
        _on_gui_language_change reads it — so the window must be open for the
        handler to work at all."""
        gui, _c, settings = make_gui(gui_language="de")
        # "start" is one of only 8 keys (of 185) whose German and English text
        # is identical — assert on one that actually differs.
        assert gui.gui_texts.get("stopped") == "Gestoppt"

        gui._open_settings_window()
        gui.update_idletasks()
        gui.gui_lang_combo.set("English")
        gui._on_gui_language_change()

        assert settings.gui_language == "en"
        assert gui.gui_texts.get("stopped") == "Stopped"

    def test_settings_window_opens_once_and_is_reused(self, make_gui):
        gui, _c, _s = make_gui()
        gui._open_settings_window()
        first = gui._settings_win
        gui._open_settings_window()
        assert gui._settings_win is first, "a second open must not stack windows"

    def test_theme_switch_repaints_and_persists(self, make_gui):
        gui, _c, settings = make_gui(theme_mode="light")
        gui._on_theme_change("dark")
        assert settings.theme_mode == "dark"
        assert gui._theme_mode == "dark"

    def test_log_panel_toggle_round_trips(self, make_gui):
        gui, _c, settings = make_gui(log_panel_collapsed=True)
        gui._toggle_log_panel()
        assert settings.log_panel_collapsed is False
        gui._toggle_log_panel()
        assert settings.log_panel_collapsed is True


class _FakeSubtitleWindow:
    """Records the announcement/overlay calls the AppGUI drives, without
    opening a real fullscreen overlay."""

    def __init__(self):
        self.announcement = None
        self.stopped_hint = None
        self.destroyed = False

    def winfo_exists(self):
        return not self.destroyed

    def set_announcement(self, text):
        self.announcement = text

    def clear_announcement(self):
        self.announcement = ""

    def set_stopped_hint(self, visible):
        self.stopped_hint = visible

    def destroy(self):
        self.destroyed = True


class TestAnnouncement:
    """The megaphone announcement window + overlay lifecycle."""

    def _duration_label(self, gui, key):
        return gui.gui_texts[key]

    def test_send_shows_message_and_records_history(self, make_gui):
        gui, _c, settings = make_gui()
        gui._open_announce_window()
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake

        gui._announce_textbox.insert("1.0", "Please silence your phones")
        gui._announce_duration_combo.set(
            self._duration_label(gui, "announce_duration_until_stopped")
        )
        gui._send_announcement()

        assert fake.announcement == "Please silence your phones"
        assert gui._announcement_text_active == "Please silence your phones"
        assert gui._has_active_announcement() is True
        assert gui._announcement_until_stopped is True
        assert gui._announcement_job is None  # "until stopped" arms no timer
        assert settings.announcement_history[0] == "Please silence your phones"

    def test_timed_duration_arms_a_timer(self, make_gui):
        gui, _c, _s = make_gui()
        gui._open_announce_window()
        gui.subtitle_window = _FakeSubtitleWindow()
        gui._announce_textbox.insert("1.0", "Break for 10 minutes")
        gui._announce_duration_combo.set(
            self._duration_label(gui, "announce_duration_5m")
        )
        gui._send_announcement()
        assert gui._announcement_job is not None
        assert gui._announcement_until_stopped is False

    def test_empty_message_is_a_no_op(self, make_gui):
        gui, _c, settings = make_gui()
        gui._open_announce_window()
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._announce_textbox.insert("1.0", "   ")
        gui._send_announcement()
        assert fake.announcement is None
        assert gui._has_active_announcement() is False
        assert settings.announcement_history == []

    def test_send_replaces_the_previous_announcement(self, make_gui):
        gui, _c, _s = make_gui()
        gui._open_announce_window()
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake

        gui._announce_textbox.insert("1.0", "First")
        gui._announce_duration_combo.set(
            self._duration_label(gui, "announce_duration_until_stopped")
        )
        gui._send_announcement()

        gui._announce_textbox.delete("1.0", "end")
        gui._announce_textbox.insert("1.0", "Second")
        gui._send_announcement()

        assert fake.announcement == "Second"
        assert gui._announcement_text_active == "Second"

    def test_stop_clears_the_message(self, make_gui):
        gui, _c, _s = make_gui()
        gui._open_announce_window()
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._announce_textbox.insert("1.0", "Notice")
        gui._announce_duration_combo.set(
            self._duration_label(gui, "announce_duration_until_stopped")
        )
        gui._send_announcement()
        gui._stop_announcement()

        assert fake.announcement == ""
        assert gui._has_active_announcement() is False
        assert gui._announcement_job is None

    def test_history_dedupes_and_caps_at_three(self, make_gui):
        gui, _c, settings = make_gui()
        for text in ["a", "b", "c", "d", "e", "f", "a"]:
            gui._push_announcement_history(text)
        # Most-recent-first, deduped, capped at 3.
        assert settings.announcement_history == ["a", "f", "e"]

    def test_send_resizes_the_window(self, make_gui, monkeypatch):
        # Regression: sending used to repopulate the Recent list without
        # resizing, so newly added rows could render below the window's
        # bottom edge until it was closed and reopened.
        gui, _c, _s = make_gui()
        gui._open_announce_window()
        gui.subtitle_window = _FakeSubtitleWindow()
        calls = []
        monkeypatch.setattr(gui, "_resize_announce_window", lambda: calls.append(1))
        gui._announce_textbox.insert("1.0", "Please silence your phones")
        gui._send_announcement()
        assert calls

    def test_favorite_pins_text_and_removes_from_history(self, make_gui):
        gui, _c, settings = make_gui()
        gui._push_announcement_history("Please silence your phones")
        gui._favorite_announcement("Please silence your phones")
        assert settings.announcement_favorites == ["Please silence your phones"]
        assert settings.announcement_history == []

    def test_favorited_text_survives_history_rotation(self, make_gui):
        gui, _c, settings = make_gui()
        gui._push_announcement_history("Keep me")
        gui._favorite_announcement("Keep me")
        for text in ["a", "b", "c", "d", "e", "f"]:
            gui._push_announcement_history(text)
        assert settings.announcement_favorites == ["Keep me"]
        assert "Keep me" not in settings.announcement_history
        assert len(settings.announcement_history) == 3

    def test_unfavorite_does_not_restore_to_history(self, make_gui):
        gui, _c, settings = make_gui()
        gui._push_announcement_history("Notice")
        gui._favorite_announcement("Notice")
        gui._unfavorite_announcement("Notice")
        assert settings.announcement_favorites == []
        assert settings.announcement_history == []

    def test_sending_a_favorited_text_does_not_duplicate_into_history(
        self, make_gui
    ):
        gui, _c, settings = make_gui()
        gui._open_announce_window()
        gui.subtitle_window = _FakeSubtitleWindow()
        gui._favorite_announcement("Pinned reminder")
        gui._announce_textbox.insert("1.0", "Pinned reminder")
        gui._send_announcement()
        assert settings.announcement_history == []
        assert settings.announcement_favorites == ["Pinned reminder"]

    def test_favorites_reject_new_entry_once_full(self, make_gui, monkeypatch):
        # Once the cap is reached, favoriting one more DISTINCT text must be
        # refused (with a warning) rather than silently evicting the oldest
        # pin — that would defeat the point of pinning it.
        from config import ANNOUNCEMENT_FAVORITES_MAX

        gui, _c, settings = make_gui()
        alerts = []
        monkeypatch.setattr(
            gui, "_alert", lambda title, message, **k: alerts.append(message)
        )
        for i in range(ANNOUNCEMENT_FAVORITES_MAX):
            gui._favorite_announcement(f"msg{i}")
        assert len(settings.announcement_favorites) == ANNOUNCEMENT_FAVORITES_MAX
        assert alerts == []

        gui._favorite_announcement("one_too_many")
        assert alerts
        assert "one_too_many" not in settings.announcement_favorites
        assert len(settings.announcement_favorites) == ANNOUNCEMENT_FAVORITES_MAX

    def test_refavoriting_existing_entry_reorders_even_when_full(
        self, make_gui, monkeypatch
    ):
        from config import ANNOUNCEMENT_FAVORITES_MAX

        gui, _c, settings = make_gui()
        monkeypatch.setattr(gui, "_alert", lambda *a, **k: None)
        for i in range(ANNOUNCEMENT_FAVORITES_MAX):
            gui._favorite_announcement(f"msg{i}")
        # Re-favoriting an already-pinned text is a reorder, not a new
        # entry, so it is exempt from the full-list rejection.
        gui._favorite_announcement("msg0")
        assert settings.announcement_favorites[0] == "msg0"
        assert len(settings.announcement_favorites) == ANNOUNCEMENT_FAVORITES_MAX

    def test_favorites_section_hidden_when_empty_shown_after_favoriting(
        self, make_gui
    ):
        gui, _c, _s = make_gui()
        gui._open_announce_window()
        assert not gui._announce_favorites_frame.grid_info()
        gui._favorite_announcement("Pinned")
        assert gui._announce_favorites_frame.grid_info()

    def test_on_stop_keeps_overlay_when_announcement_active(self, make_gui):
        # hide_subtitle_on_stop=True normally destroys the overlay on stop, but
        # an active "until stopped" announcement survives the stop when the
        # announcement window's "hide when stopped" toggle is off.
        gui, _c, _s = make_gui(
            hide_subtitle_on_stop=True, stop_announcement_on_live_stop=False
        )
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui._announcement_text_active = "Stays up"
        gui.on_stop()
        assert gui.subtitle_window is fake
        assert fake.destroyed is False

    def test_on_stop_clears_announcement_when_toggle_is_on(self, make_gui):
        # Default: stopping the session also clears an in-progress
        # announcement, which then lets hide-on-stop tear the overlay down.
        gui, _c, settings = make_gui(hide_subtitle_on_stop=True)
        assert settings.stop_announcement_on_live_stop is True
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui._announcement_text_active = "Goes away"
        gui.on_stop()
        assert gui._has_active_announcement() is False
        assert gui.subtitle_window is None
        assert fake.destroyed is True

    def test_on_stop_destroys_overlay_without_announcement(self, make_gui):
        gui, _c, _s = make_gui(hide_subtitle_on_stop=True)
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui.on_stop()
        assert gui.subtitle_window is None
        assert fake.destroyed is True


class TestMicTestDeviceChange:
    """Switching the input device while the mic test runs must move the test.

    The preview capture thread owns the device it was opened with, so leaving
    it alone reads to the operator as "the new mic is dead" until they restart
    the app.
    """

    def _select_second_device(self, gui):
        if len(gui.device_indices) < 2:
            pytest.skip("machine has fewer than two input devices")
        gui.device_combo.current(1)

    def test_running_test_moves_to_the_new_device(self, make_gui):
        gui, controller, _s = make_gui()
        self._select_second_device(gui)
        controller.level_test_running = True
        controller.level_test_device = gui.device_indices[0]

        gui._on_device_change()

        assert controller.level_test_starts == 1
        assert controller.level_test_device == gui.device_indices[1]
        assert controller.level_test_running is True

    def test_running_test_is_not_put_on_a_timer(self, make_gui):
        gui, controller, _s = make_gui()
        self._select_second_device(gui)
        controller.level_test_running = True

        gui._on_device_change()

        # An explicit mic test keeps running until the operator stops it.
        assert gui._input_level_auto_job is None

    def test_no_test_running_opens_a_short_auto_preview(self, make_gui):
        gui, controller, _s = make_gui()
        self._select_second_device(gui)

        gui._on_device_change()

        # Picking a device shows it working without pressing Test...
        assert controller.level_test_starts == 1
        assert controller.level_test_device == gui.device_indices[1]
        assert controller.level_test_running is True
        assert gui._input_level_auto_job is not None

        gui._auto_stop_input_level()  # ...and releases the device again

        assert controller.level_test_running is False
        assert gui._input_level_auto_job is None

    def test_test_button_takes_the_preview_off_the_timer(self, make_gui):
        gui, controller, _s = make_gui()
        self._select_second_device(gui)
        gui._on_device_change()  # auto-preview running, auto-stop pending

        gui._toggle_input_level_test()  # stops it (it is running)
        assert gui._input_level_auto_job is None
        assert controller.level_test_running is False

        gui._toggle_input_level_test()  # explicit test: no timer
        assert controller.level_test_running is True
        assert gui._input_level_auto_job is None

    def test_unopenable_device_stops_the_test_instead_of_leaving_it_half_open(
        self, make_gui, monkeypatch
    ):
        gui, controller, _s = make_gui()
        self._select_second_device(gui)
        controller.level_test_running = True
        controller.level_test_error = RuntimeError("device busy")
        alerts = []
        monkeypatch.setattr(
            gui, "_alert", lambda *a, **k: alerts.append(a), raising=False
        )

        gui._on_device_change()

        assert controller.level_test_stops == 1
        assert controller.level_test_running is False
        assert alerts, "the operator must be told why the meter went quiet"

    def test_live_session_still_hot_swaps_instead_of_previewing(self, make_gui):
        gui, controller, _s = make_gui()
        self._select_second_device(gui)
        gui._running = True

        gui._on_device_change()

        # A live session feeds the meter itself; no preview may be opened.
        assert controller.level_test_starts == 0


class TestCardGridReflow:
    """The card grid reflows to 1/2/3 columns so a wide window shows every
    card at once and a small one stays usable instead of being clipped."""

    def _pin_width(self, gui, monkeypatch, logical_width):
        monkeypatch.setattr(gui, "_get_window_scaling", lambda: 1.0, raising=False)
        monkeypatch.setattr(
            gui.sidebar, "winfo_width", lambda: logical_width, raising=False
        )

    def test_column_count_follows_the_window_width(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui()
        for width, expected in (
            (gui._COL2_MIN_W - 1, 1),
            (gui._COL2_MIN_W, 2),
            (gui._COL3_MIN_W - 1, 2),
            (gui._COL3_MIN_W, 3),
        ):
            self._pin_width(gui, monkeypatch, width)
            assert gui._column_count() == expected, width

    def test_open_log_panel_forces_a_single_column(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui()
        self._pin_width(gui, monkeypatch, gui._COL3_MIN_W)
        gui._log_collapsed = False
        assert gui._column_count() == 1

    def test_groups_are_placed_once_per_column_count(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui()
        groups = (gui._col_a, gui._col_b, gui._col_c)
        for width in (400, gui._COL2_MIN_W, gui._COL3_MIN_W):
            self._pin_width(gui, monkeypatch, width)
            gui._layout_sidebar_cards()
            cells = {}
            for group in groups:
                info = group.grid_info()
                for row in range(int(info["row"]), int(info["row"]) + int(info["rowspan"])):
                    cell = (row, int(info["column"]))
                    assert cell not in cells, f"{cell} occupied twice at {width}"
                    cells[cell] = group
            assert gui._applied_columns == gui._column_count()

    def test_wide_window_caps_and_centres_the_grid(self, make_gui, monkeypatch):
        """Past the cap the extra width becomes margin, not wider cards."""
        gui, _c, _s = make_gui()
        self._pin_width(gui, monkeypatch, gui._MAX_CARD_AREA_W_WIDE + 400)
        assert gui._collapsed_margin(3) == 200
        self._pin_width(gui, monkeypatch, gui._MAX_CARD_AREA_W_WIDE)
        assert gui._collapsed_margin(3) == 0

    def test_advanced_opens_in_three_columns_and_closes_below(
        self, make_gui, monkeypatch
    ):
        """Group C is nothing but the Advanced header while collapsed, so the
        third column would otherwise be won and then left empty."""
        gui, _c, _s = make_gui()
        assert gui.advanced_visible is False

        self._pin_width(gui, monkeypatch, gui._COL3_MIN_W)
        gui._layout_sidebar_cards()
        assert gui.advanced_visible is True

        self._pin_width(gui, monkeypatch, gui._COL2_MIN_W)
        gui._layout_sidebar_cards()
        assert gui.advanced_visible is False

    def test_manual_advanced_toggle_survives_until_the_columns_change(
        self, make_gui, monkeypatch
    ):
        gui, _c, _s = make_gui()
        self._pin_width(gui, monkeypatch, gui._COL3_MIN_W)
        gui._layout_sidebar_cards()
        assert gui.advanced_visible is True

        gui._toggle_advanced_settings()  # user closes it at this width
        assert gui.advanced_visible is False
        gui._layout_sidebar_cards()  # a resize that keeps 3 columns
        assert gui.advanced_visible is False

        self._pin_width(gui, monkeypatch, gui._COL2_MIN_W)
        gui._layout_sidebar_cards()
        self._pin_width(gui, monkeypatch, gui._COL3_MIN_W)
        gui._layout_sidebar_cards()
        assert gui.advanced_visible is True

    def test_card_groups_keep_their_natural_height(self, make_gui, monkeypatch):
        """Guards the 2026-07-21 revert: stretching a group/card to level the
        columns' bottom edges corrupted the Tcl interpreter intermittently."""
        gui, _c, _s = make_gui()
        for width in (gui._COL2_MIN_W, gui._COL3_MIN_W):
            self._pin_width(gui, monkeypatch, width)
            gui._layout_sidebar_cards()
            for group in (gui._col_a, gui._col_b, gui._col_c):
                assert group.grid_info()["sticky"] == "new", width

    def _pin_bottoms(self, gui, monkeypatch, display_bottom, advanced_bottom):
        """Fake the two columns' rendered bottom edges (nothing is mapped in a
        test, so _align_advanced_card would bail out before measuring)."""
        monkeypatch.setattr(gui, "_responsive_scale", 1.0, raising=False)
        for group, bottom in (
            (gui._col_a, display_bottom),
            (gui._col_c, advanced_bottom),
        ):
            monkeypatch.setattr(group, "winfo_ismapped", lambda: True, raising=False)
            monkeypatch.setattr(group, "winfo_rooty", lambda: 0, raising=False)
            monkeypatch.setattr(
                group, "winfo_height", lambda b=bottom: b, raising=False
            )

    def test_advanced_is_padded_down_to_meet_the_display_column(
        self, make_gui, monkeypatch
    ):
        gui, _c, _s = make_gui()
        gui._applied_columns = 2
        gui._advanced_gap = 0
        gui._typography_open = False
        self._pin_bottoms(gui, monkeypatch, display_bottom=500, advanced_bottom=400)
        gui._align_advanced_card()
        assert gui._advanced_gap == 100

    def test_advanced_stays_put_when_the_subtitle_settings_open(
        self, make_gui, monkeypatch
    ):
        """Opening the subtitle-appearance expander grows the display column.
        Advanced must hold its position instead of following it down, so the
        gap measured while the expander was closed stands."""
        gui, _c, _s = make_gui()
        gui._applied_columns = 2
        gui._advanced_gap = 40
        gui._typography_open = True
        self._pin_bottoms(gui, monkeypatch, display_bottom=900, advanced_bottom=400)
        gui._align_advanced_card()
        assert gui._advanced_gap == 40

    @staticmethod
    def _pady(widget) -> tuple[int, int]:
        """Tk reports an even pady as a single value, an uneven one as a pair."""
        value = widget.grid_info()["pady"]
        return tuple(value) if isinstance(value, tuple) else (value, value)

    def test_closed_advanced_card_pads_evenly_above_and_below_its_header(
        self, make_gui
    ):
        """Collapsed, the header is the whole card: its smaller bottom pad (the
        gap to the body) would read as a lopsided card."""
        gui, _c, _s = make_gui()
        assert gui.advanced_visible is False
        top, bottom = self._pady(gui._advanced_header)
        assert top == bottom

        gui._set_advanced_visible(True)  # body back: bottom pad is a gap again
        top, bottom = self._pady(gui._advanced_header)
        assert bottom < top

    def test_minimum_size_is_below_the_default(self, make_gui):
        """The window may be dragged well under its opening size (item: "as
        big and as small as the user wants")."""
        gui, _c, _s = make_gui()
        assert gui._MIN_W < gui._DEFAULT_W
        assert gui._MIN_H < gui._DEFAULT_H
        # CTk's minsize() has no query form (it would compare against None) —
        # read back what _setup_window stored on the window instead.
        assert (gui._min_width, gui._min_height) == (gui._MIN_W, gui._MIN_H)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

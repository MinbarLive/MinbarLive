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


def _display_available() -> bool:
    try:
        root = tk.Tk()
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
        gui = app_gui.AppGUI(controller)
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
        gui, _c, _settings = make_gui(ai_provider="gemini")
        from providers import get_model_choices

        expected = [mid for _n, mid in get_model_choices("gemini", "translation")]
        assert gui._model_ids == expected

    def test_selecting_the_same_provider_is_a_no_op(self, make_gui):
        gui, _c, settings = make_gui(ai_provider="gemini")
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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

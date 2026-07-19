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
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.settings import (
    DEFAULT_AI_PROVIDER,
    DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
    PIPELINE_MODE_SEGMENTED,
    PIPELINE_MODE_STREAMING,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    Settings,
)


def _display_available() -> bool:
    # Probe in a child process. Creating and destroying a Tk root here in the
    # pytest process poisons Tcl's global library bootstrap intermittently on
    # Windows (the first real CTk root then reports a missing init.tcl or
    # tcl_findLibrary). The child keeps the availability check isolated from
    # the GUI lifecycle this module is meant to test.
    probe = "import tkinter as tk; root = tk.Tk(); root.destroy()"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0


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
        self.last_input_device = None

    def start(self, input_device=None):
        self.started += 1
        self.last_input_device = input_device

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
        import gui.settings_view as settings_view

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
        monkeypatch.setattr(
            app_gui,
            "get_input_devices",
            lambda: (
                ["1. Test Microphone"],
                ["Test Microphone"],
                [7],
                [False],
            ),
        )
        # No keychain reads, and never open the key dialog during a test.
        monkeypatch.setattr(app_gui, "get_stored_api_key", lambda _p: None)
        monkeypatch.setattr(app_gui, "has_usable_key", lambda _p: True)
        monkeypatch.setattr(settings_view, "has_usable_key", lambda _p: True)
        monkeypatch.setattr(app_gui, "set_api_key", lambda _k: None)
        # Cost history is independently tested against a temporary directory;
        # GUI drive-throughs must never touch the operator's real AppData.
        monkeypatch.setattr(app_gui, "begin_cost_session", lambda: "test-session")
        monkeypatch.setattr(app_gui, "cancel_cost_session", lambda: None)
        monkeypatch.setattr(app_gui, "end_cost_session", lambda *_a, **_k: None)
        monkeypatch.setattr(app_gui, "flush_cost_history", lambda: None)
        monkeypatch.setattr(app_gui, "active_cost_session", lambda: None)
        monkeypatch.setattr(app_gui, "latest_cost_session", lambda: None)
        monkeypatch.setattr(app_gui, "cost_revision", lambda: 0)
        # The provider-default repair re-resolves from the stored keys; without
        # pinning this, results would depend on which keys the machine running
        # the suite happens to have. The rule lives in gui.control_state, so
        # that is where it must be patched.
        monkeypatch.setattr(
            control_state, "resolve_provider_by_keys", lambda **k: DEFAULT_AI_PROVIDER
        )

        controller = FakeController()
        # This module intentionally creates and destroys dozens of independent
        # Tk interpreters in one Windows process.  Tcl 8.6 occasionally loses
        # its library bootstrap between those synthetic roots even though a
        # real MinbarLive process creates only one.  Retry that narrow harness
        # failure once; all product exceptions still fail immediately.
        for attempt in range(2):
            try:
                gui = app_gui.AppGUI(controller)
                break
            except tk.TclError as exc:
                known_tcl_bootstrap_flake = any(
                    marker in str(exc)
                    for marker in (
                        "tcl_findLibrary",
                        "Can't find a usable init.tcl",
                        "Can't find a usable tk.tcl",
                    )
                )
                if attempt or not known_tcl_bootstrap_flake:
                    raise
                app_gui._clear_stale_scaling_windows(force=True)
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

    def test_idle_subtitle_window_exists_when_not_hidden(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        monkeypatch.setattr(app_gui, "SubtitleWindow", _FakeSubtitleWindow)
        gui, _c, _s = make_gui(hide_subtitle_on_stop=False)

        assert isinstance(gui.subtitle_window, _FakeSubtitleWindow)
        assert gui.subtitle_window.stopped_hint is True

    def test_signal_path_is_solid_and_uses_subtle_depth_cards(self, make_gui):
        gui, _c, _s = make_gui()

        assert gui.signal_band.cget("fg_color") == gui._colors["sidebar"]
        assert gui._dashboard._signal_rail.cget("fg_color") == "transparent"
        assert not hasattr(gui._dashboard, "_pattern_image")
        assert len(gui._dashboard._stage_widgets) == 4
        for widgets in gui._dashboard._stage_widgets:
            assert widgets["shadow"].cget("fg_color") == gui._colors["shadow"]
            assert widgets["surface"].cget("fg_color") == gui._colors["panel"]
            assert widgets["surface"].cget("border_color") == gui._colors["border"]
            assert (
                widgets["highlight"].cget("fg_color")
                == (gui._colors["surface_highlight"])
            )

        assert gui._shadow_frames == [
            gui._operator_dock_shadow,
            gui._session_col,
            gui._output_col,
            gui._services_col,
        ]
        assert [style["role"] for style in gui._section_card_styles] == [
            "session",
            "output",
            "services",
        ]
        assert [style["card"] for style in gui._section_card_styles] == [
            gui.language_card,
            gui.display_card,
            gui.advanced_card,
        ]
        assert [card.master for card in (
            gui.language_card,
            gui.display_card,
            gui.advanced_card,
        )] == [gui._session_col, gui._output_col, gui._services_col]

        role_surfaces = set()
        for style in gui._section_card_styles:
            role = style["role"]
            colors = gui._section_role_colors(role)
            role_surfaces.add(colors["surface"])
            assert style["card"].cget("fg_color") == colors["surface"]
            assert style["card"].cget("border_color") == colors["border"]
            assert style["card"].cget("border_width") == 2
            assert style["header"].cget("fg_color") == colors["soft"]
            assert style["symbol_shell"].cget("border_color") == colors["border"]
            assert style["symbol"].cget("text_color") == colors["accent"]
            assert style["accent"].cget("fg_color") == colors["accent"]
            assert style["highlight"].cget("fg_color") == gui._colors[
                "card_highlight"
            ]
            assert style["lowlight"].cget("fg_color") == gui._colors[
                "card_lowlight"
            ]
        assert len(role_surfaces) == 3

        assert gui.device_combo.cget("border_width") == 2
        assert gui.speed_decrease_btn.cget("border_width") == 1
        assert len(gui._recessed_panel_styles) == 5
        for style in gui._recessed_panel_styles:
            assert style["panel"].cget("fg_color") == gui._colors["recessed"]
            assert style["panel"].cget("border_color") == gui._colors[
                "recessed_border"
            ]


class TestProviderSelection:
    """The provider/model/strategy cluster — the most intricate logic in the
    control panel and the part most likely to break silently."""

    def test_switching_provider_repopulates_models_and_resets_default(self, make_gui):
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

    def test_unchanged_topmost_state_is_not_written_again(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui(always_on_top=True)
        gui.subtitle_window = _FakeOverlay()
        gui._control_topmost_state = None
        calls = []
        monkeypatch.setattr(gui, "attributes", lambda *args: calls.append(args) or "")

        gui._apply_control_window_topmost()
        gui._apply_control_window_topmost()

        assert calls == [("-topmost", True)]

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows style probe")
    def test_control_window_is_not_a_layered_surface(self, make_gui):
        """An opaque CTk dashboard must not pay layered-window composition
        costs while Windows moves it by its native title bar."""
        import ctypes
        from ctypes import wintypes

        gui, _c, _s = make_gui(always_on_top=True)
        gui.subtitle_window = _FakeOverlay()
        gui._apply_control_window_topmost()
        gui.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        get_style = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
        get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        hwnd = user32.GetParent(gui.winfo_id())

        ex_style = int(get_style(hwnd, -20))

        assert ex_style & 0x00080000 == 0  # WS_EX_LAYERED

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

    def test_explicit_openai_profile_survives_gui_restart_repair(self, make_gui):
        gui, _c, settings = make_gui(
            ai_provider="openai",
            transcription_provider="openai_realtime",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            use_default_translation_model=True,
            use_default_transcription_model=True,
        )

        assert settings.ai_provider == "openai"
        assert settings.transcription_provider == "openai_realtime"
        assert (
            gui._provider_profile_ids[gui.provider_profile_combo.current()] == "openai"
        )


class TestStartStop:
    def test_start_then_stop_drives_the_controller(self, make_gui):
        gui, controller, _s = make_gui()
        assert gui.primary_action_btn.cget("text") == gui.gui_texts.get(
            "v3_start_live", "Live starten"
        )
        gui.on_start()
        assert controller.started == 1
        assert gui._running is True
        assert gui.primary_action_btn.cget("text") == gui.gui_texts.get(
            "v3_stop_live", "Live stoppen"
        )

        gui.on_stop()
        assert controller.stopped >= 1
        assert gui._running is False
        assert gui.primary_action_btn.cget("text") == gui.gui_texts.get(
            "v3_start_live", "Live starten"
        )

    def test_start_re_resolves_same_microphone_after_index_change(
        self, make_gui, monkeypatch
    ):
        gui, controller, _settings = make_gui()
        monkeypatch.setattr(
            gui,
            "_get_input_devices",
            lambda: (
                ["1. Test Microphone"],
                ["Test Microphone"],
                [21],
                [False],
            ),
        )

        gui.on_start()

        assert controller.last_input_device == 21

    def test_missing_selected_microphone_never_switches_to_another(
        self, make_gui, monkeypatch
    ):
        gui, controller, _settings = make_gui()
        monkeypatch.setattr(
            gui,
            "_get_input_devices",
            lambda: (
                ["1. Different Microphone"],
                ["Different Microphone"],
                [4],
                [False],
            ),
        )
        monkeypatch.setattr(gui, "_alert", lambda *_a, **_k: None)

        gui.on_start()

        assert controller.started == 0
        assert "microphone" in gui._runtime_errors


class TestCostCounter:
    def test_cost_counter_is_inside_ai_services_and_opens_cost_history(self, make_gui, monkeypatch):
        gui, _controller, _settings = make_gui()
        assert gui._cost_title_label.master is not None
        assert set(gui._cost_provider_labels) == {"openai", "gemini"}
        assert "OpenAI" in gui._cost_provider_labels["openai"].cget("text")
        assert "Gemini" in gui._cost_provider_labels["gemini"].cget("text")

        opened = []
        monkeypatch.setattr(gui, "_open_history_window", opened.append)
        gui._cost_history_btn.invoke()
        assert opened == ["costs"]

    def test_cost_history_tab_shows_real_start_stop_record(self, make_gui, monkeypatch):
        import gui.history_view as history_view

        gui, _controller, _settings = make_gui()
        record = {
            "id": "cost-1",
            "started_at": "2026-07-19T10:00:00+00:00",
            "ended_at": "2026-07-19T10:05:00+00:00",
            "status": "completed",
            "pricing_version": "2026-07-19",
            "total_cost_usd": "0.0012",
            "fully_priced": True,
            "providers": {
                "openai": {
                    "cost_usd": "0.0012",
                    "fully_priced": True,
                    "models": {
                        "gpt-4o-mini": {
                            "cost_usd": "0.0012",
                            "fully_priced": True,
                            "roles": ["translation"],
                            "usage": {
                                "input_text_tokens": 100,
                                "output_text_tokens": 20,
                            },
                        }
                    },
                }
            },
        }
        monkeypatch.setattr(history_view, "list_cost_sessions", lambda: [record])
        gui._open_history_window("costs")
        assert gui._history_active_tab == "costs"
        assert "gpt-4o-mini" in gui._history_textbox.get("1.0", "end")
        assert "$0.0012" in gui._history_textbox.get("1.0", "end")

    def test_start_stop_owns_one_logical_cost_session(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, _controller, _settings = make_gui()
        lifecycle = []
        monkeypatch.setattr(
            app_gui, "begin_cost_session", lambda: lifecycle.append("begin") or "id"
        )
        monkeypatch.setattr(
            app_gui, "end_cost_session", lambda *_a, **_k: lifecycle.append("end")
        )
        gui.on_start()
        gui.on_stop()
        assert lifecycle == ["begin", "end"]

    def test_failed_start_discards_provisional_cost_session(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, controller, _settings = make_gui()
        lifecycle = []
        monkeypatch.setattr(
            app_gui, "begin_cost_session", lambda: lifecycle.append("begin") or "id"
        )
        monkeypatch.setattr(
            app_gui, "cancel_cost_session", lambda: lifecycle.append("cancel")
        )
        monkeypatch.setattr(
            controller, "start", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no"))
        )
        monkeypatch.setattr(gui, "_alert", lambda *_a, **_k: None)
        gui.on_start()
        assert lifecycle == ["begin", "cancel"]


class TestStartStopBehavior:
    def test_start_is_blocked_when_a_key_is_missing(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, controller, _s = make_gui()
        monkeypatch.setattr(app_gui, "has_usable_key", lambda _p: False)
        monkeypatch.setattr(gui, "_prompt_provider_key", lambda _p: None)

        gui.on_start()

        assert controller.started == 0, "must not start without a usable key"
        assert gui._running is False

    def test_openai_key_does_not_mask_required_gemini_key(self, make_gui, monkeypatch):
        gui, controller, _s = make_gui(
            ai_provider="gemini",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider="gemini_realtime",
        )
        prompts = []
        monkeypatch.setattr(
            gui, "_key_available", lambda provider: provider == "openai"
        )
        monkeypatch.setattr(gui, "_prompt_provider_key", prompts.append)

        gui.on_start()

        assert prompts == ["gemini"]
        assert controller.started == 0
        assert "Google Gemini" in gui.action_summary_label.cget("text")
        assert gui.primary_action_btn.cget("text") == gui.gui_texts.get(
            "v3_complete_setup", "Setup abschließen"
        )
        assert all(
            widgets["button"].cget("text").startswith("Google Gemini · ")
            for widgets in gui._v3_service_rows.values()
        )

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

    def test_internal_restart_keeps_window_and_redacts_a_failure(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        gui, controller, _s = make_gui(
            ai_provider="openai",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider="openai_realtime",
        )
        overlay = _FakeSubtitleWindow()
        gui.subtitle_window = overlay
        gui._running = True

        gui._restart_pipeline_for_live_change()

        assert controller.restarted == 1
        assert gui.subtitle_window is overlay

        secret = "sk-proj-restart-secret-that-must-not-appear"
        monkeypatch.setattr(app_gui, "get_stored_api_key", lambda _provider: secret)

        def fail_restart(**_kwargs):
            raise RuntimeError(f"rejected {secret}")

        monkeypatch.setattr(controller, "restart", fail_restart)
        alerts = []
        monkeypatch.setattr(
            gui, "_alert", lambda _title, message, **_kwargs: alerts.append(message)
        )

        gui._restart_pipeline_for_live_change()

        assert gui._running is False
        assert alerts and secret not in alerts[0]
        assert secret not in gui.action_summary_label.cget("text")
        assert "Spracherkennung · OpenAI" in gui.action_summary_label.cget("text")

    def test_translation_and_interim_text_only_reach_subtitle_window(self, make_gui):
        gui, controller, settings = make_gui(
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider=DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            show_interim_transcript=True,
        )
        overlay = _FakeSubtitleWindow()
        gui.subtitle_window = overlay
        controller.get_live_transcript = lambda: ("interim words", False)
        controller.translation_queue.put(("translated words", "source words"))

        gui._process_translation_queue()

        assert overlay.subtitles == [("translated words", "source words")]
        assert overlay.live_text == [("interim words", False)]
        assert settings.pipeline_mode == PIPELINE_MODE_STREAMING
        assert not any(
            hasattr(gui, name)
            for name in ("translation_preview", "live_transcript_label", "waveform")
        )


class TestRuntimeErrorUX:
    def test_synchronous_audio_failure_is_assigned_to_microphone(
        self, make_gui, monkeypatch
    ):
        from audio.device_support import AudioInputError

        gui, controller, _settings = make_gui(
            ai_provider="openai",
            transcription_provider="openai_realtime",
            pipeline_mode=PIPELINE_MODE_STREAMING,
        )
        monkeypatch.setattr(
            controller,
            "start",
            lambda **_kwargs: (_ for _ in ()).throw(
                AudioInputError("WdmSyncIoctl element not found")
            ),
        )
        alerts = []
        monkeypatch.setattr(
            gui, "_alert", lambda _title, message, **_kwargs: alerts.append(message)
        )

        gui.on_start()

        expected = gui.gui_texts["v3_microphone_open_failed"]
        assert gui._running is False
        assert gui._runtime_errors == {"microphone": expected}
        assert alerts == [expected]
        assert "WdmSyncIoctl" not in gui.action_summary_label.cget("text")

    def test_synchronous_openai_auth_rejection_is_actionable(
        self, make_gui, monkeypatch
    ):
        gui, controller, _settings = make_gui(
            ai_provider="openai",
            transcription_provider="openai_realtime",
            pipeline_mode=PIPELINE_MODE_STREAMING,
        )

        def reject_start(**_kwargs):
            raise RuntimeError("HTTP 401 invalid_api_key sk-proj-****WXYZ")

        monkeypatch.setattr(controller, "start", reject_start)
        alerts = []
        monkeypatch.setattr(
            gui, "_alert", lambda _title, message, **_kwargs: alerts.append(message)
        )

        gui.on_start()

        assert gui._running is False
        assert gui._rejected_key_provider == "openai"
        assert alerts == [
            "Schlüssel von OpenAI abgelehnt. Bitte den OpenAI-Schlüssel ersetzen."
        ]
        assert "****WXYZ" not in gui.action_summary_label.cget("text")
        assert gui.primary_action_btn.cget("text") == "OpenAI-Schlüssel ersetzen"

    def test_input_stream_error_names_microphone_role_and_redacts_key(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        gui, controller, _settings = make_gui(
            ai_provider="openai",
            transcription_provider="openai_realtime",
            pipeline_mode=PIPELINE_MODE_STREAMING,
        )
        secret = "sk-proj-runtime-secret-that-must-not-appear"
        monkeypatch.setattr(
            app_gui,
            "get_stored_api_key",
            lambda provider: secret if provider == "openai" else None,
        )
        messages: list[str] = []
        monkeypatch.setattr(
            app_gui,
            "log",
            lambda message, **_kwargs: messages.append(str(message)),
        )
        if gui.error_poll_job is not None:
            gui.after_cancel(gui.error_poll_job)
            gui.error_poll_job = None

        controller.error_queue.put(f"input_stream_error:rejected {secret}")
        gui._poll_errors()

        summary = gui.action_summary_label.cget("text")
        assert summary.startswith("Mikrofon:")
        assert "Fehler im Eingabestream" in summary
        assert secret not in summary
        assert "[REDACTED]" in summary
        assert (
            gui._dashboard._stage_widgets[0]["state"].cget("text")
            == (gui.gui_texts["v3_error"])
        )
        assert secret not in " ".join(messages)

    def test_provider_error_names_role_provider_and_marks_signal_path(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        gui, controller, _settings = make_gui(
            ai_provider="openai",
            transcription_provider="openai_realtime",
            pipeline_mode=PIPELINE_MODE_STREAMING,
        )
        monkeypatch.setattr(app_gui, "get_stored_api_key", lambda _provider: None)
        if gui.error_poll_job is not None:
            gui.after_cancel(gui.error_poll_job)
            gui.error_poll_job = None

        controller.error_queue.put("transcription_error:key rejected")
        gui._poll_errors()

        summary = gui.action_summary_label.cget("text")
        assert summary == "Spracherkennung · OpenAI: key rejected"
        assert (
            gui._v3_service_rows["transcription"]["status"].cget("text")
            == (gui.gui_texts["v3_error"])
        )
        assert (
            gui._dashboard._stage_widgets[1]["state"].cget("text")
            == (gui.gui_texts["v3_error"])
        )

    def test_fatal_openai_auth_error_stops_and_offers_exact_key_replacement(
        self, make_gui
    ):
        gui, controller, _settings = make_gui(
            ai_provider="openai",
            transcription_provider="openai_realtime",
            pipeline_mode=PIPELINE_MODE_STREAMING,
        )
        if gui.error_poll_job is not None:
            gui.after_cancel(gui.error_poll_job)
            gui.error_poll_job = None
        gui.on_start()
        assert gui._running is True

        controller.error_queue.put("fatal_transcription_error:invalid_api_key")
        gui._poll_errors()
        gui.update_idletasks()

        assert gui._running is False
        assert controller.stopped >= 1
        assert gui._rejected_key_provider == "openai"
        assert gui.action_summary_label.cget("text") == (
            "Spracherkennung · OpenAI: Schlüssel von OpenAI abgelehnt. "
            "Bitte den OpenAI-Schlüssel ersetzen."
        )
        assert gui.primary_action_btn.cget("text") == ("OpenAI-Schlüssel ersetzen")

    def test_replacing_rejected_provider_key_clears_auth_error(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        gui, _controller, _settings = make_gui()
        gui._record_runtime_error("transcription", "rejected")
        gui._rejected_key_provider = "openai"
        monkeypatch.setattr(app_gui, "prompt_for_api_key", lambda **_kwargs: "new")

        gui._prompt_provider_key("openai")

        assert gui._rejected_key_provider is None
        assert gui._runtime_errors == {}
        assert gui._runtime_error_message is None

    def test_successful_restart_clears_previous_runtime_error(self, make_gui):
        gui, _controller, _settings = make_gui()
        gui._record_runtime_error("transcription", "temporary failure")
        assert gui._runtime_error_message

        gui.on_start()

        assert gui._runtime_error_message is None
        assert gui._runtime_errors == {}


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

    def test_key_vault_actions_name_selected_service_and_use_exact_status(
        self, make_gui
    ):
        gui, _c, _s = make_gui()
        gui._open_settings_window()
        gui.api_key_provider_combo.current(0)
        gui._refresh_api_key_status()

        assert gui.api_key_status_label.cget("text") == "Schlüssel gespeichert"
        assert gui.change_key_btn.cget("text").startswith("Google Gemini · ")
        assert gui.remove_key_btn.cget("text").startswith("Google Gemini · ")

    def test_theme_switch_repaints_and_persists(self, make_gui):
        gui, _c, settings = make_gui(theme_mode="light")
        gui._on_theme_change("dark")
        assert settings.theme_mode == "dark"
        assert gui._theme_mode == "dark"

        style_ids = [id(style["card"]) for style in gui._section_card_styles]

        for mode in ("light", "dark"):
            gui._on_theme_change(mode)

            assert all(
                shadow.cget("fg_color") == gui._colors["shadow"]
                for shadow in gui._shadow_frames
            )
            assert [id(style["card"]) for style in gui._section_card_styles] == (
                style_ids
            )
            for style in gui._section_card_styles:
                colors = gui._section_role_colors(style["role"])
                assert style["card"].cget("fg_color") == colors["surface"]
                assert style["card"].cget("border_color") == colors["border"]
                assert style["header"].cget("fg_color") == colors["soft"]
                assert style["accent"].cget("fg_color") == colors["accent"]
                assert style["highlight"].cget("fg_color") == gui._colors[
                    "card_highlight"
                ]
                assert style["lowlight"].cget("fg_color") == gui._colors[
                    "card_lowlight"
                ]
            for style in gui._recessed_panel_styles:
                assert style["panel"].cget("fg_color") == gui._colors[
                    "recessed"
                ]
                assert style["shadow"].cget("fg_color") == gui._colors[
                    "recessed_shadow"
                ]
                assert style["highlight"].cget("fg_color") == gui._colors[
                    "recessed_highlight"
                ]

            assert gui._operator_dock.cget("fg_color") == gui._colors["panel"]
            assert gui._operator_dock.cget("border_color") == gui._colors[
                "brass_soft"
            ]
            assert gui._operator_dock_highlight.cget("fg_color") == gui._colors[
                "surface_highlight"
            ]
            assert gui.log_panel.cget("fg_color") == gui._colors["panel"]
            assert gui.device_combo.cget("border_color") == gui._colors[
                "entry_border"
            ]
            assert gui.speed_decrease_btn.cget("border_color") == gui._colors[
                "button_border"
            ]

    def test_log_panel_toggle_round_trips(self, make_gui):
        gui, _c, settings = make_gui(log_panel_collapsed=True)
        assert not gui.content.grid_info()
        gui._toggle_log_panel()
        assert settings.log_panel_collapsed is False
        assert gui.content.grid_info()
        gui._toggle_log_panel()
        assert settings.log_panel_collapsed is True
        assert not gui.content.grid_info()

    def test_open_log_panel_has_reachable_internal_close_button(self, make_gui):
        gui, _c, settings = make_gui(log_panel_collapsed=True)
        gui.geometry(f"{gui._MIN_W}x{gui._MIN_H}")
        gui._toggle_log_panel()
        gui.update_idletasks()

        button = gui._log_close_btn
        # The test harness intentionally never pumps Tk's native map events,
        # so verify the durable layout relationship instead: the close action
        # is gridded inside the visible drawer, not in the clipped sidebar.
        assert button.winfo_manager() == "grid"
        assert button.master.master is gui.content
        assert int(button.grid_info()["column"]) == 2

        button.invoke()
        assert settings.log_panel_collapsed is True
        assert not gui.content.grid_info()

    def test_close_log_panel_is_idempotent(self, make_gui):
        gui, _c, settings = make_gui(log_panel_collapsed=True)
        gui._toggle_log_panel()

        gui._close_log_panel()
        gui._close_log_panel()

        assert settings.log_panel_collapsed is True
        assert not gui.content.grid_info()

    def test_wide_layout_resets_stacked_column_spans(self, make_gui, monkeypatch):
        gui, _c, _settings = make_gui(log_panel_collapsed=True)
        for column in (gui._session_col, gui._output_col, gui._services_col):
            column.grid_configure(columnspan=3)
        gui._v3_layout_mode = "stacked"
        monkeypatch.setattr(gui.sidebar, "winfo_width", lambda: 1800)

        gui._layout_sidebar_cards()

        assert gui._v3_layout_mode == "wide"
        assert all(
            int(column.grid_info()["columnspan"]) == 1
            for column in (gui._session_col, gui._output_col, gui._services_col)
        )

    def test_responsive_breakpoint_uses_hysteresis_while_resizing(
        self, make_gui, monkeypatch
    ):
        gui, _c, _settings = make_gui(log_panel_collapsed=True)
        monkeypatch.setattr(gui, "_get_window_scaling", lambda: 1.0)
        monkeypatch.setattr(gui, "_wide_layout_breakpoint", lambda: 1100)

        gui._v3_layout_mode = None
        monkeypatch.setattr(gui.sidebar, "winfo_width", lambda: 1099)
        gui._layout_sidebar_cards()
        assert gui._v3_layout_mode == "stacked"
        assert [
            int(column.grid_info()["row"])
            for column in (gui._session_col, gui._output_col, gui._services_col)
        ] == [0, 1, 2]

        # Once stacked, the layout does not flap at the 1100 boundary; it
        # switches only after crossing the upper edge of the dead band.
        monkeypatch.setattr(gui.sidebar, "winfo_width", lambda: 1100)
        gui._layout_sidebar_cards()
        assert gui._v3_layout_mode == "stacked"

        monkeypatch.setattr(gui.sidebar, "winfo_width", lambda: 1120)
        gui._layout_sidebar_cards()
        assert gui._v3_layout_mode == "wide"
        assert [
            int(column.grid_info()["column"])
            for column in (gui._session_col, gui._output_col, gui._services_col)
        ] == [0, 1, 2]

    def test_subtitle_presentation_controls_belong_to_output_card(self, make_gui):
        gui, _c, _settings = make_gui()
        output_path = f"{gui.display_card}."
        session_path = f"{gui.language_card}."

        for widget in (
            gui.subtitle_mode_combo,
            gui.speed_decrease_btn,
            gui.transparent_checkbox,
            gui.font_decrease_btn,
            gui.source_font_decrease_btn,
            gui.translation_color_btn,
            gui.source_color_btn,
            gui.height_slider,
            gui.bilingual_cb,
            gui.adaptive_catchup_cb,
            gui.show_interim_cb,
        ):
            assert str(widget).startswith(output_path)
            assert not str(widget).startswith(session_path)


class TestDropdownExplanations:
    def test_initial_explanations_match_real_strategy_and_subtitle_mode(
        self, make_gui
    ):
        gui, _controller, _settings = make_gui(
            gui_language="de",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider=DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            subtitle_mode=SUBTITLE_MODE_REALTIME,
        )

        assert gui.strategy_help_label.cget("text") == gui.gui_texts[
            "strategy_help_realtime"
        ]
        assert gui.subtitle_mode_help_label.cget("text") == gui.gui_texts[
            "subtitle_help_realtime"
        ]
        assert str(gui.strategy_help_label).startswith(f"{gui.language_card}.")
        assert str(gui.subtitle_mode_help_label).startswith(f"{gui.display_card}.")

    def test_explanations_follow_strategy_fallback_and_subtitle_selection(
        self, make_gui
    ):
        gui, _controller, settings = make_gui(
            gui_language="de",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider=DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            subtitle_mode=SUBTITLE_MODE_REALTIME,
        )

        semantic_index = gui._strategy_ids.index("semantic")
        gui.strategy_combo.current(semantic_index)
        gui._on_strategy_change()

        assert settings.processing_strategy == "semantic"
        assert gui.strategy_help_label.cget("text") == gui.gui_texts[
            "strategy_help_semantic"
        ]
        assert gui._effective_subtitle_mode() == SUBTITLE_MODE_CONTINUOUS
        assert gui.subtitle_mode_help_label.cget("text") == gui.gui_texts[
            "subtitle_help_continuous"
        ]

        static_index = gui._subtitle_mode_values.index(SUBTITLE_MODE_STATIC)
        gui.subtitle_mode_combo.current(static_index)
        gui._on_subtitle_mode_change()
        assert gui.subtitle_mode_help_label.cget("text") == gui.gui_texts[
            "subtitle_help_static"
        ]

    def test_explanations_retranslate_and_use_rtl_alignment(self, make_gui):
        import gui.app_gui as app_gui

        gui, _controller, _settings = make_gui(
            gui_language="de",
            pipeline_mode=PIPELINE_MODE_STREAMING,
            transcription_provider=DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            subtitle_mode=SUBTITLE_MODE_REALTIME,
        )
        gui.gui_lang_code = "ar"
        gui._gui_lang = "ar"
        gui.gui_texts = app_gui.load_gui_translations("ar")
        gui._t = gui.gui_texts

        gui._update_all_ui_texts()

        assert gui.strategy_help_label.cget("text") == gui.gui_texts[
            "strategy_help_realtime"
        ]
        assert gui.subtitle_mode_help_label.cget("text") == gui.gui_texts[
            "subtitle_help_realtime"
        ]
        assert gui.strategy_help_label.cget("anchor") == "e"
        assert gui.strategy_help_label.cget("justify") == "right"
        assert gui.subtitle_mode_help_label.cget("anchor") == "e"
        assert gui.subtitle_mode_help_label.cget("justify") == "right"


class TestSubtitleTypography:
    class _TypographyOverlay:
        def __init__(self):
            self.translation_base = 40
            self.source_base = 40 / 0.7
            self.translation_colors = []
            self.source_colors = []

        def winfo_exists(self):
            return True

        def increase_font(self):
            self.translation_base -= 5

        def decrease_font(self):
            self.translation_base += 5

        def get_font_size_base(self):
            return self.translation_base

        def increase_source_font(self):
            self.source_base -= 5

        def decrease_source_font(self):
            self.source_base += 5

        def get_source_font_size_base(self):
            return self.source_base

        def set_translation_text_color(self, color):
            self.translation_colors.append(color)

        def set_source_text_color(self, color):
            self.source_colors.append(color)

    def test_each_role_has_its_own_size_and_color_controls(self, make_gui):
        gui, _c, _settings = make_gui(
            source_language="Arabic", target_language="German"
        )

        assert gui.font_decrease_btn is gui.translation_font_decrease_btn
        assert gui.font_increase_btn is gui.translation_font_increase_btn
        assert gui.source_font_decrease_btn is not gui.translation_font_decrease_btn
        assert gui.source_color_btn is not gui.translation_color_btn
        assert "{language}" not in gui.source_typography_label.cget("text")
        assert "{language}" not in gui.translation_typography_label.cget("text")
        assert gui.translation_font_size_label.cget("text") == "100%"
        assert gui.source_font_size_label.cget("text") == "70%"

    def test_font_buttons_persist_even_when_output_window_is_closed(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        gui, _c, settings = make_gui(
            font_size_base=40,
            source_font_size_base=40 / 0.7,
            hide_subtitle_on_stop=True,
        )
        saved = []
        monkeypatch.setattr(app_gui, "save_settings", lambda current: saved.append(current))

        gui._increase_subtitle_font()
        gui._decrease_source_subtitle_font()

        assert gui.subtitle_window is None
        assert settings.font_size_base == 35
        assert settings.source_font_size_base == pytest.approx((40 / 0.7) + 5)
        assert gui.translation_font_size_label.cget("text") == "114%"
        assert gui.source_font_size_label.cget("text") == "64%"
        assert len(saved) == 2

    def test_live_overlay_receives_role_specific_size_changes(self, make_gui):
        gui, _c, settings = make_gui(
            font_size_base=40, source_font_size_base=40 / 0.7
        )
        overlay = self._TypographyOverlay()
        gui.subtitle_window = overlay

        gui._increase_subtitle_font()
        gui._increase_source_subtitle_font()

        assert settings.font_size_base == 35
        assert settings.source_font_size_base == pytest.approx((40 / 0.7) - 5)
        assert overlay.translation_base == 35
        assert overlay.source_base == pytest.approx((40 / 0.7) - 5)

    @pytest.mark.parametrize(
        ("start", "action", "expected"),
        [
            (20.0, "_increase_source_subtitle_font", 20.0),
            (120.0, "_decrease_source_subtitle_font", 120.0),
        ],
    )
    def test_closed_source_font_respects_renderer_bounds(
        self, make_gui, start, action, expected
    ):
        gui, _c, settings = make_gui(
            hide_subtitle_on_stop=True, source_font_size_base=start
        )

        getattr(gui, action)()

        assert settings.source_font_size_base == expected
        bound_button = (
            gui.source_font_increase_btn
            if action == "_increase_source_subtitle_font"
            else gui.source_font_decrease_btn
        )
        assert bound_button.cget("state") == "disabled"

    def test_color_picker_applies_live_and_reset_restores_theme_default(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        gui, _c, settings = make_gui(
            translation_text_color="", source_text_color=""
        )
        overlay = self._TypographyOverlay()
        gui.subtitle_window = overlay
        monkeypatch.setattr(
            app_gui.colorchooser,
            "askcolor",
            lambda **_kwargs: ((171, 205, 239), "#ABCDEF"),
        )

        gui._choose_subtitle_text_color("translation")

        assert settings.translation_text_color == "#abcdef"
        assert overlay.translation_colors == ["#abcdef"]
        assert gui.translation_color_btn.cget("border_color") == "#abcdef"
        assert gui.translation_color_reset_btn.cget("state") == "normal"

        gui._reset_subtitle_text_color("translation")

        assert settings.translation_text_color == ""
        assert overlay.translation_colors == ["#abcdef", ""]
        assert gui.translation_color_reset_btn.cget("state") == "disabled"

    def test_color_picker_persists_without_output_window(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, _c, settings = make_gui(
            hide_subtitle_on_stop=True, source_text_color=""
        )
        monkeypatch.setattr(
            app_gui.colorchooser,
            "askcolor",
            lambda **_kwargs: ((18, 52, 86), "#123456"),
        )

        gui._choose_subtitle_text_color("source")

        assert gui.subtitle_window is None
        assert settings.source_text_color == "#123456"
        assert gui.source_color_btn.cget("border_color") == "#123456"

    def test_subtitle_window_receives_saved_typography(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        captured = {}

        class CaptureWindow(_FakeSubtitleWindow):
            def __init__(self, *_args, **kwargs):
                super().__init__()
                captured.update(kwargs)

        monkeypatch.setattr(app_gui, "SubtitleWindow", CaptureWindow)
        make_gui(
            hide_subtitle_on_stop=False,
            font_size_base=35,
            source_font_size_base=62.5,
            translation_text_color="#123456",
            source_text_color="#abcdef",
        )

        assert captured["font_size_base"] == 35
        assert captured["source_font_size_base"] == 62.5
        assert captured["translation_text_color"] == "#123456"
        assert captured["source_text_color"] == "#abcdef"

    def test_automatic_source_uses_plain_original_label(self, make_gui):
        gui, _c, _settings = make_gui(
            source_language="Automatic",
            pipeline_mode=PIPELINE_MODE_SEGMENTED,
            transcription_provider="openai",
        )
        assert gui.source_typography_label.cget("text") == gui.gui_texts[
            "subtitle_original_text_plain"
        ]

    def test_realtime_source_fallback_refreshes_typography_language(self, make_gui):
        gui, _c, settings = make_gui(
            source_language="Automatic",
            pipeline_mode=PIPELINE_MODE_SEGMENTED,
            transcription_provider="openai",
        )

        settings.pipeline_mode = PIPELINE_MODE_STREAMING
        gui._refresh_source_language_combo()

        assert settings.source_language == "Arabic"
        assert "العربية" in gui.source_typography_label.cget("text")


class _FakeSubtitleWindow:
    """Records the announcement/overlay calls the AppGUI drives, without
    opening a real fullscreen overlay."""

    def __init__(self, *_args, **_kwargs):
        self.announcement = None
        self.stopped_hint = None
        self.destroyed = False
        self.subtitles = []
        self.live_text = []

    def winfo_exists(self):
        return not self.destroyed

    def set_announcement(self, text):
        self.announcement = text

    def clear_announcement(self):
        self.announcement = ""

    def set_stopped_hint(self, visible):
        self.stopped_hint = visible

    def add_subtitle(self, text, source_text=None):
        self.subtitles.append((text, source_text))

    def set_live_text(self, text, settled):
        self.live_text.append((text, settled))

    def destroy(self):
        self.destroyed = True


class TestNoScreenOutput:
    """A disabled audience output must never affect the audio/AI pipeline.

    The monitor combo reserves index 0 for the ``No screen`` choice.  Real
    monitors therefore have to be mapped back by one before they are persisted
    or passed to ``SubtitleWindow``.
    """

    @staticmethod
    def _two_monitors(monkeypatch):
        import gui.app_gui as app_gui

        monkeypatch.setattr(
            app_gui,
            "get_monitors",
            lambda: [
                SimpleNamespace(x=0, y=0, width=1920, height=1080),
                SimpleNamespace(x=1920, y=0, width=2560, height=1440),
            ],
        )

    def test_disabled_output_creates_no_window_at_startup_or_session_start(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        created = []

        class CaptureWindow(_FakeSubtitleWindow):
            def __init__(self, *_args, **kwargs):
                super().__init__()
                created.append(kwargs)

        self._two_monitors(monkeypatch)
        monkeypatch.setattr(app_gui, "SubtitleWindow", CaptureWindow)
        gui, controller, settings = make_gui(
            subtitle_output_enabled=False,
            hide_subtitle_on_stop=False,
        )

        assert gui.screen_combo.current() == 0
        assert gui.subtitle_window is None
        assert created == []

        gui.on_start()

        assert controller.started == 1
        assert gui._running is True
        assert settings.subtitle_output_enabled is False
        assert gui.subtitle_window is None
        assert created == []

    def test_selecting_no_screen_destroys_overlay_without_stopping_pipeline(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        self._two_monitors(monkeypatch)
        monkeypatch.setattr(app_gui, "SubtitleWindow", _FakeSubtitleWindow)
        gui, controller, settings = make_gui(
            subtitle_output_enabled=True,
            hide_subtitle_on_stop=True,
            monitor_index=1,
        )
        gui.on_start()
        overlay = gui.subtitle_window
        assert isinstance(overlay, _FakeSubtitleWindow)

        gui.screen_combo.current(0)
        gui._on_screen_change()

        assert settings.subtitle_output_enabled is False
        assert overlay.destroyed is True
        assert gui.subtitle_window is None
        assert gui._running is True
        assert controller.started == 1
        assert controller.stopped == 0

    def test_reenable_maps_dropdown_index_to_real_monitor_index(
        self, make_gui, monkeypatch
    ):
        import gui.app_gui as app_gui

        created = []

        class CaptureWindow(_FakeSubtitleWindow):
            def __init__(self, *_args, **kwargs):
                super().__init__()
                created.append(kwargs)

        self._two_monitors(monkeypatch)
        monkeypatch.setattr(app_gui, "SubtitleWindow", CaptureWindow)
        gui, controller, settings = make_gui(
            subtitle_output_enabled=False,
            hide_subtitle_on_stop=True,
            monitor_index=0,
        )
        gui.on_start()
        assert controller.started == 1
        assert gui.subtitle_window is None

        # Combo index 2 is the second real monitor because index 0 is the
        # explicit no-output choice.
        gui.screen_combo.current(2)
        gui._on_screen_change()

        assert settings.subtitle_output_enabled is True
        assert settings.monitor_index == 1
        assert gui.selected_screen_index == 1
        assert created[-1]["monitor_index"] == 1
        assert isinstance(gui.subtitle_window, CaptureWindow)

    def test_announcement_is_blocked_before_any_state_mutation(
        self, make_gui, monkeypatch
    ):
        gui, _controller, settings = make_gui(subtitle_output_enabled=False)
        gui._open_announce_window()
        gui._announce_textbox.insert("1.0", "Please silence your phones")
        created = []
        monkeypatch.setattr(gui, "_create_subtitle_window", lambda: created.append(True))
        monkeypatch.setattr(gui, "_alert", lambda *_args, **_kwargs: None)

        gui._send_announcement()

        assert settings.announcement_history == []
        assert gui._announcement_text_active == ""
        assert gui._announcement_until_stopped is False
        assert gui._announcement_job is None
        assert gui.subtitle_window is None
        assert created == []

    def test_dashboard_renders_disabled_output_as_neutral_even_while_running(
        self, make_gui
    ):
        gui, _controller, _settings = make_gui(subtitle_output_enabled=False)
        gui._running = True

        gui._dashboard.refresh(animate=False)

        output = gui._dashboard._stage_widgets[-1]
        expected = gui.gui_texts.get("v3_output_disabled", "Deaktiviert")
        assert output["state"].cget("text") == expected
        assert output["state"].cget("text_color") == gui._colors["muted"]
        assert output["icon"].cget("text_color") == gui._colors["muted"]
        assert output["dot"].cget("fg_color") == gui._colors["muted"]


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

    def test_sending_a_favorited_text_does_not_duplicate_into_history(self, make_gui):
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

    def test_favorites_section_hidden_when_empty_shown_after_favoriting(self, make_gui):
        gui, _c, _s = make_gui()
        gui._open_announce_window()
        assert not gui._announce_favorites_frame.grid_info()
        gui._favorite_announcement("Pinned")
        assert gui._announce_favorites_frame.grid_info()

    def test_on_stop_keeps_overlay_when_announcement_active(self, make_gui):
        # hide_subtitle_on_stop=True normally destroys the overlay on stop, but
        # an active "until stopped" announcement must survive the stop.
        gui, _c, _s = make_gui(hide_subtitle_on_stop=True)
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui._announcement_text_active = "Stays up"
        gui.on_stop()
        assert gui.subtitle_window is fake
        assert fake.destroyed is False

    def test_on_stop_destroys_overlay_without_announcement(self, make_gui):
        gui, _c, _s = make_gui(hide_subtitle_on_stop=True)
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui.on_stop()
        assert gui.subtitle_window is None
        assert fake.destroyed is True

    def test_enabling_hide_on_stop_keeps_active_announcement_visible(self, make_gui):
        gui, _c, _s = make_gui(hide_subtitle_on_stop=True)
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._announcement_text_active = "Keep this visible"
        gui.hide_subtitle_on_stop_var.set(True)

        gui._on_hide_subtitle_on_stop_change()

        assert gui.subtitle_window is fake
        assert fake.destroyed is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

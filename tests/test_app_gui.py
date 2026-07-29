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
- ``subtitle_hide_mode="stopped"`` — no fullscreen overlay while stopped.

Note there is deliberately no ``update()`` pump: a manual pump loop crashes
natively inside Tcl here. ``update_idletasks()`` is enough to settle layout,
and handlers are invoked directly, which is what a callback would do anyway.
"""

import queue
import sys
import threading
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


def _probe_display() -> tuple[bool, bool]:
    """Ask the display two questions with one throwaway root.

    Returns ``(display available, per-window opacity applied)``. The first is
    not just a skip guard: a transient failure here would silently skip every
    test in this file and still report the run green.

    Opacity is asked because X11 without a compositing manager (CI's xvfb)
    accepts ``-alpha`` without raising and then reports 1.0 back whatever was
    requested — the paint-before-reveal fade is a documented no-op on such a
    display, and its *logic* is asserted via ``_reveal_pending`` instead.
    """
    try:
        root = _build_with_tk_retry(tk.Tk)
    except Exception:
        return False, False
    # Withdrawn: a mapped root flashes an empty box in the corner of the screen
    # on every run. Alpha still round-trips while withdrawn.
    root.withdraw()
    try:
        root.attributes("-alpha", 0.5)
        alpha_applied = abs(float(root.attributes("-alpha")) - 0.5) < 0.01
    except Exception:
        alpha_applied = False
    root.destroy()
    return True, alpha_applied


# The control panel needs a real display; skip rather than fail on headless CI.
_DISPLAY_AVAILABLE, _ALPHA_HONOURED = _probe_display()

pytestmark = pytest.mark.skipif(
    not _DISPLAY_AVAILABLE, reason="no display available for GUI tests"
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
        settings.subtitle_hide_mode = "stopped"  # no overlay while stopped
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
    overlay is open, and only if always-on-top is in effect for the current
    run state (mode 'always', or 'running' while a session runs). The 3-way
    selector applies to both windows live.

    The fixture default mode is 'running' with the panel stopped, so it starts
    NOT topmost — these tests set the state they need explicitly."""

    def test_not_topmost_while_no_overlay_open(self, make_gui):
        # subtitle_hide_mode="stopped" (fixture default) => no overlay stopped.
        gui, _c, _s = make_gui(always_on_top_mode="always")
        assert gui.subtitle_window is None
        assert gui._control_window_should_be_topmost() is False
        assert _topmost(gui) is False

    def test_topmost_while_overlay_open(self, make_gui):
        gui, _c, _s = make_gui(always_on_top_mode="always")
        gui.subtitle_window = _FakeOverlay()
        gui._apply_control_window_topmost()
        assert gui._control_window_should_be_topmost() is True
        if _wm_reflects_topmost(gui):
            assert _topmost(gui) is True

    def test_select_never_drops_both_windows(self, make_gui):
        gui, _c, settings = make_gui(always_on_top_mode="always")
        overlay = _FakeOverlay()
        gui.subtitle_window = overlay

        gui._on_always_on_top_mode_change(gui._always_on_top_mode_labels()[0])  # Never

        assert settings.always_on_top_mode == "never"
        assert overlay.always_on_top_calls == [False]  # overlay told to drop
        assert gui._control_window_should_be_topmost() is False
        assert _topmost(gui) is False

    def test_select_always_restores_topmost_with_overlay(self, make_gui):
        gui, _c, settings = make_gui(always_on_top_mode="never")
        gui.subtitle_window = _FakeOverlay()

        gui._on_always_on_top_mode_change(gui._always_on_top_mode_labels()[2])  # Always

        assert settings.always_on_top_mode == "always"
        assert gui._control_window_should_be_topmost() is True
        if _wm_reflects_topmost(gui):
            assert _topmost(gui) is True

    def test_never_mode_is_never_topmost_even_with_overlay(self, make_gui):
        gui, _c, _s = make_gui(always_on_top_mode="never")
        gui.subtitle_window = _FakeOverlay()
        gui._apply_control_window_topmost()
        assert gui._control_window_should_be_topmost() is False
        assert _topmost(gui) is False

    def test_running_mode_topmost_only_while_running(self, make_gui):
        # 'running': not topmost when stopped, topmost once a session runs.
        gui, _c, _s = make_gui(always_on_top_mode="running")
        gui.subtitle_window = _FakeOverlay()
        assert gui._control_window_should_be_topmost() is False
        gui._running = True
        assert gui._control_window_should_be_topmost() is True

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


def _start_and_settle(gui, timeout: float = 5.0):
    """Press Start and let it finish.

    Start hands controller.start() to a worker thread and picks the outcome up
    from an after() poll (the provider handshake can run for tens of seconds
    and must not freeze the window). There is no mainloop here, so join the
    worker and run the poll by hand.
    """
    gui.on_start()
    thread = gui._start_thread
    if thread is not None:
        thread.join(timeout)
    gui._poll_start_result()


def _stop_and_settle(gui, timeout: float = 5.0):
    """Press Stop and let it finish.

    Stop hands controller.stop() to a worker thread and picks the teardown up
    from an after() poll (closing a streaming session can block on the
    connection lock for tens of seconds and must not freeze the window). There
    is no mainloop here, so join the worker and run the poll by hand."""
    gui.on_stop()
    thread = gui._stop_thread
    if thread is not None:
        thread.join(timeout)
    gui._poll_stop_result()


class TestStartStop:
    def test_starting_does_not_change_the_card_heights(self, make_gui):
        """The "stop to change" hint shares the strategy label's line. As its
        own row it grew the Translation-flow card by ~24px on every Start,
        which pushed the Advanced card below the left column's bottom edge."""
        gui, _controller, _s = make_gui()
        gui.update_idletasks()
        stopped = gui.language_card.winfo_reqheight()

        _start_and_settle(gui)
        gui.update_idletasks()
        assert gui.strategy_running_hint.winfo_ismapped()
        assert gui.language_card.winfo_reqheight() == stopped

        _stop_and_settle(gui)
        gui.update_idletasks()
        assert gui.language_card.winfo_reqheight() == stopped

    def test_start_then_stop_drives_the_controller(self, make_gui):
        gui, controller, _s = make_gui()
        _start_and_settle(gui)
        assert controller.started == 1
        assert gui._running is True

        _stop_and_settle(gui)
        assert controller.stopped >= 1
        assert gui._running is False

    def test_start_is_blocked_when_a_key_is_missing(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, controller, _s = make_gui()
        monkeypatch.setattr(app_gui, "has_usable_key", lambda _p: False)
        monkeypatch.setattr(gui, "_prompt_provider_key", lambda _p: None)

        _start_and_settle(gui)

        assert controller.started == 0, "must not start without a usable key"
        assert gui._running is False

    def test_escape_on_the_overlay_stops_but_does_not_close(self, make_gui):
        gui, controller, _s = make_gui()
        _start_and_settle(gui)

        gui._request_stop_from_subtitle()
        if gui._stop_thread is not None:
            gui._stop_thread.join(5)
        gui._poll_stop_result()

        assert controller.stopped >= 1
        assert gui.winfo_exists(), "Esc must never close the app"

    def test_escape_while_idle_does_nothing(self, make_gui):
        gui, controller, _s = make_gui()
        gui._request_stop_from_subtitle()
        assert controller.stopped == 0

    def _gated_start(self, controller):
        """Replace controller.start with one the test releases on demand."""
        gate = threading.Event()
        calls = []

        def slow_start(input_device=None):
            calls.append(input_device)
            gate.wait(5)
            controller.started += 1

        controller.start = slow_start
        return gate, calls

    def test_start_does_not_block_the_tk_thread(self, make_gui):
        """Opening a streaming session waits for the provider's confirmation —
        measured at 30+ s on the first connect after an API key changes. Run
        inline, that froze the whole window ("Keine Rückmeldung")."""
        gui, controller, _s = make_gui()
        gate, _calls = self._gated_start(controller)

        gui.on_start()

        assert gui._starting is True
        assert gui._running is False
        gui.update_idletasks()  # the Tk thread is free to keep working
        assert gui.start_btn.cget("state") == "disabled"
        assert gui.status_label.cget("text") == gui.gui_texts["connecting"]

        gate.set()
        gui._start_thread.join(5)
        gui._poll_start_result()

        assert gui._starting is False
        assert gui._running is True
        assert controller.started == 1

    def test_a_second_start_while_connecting_is_ignored(self, make_gui):
        gui, controller, _s = make_gui()
        gate, calls = self._gated_start(controller)

        gui.on_start()
        gui.on_start()  # impatient second click

        gate.set()
        gui._start_thread.join(5)
        gui._poll_start_result()
        assert len(calls) == 1, "a second session must not be opened"

    def test_a_failed_start_reports_and_stays_stopped(self, make_gui, monkeypatch):
        """The failure arrives on the worker thread; it still has to surface in
        the normal dialog and leave the panel in the stopped state."""
        gui, controller, _s = make_gui()
        alerts = []
        monkeypatch.setattr(gui, "_alert", lambda *a, **k: alerts.append(a))

        def boom(input_device=None):
            raise RuntimeError("startup timed out before session confirmation")

        controller.start = boom

        _start_and_settle(gui)

        assert gui._running is False
        assert gui._starting is False
        assert alerts, "a failed start must tell the user"
        assert "session confirmation" in alerts[0][1]
        assert gui.start_btn.cget("state") == "normal", "Start must be usable again"

    def test_stop_does_not_block_the_tk_thread(self, make_gui):
        """Closing a streaming session takes the connection lock, which a
        reconnect blocked in a slow open_stream() can hold for tens of seconds.
        Run inline and the whole window freezes for that wait — so Stop hands
        controller.stop() to a worker thread, exactly as Start does."""
        gui, controller, _s = make_gui()
        _start_and_settle(gui)

        gate = threading.Event()

        def slow_stop():
            gate.wait(5)
            controller.stopped += 1

        controller.stop = slow_stop

        gui.on_stop()

        assert gui._stopping is True
        assert gui._running is True, "still running until the stop completes"
        gui.update_idletasks()  # the Tk thread is free to keep working
        assert gui.stop_btn.cget("state") == "disabled"

        gate.set()
        gui._stop_thread.join(5)
        gui._poll_stop_result()

        assert gui._stopping is False
        assert gui._running is False
        assert controller.stopped == 1

    def test_a_second_stop_while_stopping_is_ignored(self, make_gui):
        gui, controller, _s = make_gui()
        _start_and_settle(gui)

        gate = threading.Event()
        calls = []

        def slow_stop():
            calls.append(1)
            gate.wait(5)

        controller.stop = slow_stop

        gui.on_stop()
        gui.on_stop()  # impatient second click while the first is in flight

        gate.set()
        gui._stop_thread.join(5)
        gui._poll_stop_result()
        assert len(calls) == 1, "a second stop must not run while one is in flight"


class TestApiKeyPrompt:
    """Two dialogs must never stack. The key dialog grabs input and runs its
    own event loop — a second one opened by an after() timer (grabs block
    clicks, not timers) sits invisible behind it and only surfaces once the
    first is dismissed. Reported as "after entering the key I get a second key
    prompt", with the app already running behind it."""

    def test_a_timer_cannot_stack_a_second_dialog(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        gui, _c, _s = make_gui()
        opened = []

        def fake_prompt(**kwargs):
            opened.append(kwargs["provider"])
            # Exactly what the startup timer did while this dialog was open.
            gui.on_change_key()

        monkeypatch.setattr(app_gui, "prompt_for_api_key", fake_prompt)

        gui._prompt_provider_key("openai")

        assert opened == ["openai"], "the timer must not open a second dialog"

    def test_the_prompt_is_usable_again_afterwards(self, make_gui, monkeypatch):
        """The guard must not latch: the next Start still has to be able to ask."""
        import gui.app_gui as app_gui

        gui, _c, _s = make_gui()
        opened = []
        monkeypatch.setattr(
            app_gui,
            "prompt_for_api_key",
            lambda **kwargs: opened.append(kwargs["provider"]),
        )

        gui._prompt_provider_key("openai")
        gui._prompt_provider_key("gemini")

        assert opened == ["openai", "gemini"]

    def test_the_deferred_startup_prompt_rechecks_first(self, make_gui, monkeypatch):
        """The 500 ms startup prompt fires after Start may already have asked
        for — and stored — the key."""
        import gui.app_gui as app_gui

        gui, _c, _s = make_gui()
        opened = []
        monkeypatch.setattr(
            app_gui,
            "prompt_for_api_key",
            lambda **kwargs: opened.append(kwargs["provider"]),
        )
        # The fixture pins has_usable_key True: a key exists by the time the
        # deferred callback runs.
        gui._prompt_key_if_missing()

        assert opened == [], "must not ask for a key that is already there"


class TestSettingsRemoveKeyGating:
    """The settings window's Remove button must only offer to delete a key
    that actually exists: Change stays available for any chosen provider,
    Remove follows has_usable_key, and the handler no-ops defensively."""

    def _select_openai(self, gui, monkeypatch, saved):
        import gui.settings_view as settings_view

        monkeypatch.setattr(settings_view, "has_usable_key", lambda _p: saved)
        gui._open_settings_window()
        gui.update_idletasks()
        gui.api_key_provider_combo.current(0)  # OpenAI
        gui._refresh_api_key_status()

    def test_remove_is_disabled_without_a_stored_key(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui()
        self._select_openai(gui, monkeypatch, saved=False)
        assert gui.change_key_btn.cget("state") == "normal"
        assert gui.remove_key_btn.cget("state") == "disabled"

    def test_remove_is_enabled_with_a_stored_key(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui()
        self._select_openai(gui, monkeypatch, saved=True)
        assert gui.change_key_btn.cget("state") == "normal"
        assert gui.remove_key_btn.cget("state") == "normal"

    def test_the_handler_never_removes_a_missing_key(self, make_gui, monkeypatch):
        """Even a click on a stale-enabled button must not open the remove
        flow when there is no key."""
        import gui.settings_view as settings_view

        gui, _c, _s = make_gui()
        removed = []
        monkeypatch.setattr(
            settings_view,
            "remove_api_key",
            lambda **kwargs: removed.append(kwargs["provider"]),
        )
        self._select_openai(gui, monkeypatch, saved=False)
        gui._on_settings_remove_key()
        assert removed == []

    def test_the_handler_removes_a_stored_key(self, make_gui, monkeypatch):
        import gui.settings_view as settings_view

        gui, _c, _s = make_gui()
        removed = []
        monkeypatch.setattr(
            settings_view,
            "remove_api_key",
            lambda **kwargs: removed.append(kwargs["provider"]),
        )
        self._select_openai(gui, monkeypatch, saved=True)
        gui._on_settings_remove_key()
        assert removed == ["openai"]


class TestIntegratedWindows:
    """The window_style setting: separate windows (default, while integrated
    mode is still tested on Linux) vs in-app panels.

    Integrated mode is gated to Windows (see _integrated_windows_supported),
    so the tests that drive a real in-app panel only run there — off Windows
    that configuration cannot occur, and the dim overlay would be exercised
    under exactly the conditions (no compositor, no window manager) that made
    the mode unusable on X11 in the first place. What matters on Linux is the
    gate, and test_integrated_is_gated_to_windows drives BOTH of its branches
    on every platform."""

    _windows_only = pytest.mark.skipif(
        sys.platform != "win32",
        reason="integrated mode is Windows-only; the gate is covered on all "
        "platforms by test_integrated_is_gated_to_windows",
    )

    def test_windowed_is_the_default(self, make_gui):
        gui, _c, settings = make_gui()
        assert settings.window_style == "windowed"

    @_windows_only
    def test_integrated_opens_as_in_app_panel(self, make_gui):
        gui, _c, _s = make_gui(window_style="integrated")
        gui._open_settings_window()
        gui.update_idletasks()
        win = gui._settings_win
        assert gui._modal_host.is_presented(win)
        assert bool(win.overrideredirect())
        overlay = gui._modal_host._overlay
        assert overlay is not None and overlay.winfo_exists()

    @_windows_only
    def test_escape_closes_the_panel_and_hides_the_overlay(self, make_gui):
        # Drives the host's Escape handler directly — a synthesized key event
        # needs a mapped + focused window, and this file never pumps the event
        # loop (see the module docstring).
        gui, _c, _s = make_gui(window_style="integrated")
        gui._open_settings_window()
        gui.update_idletasks()
        gui._modal_host._on_escape()
        gui.update_idletasks()
        assert not gui._settings_win_exists()
        assert gui._modal_host.active is False
        overlay = gui._modal_host._overlay
        assert overlay is not None and overlay.state() == "withdrawn"

    def test_windowed_mode_keeps_separate_windows(self, make_gui):
        gui, _c, _s = make_gui(window_style="windowed")
        gui._open_settings_window()
        gui.update_idletasks()
        win = gui._settings_win
        assert not gui._modal_host.is_presented(win)
        assert not bool(win.overrideredirect())
        assert gui._modal_host._overlay is None

    def test_integrated_is_gated_to_windows(self, make_gui, monkeypatch):
        """On X11 the dim overlay is solid black and borderless panels do not
        reliably stack above it (black screen, no popup) — integrated mode
        must fall back to separate windows off Windows even when selected.

        Both branches are driven through the platform-capability constant, so
        this runs on every host: patching sys.platform itself would apply
        process-wide (gui.widgets.sys *is* the sys module) and change what
        Tk/CustomTkinter do about titlebars mid-test."""
        import gui.widgets as widgets

        gui, _c, _s = make_gui(window_style="integrated")
        monkeypatch.setattr(widgets, "INTEGRATED_WINDOWS_SUPPORTED", True)
        assert gui._use_integrated_windows() is True
        monkeypatch.setattr(widgets, "INTEGRATED_WINDOWS_SUPPORTED", False)
        assert gui._use_integrated_windows() is False
        gui._open_settings_window()
        gui.update_idletasks()
        assert not gui._modal_host.is_presented(gui._settings_win)
        assert not bool(gui._settings_win.overrideredirect())
        # The control itself must be unreachable, not just ineffective.
        # CTkSegmentedButton.cget("state") raises (unsupported argument), so
        # read the attribute configure() stores it in.
        assert gui.window_style_segment._state == "disabled"
        assert gui.window_style_segment.get() == gui.gui_texts.get(
            "window_style_windowed", "Windows"
        )

    def test_segment_round_trips_the_setting(self, make_gui):
        gui, _c, settings = make_gui(window_style="integrated")
        gui._open_settings_window()
        gui.update_idletasks()
        gui._on_window_style_change(
            gui.gui_texts.get("window_style_windowed", "Windows")
        )
        assert settings.window_style == "windowed"
        gui._on_window_style_change(
            gui.gui_texts.get("window_style_integrated", "Integrated")
        )
        assert settings.window_style == "integrated"

    @_windows_only
    def test_announce_panel_routes_resize_through_the_host(self, make_gui):
        gui, _c, _s = make_gui(window_style="integrated")
        gui._open_announce_window()
        gui.update_idletasks()
        win = gui._announce_win
        assert gui._modal_host.is_presented(win)
        # The natural-height resize must not screen-centre an in-app panel.
        gui._resize_announce_window()
        assert gui._modal_host.is_presented(win)
        gui._close_announce_window()
        assert gui._modal_host.active is False


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
        # subtitle_hide_mode="stopped" normally destroys the overlay on stop,
        # but an active "until stopped" announcement survives the stop when the
        # announcement window's "hide when stopped" toggle is off.
        gui, _c, _s = make_gui(
            subtitle_hide_mode="stopped", stop_announcement_on_live_stop=False
        )
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui._announcement_text_active = "Stays up"
        _stop_and_settle(gui)
        assert gui.subtitle_window is fake
        assert fake.destroyed is False

    def test_on_stop_clears_announcement_when_toggle_is_on(self, make_gui):
        # Default: stopping the session also clears an in-progress
        # announcement, which then lets the hide policy tear the overlay down.
        gui, _c, settings = make_gui(subtitle_hide_mode="stopped")
        assert settings.stop_announcement_on_live_stop is True
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui._announcement_text_active = "Goes away"
        _stop_and_settle(gui)
        assert gui._has_active_announcement() is False
        assert gui.subtitle_window is None
        assert fake.destroyed is True

    def test_on_stop_destroys_overlay_without_announcement(self, make_gui):
        gui, _c, _s = make_gui(subtitle_hide_mode="stopped")
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        _stop_and_settle(gui)
        assert gui.subtitle_window is None
        assert fake.destroyed is True


class TestSubtitleCloseRequest:
    """Closing the overlay (taskbar close / Alt+F4) must never quit the app.

    Its WM_DELETE_WINDOW routes to _request_close_from_subtitle — stop a
    running session (like Esc) and destroy only the overlay. The full app
    shutdown (on_close) is reserved for the control panel."""

    def test_overlay_close_protocol_is_not_the_app_shutdown(self, make_gui):
        gui, _c, _s = make_gui()
        gui._create_subtitle_window()  # real overlay, real protocol wiring
        win = gui.subtitle_window
        assert win is not None and win.winfo_exists()
        assert win._on_close == gui._request_close_from_subtitle
        win._on_close()  # what WM_DELETE_WINDOW invokes
        assert gui.subtitle_window is None
        assert gui.winfo_exists()  # the control panel is still up

    def test_close_while_stopped_destroys_only_the_overlay(self, make_gui):
        gui, controller, _s = make_gui()
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._request_close_from_subtitle()
        assert fake.destroyed is True
        assert gui.subtitle_window is None
        assert controller.stopped == 0  # nothing ran, nothing to stop

    def test_close_while_running_stops_the_session_first(self, make_gui):
        gui, controller, _s = make_gui()
        fake = _FakeSubtitleWindow()
        gui.subtitle_window = fake
        gui._running = True
        gui._request_close_from_subtitle()
        thread = gui._stop_thread
        if thread is not None:
            thread.join(5.0)
        gui._poll_stop_result()
        assert controller.stopped == 1
        assert gui._running is False
        assert fake.destroyed is True
        assert gui.subtitle_window is None
        assert gui.winfo_exists()


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
        test, so _align_advanced_card would bail out before measuring).

        The window scaling is pinned to 1.0 so one logical unit is one pixel
        and the arithmetic below reads plainly — that is the factor the pixel
        delta is divided by (see TestAdvancedAlignConvergence)."""
        monkeypatch.setattr(gui, "_get_window_scaling", lambda: 1.0, raising=False)
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


class _AlignGroup:
    """A card group whose rendered position answers to the current gap.

    Only the four geometry calls _align_advanced_card makes are implemented,
    plus the no-op grid() _layout_sidebar_cards performs on it.
    """

    def __init__(self, rooty, height, gui=None, scaling=1.0, base=0):
        self._rooty = rooty
        self._height = height
        self._gui = gui
        self._scaling = scaling
        self._base = base

    def winfo_ismapped(self):
        return True

    def winfo_rooty(self):
        if self._gui is None:
            return self._rooty
        # Padding above Advanced pushes it down by gap × the widget scaling —
        # exactly what CustomTkinter does with a logical pady on its way into
        # grid(). This is the feedback path the alignment loop closes over.
        return self._base + round(self._gui._advanced_gap * self._scaling)

    def winfo_height(self):
        return self._height

    def grid(self, **_kwargs):
        pass


class TestAdvancedAlignConvergence:
    """_align_advanced_card is a feedback loop: it measures the rendered
    bottom-edge delta, re-pads Advanced, and the resulting <Configure> queues
    another pass. The delta is real pixels and the padding is logical, so the
    divisor must be the WINDOW scaling (DPI × design clamp). Dividing by
    _responsive_scale (the clamp alone) overshot every correction by the DPI
    factor and round() locked the gap into a two-value cycle, which re-queued
    this pass forever and froze the control panel — reported 2026-07-29 after
    collapsing the log panel on a 1.5× DPI screen.
    """

    _SCALING = 1.5  # a real 150% display, deliberately != _responsive_scale

    def _rig(self, gui, monkeypatch):
        """Two columns, Advanced 40px short of the display column's bottom."""
        monkeypatch.setattr(
            gui, "_get_window_scaling", lambda: self._SCALING, raising=False
        )
        monkeypatch.setattr(
            gui.sidebar,
            "winfo_width",
            lambda: int(gui._COL2_MIN_W * self._SCALING),
            raising=False,
        )
        gui._log_collapsed = True
        gui._typography_open = False
        gui._applied_columns = 2
        gui._advanced_gap = 0
        monkeypatch.setattr(gui, "_col_a", _AlignGroup(100, 400), raising=False)
        monkeypatch.setattr(
            gui,
            "_col_c",
            _AlignGroup(0, 200, gui=gui, scaling=self._SCALING, base=260),
            raising=False,
        )

    def _run(self, gui, passes=25):
        """Drive the loop by hand: after_idle never fires without a mainloop."""
        history = []
        for _ in range(passes):
            gui._advanced_align_pending = False
            gui._align_advanced_card()
            history.append(gui._advanced_gap)
            if len(history) >= 2 and history[-1] == history[-2]:
                break  # reached a fixed point
        return history

    def test_the_gap_reaches_a_fixed_point(self, make_gui, monkeypatch):
        gui, _c, _s = make_gui()
        self._rig(gui, monkeypatch)
        history = self._run(gui)
        assert history[-1] == history[-2], f"never settled: {history}"

    def test_the_gap_never_cycles_between_two_values(self, make_gui, monkeypatch):
        """The exact freeze signature: [34, 37, 34, 37, ...] forever."""
        gui, _c, _s = make_gui()
        self._rig(gui, monkeypatch)
        history = self._run(gui)
        settled = history[-1]
        assert history.count(settled) >= 2
        # No value may reappear after the run has moved past it — a repeat is
        # a limit cycle, which is what starved the idle queue.
        for index, gap in enumerate(history[:-2]):
            assert gap not in history[index + 1 : -1], f"cycle in {history}"

    def test_the_correction_divides_by_the_window_scaling(
        self, make_gui, monkeypatch
    ):
        """One pass over a known delta, so a wrong divisor is unambiguous."""
        gui, _c, _s = make_gui()
        self._rig(gui, monkeypatch)
        # Static column C: the delta is a fixed 40px regardless of the gap.
        monkeypatch.setattr(gui, "_col_c", _AlignGroup(260, 200), raising=False)
        gui._responsive_scale = 0.86  # what the buggy divisor used
        gui._advanced_align_pending = False
        gui._align_advanced_card()
        assert gui._advanced_gap == round(40 / self._SCALING)  # 27, not 47


class _Configure:
    """The one field of a <Configure> event the wordmark fitter reads."""

    def __init__(self, width):
        self.width = width


class TestBrandWordmark:
    """The header wordmark is dropped, not squeezed, when the buttons leave no
    room: a compressed CTkLabel centre-clips its text ("inbarLi"), which reads
    as a rendering fault. Opening the log panel halves the sidebar and is the
    layout where this actually bites."""

    def _header_width_for(self, gui, text_width):
        """The header width that leaves exactly text_width px for the wordmark."""
        width = (
            text_width
            + gui._header_buttons_span()
            + sum(gui._grid_padx(gui._brand_frame))
            + gui._BRAND_TITLE_GAP
        )
        logo = gui._brand_logo_label
        if logo is not None and logo.winfo_manager() == "pack":
            width += logo.winfo_reqwidth() + gui._BRAND_LOGO_GAP
        return width

    def _shown(self, gui):
        return gui._brand_title_label.winfo_manager() == "pack"

    def test_shown_when_it_exactly_fits(self, make_gui):
        gui, _c, _s = make_gui()
        needed = gui._brand_title_label.winfo_reqwidth()
        gui._fit_brand_wordmark(_Configure(self._header_width_for(gui, needed)))
        assert self._shown(gui) is True

    def test_dropped_one_pixel_short(self, make_gui):
        gui, _c, _s = make_gui()
        needed = gui._brand_title_label.winfo_reqwidth()
        gui._fit_brand_wordmark(_Configure(self._header_width_for(gui, needed - 1)))
        assert self._shown(gui) is False

    def test_comes_back_after_the_logo_when_room_returns(self, make_gui):
        """Re-packing must not put the wordmark in front of the logo."""
        gui, _c, _s = make_gui()
        needed = gui._brand_title_label.winfo_reqwidth()
        gui._fit_brand_wordmark(_Configure(self._header_width_for(gui, needed - 1)))
        assert self._shown(gui) is False

        gui._fit_brand_wordmark(_Configure(self._header_width_for(gui, needed)))
        assert self._shown(gui) is True
        assert gui._brand_frame.pack_slaves()[-1] is gui._brand_title_label

    def test_decision_ignores_the_previous_layout(self, make_gui, monkeypatch):
        """Regression: <Configure> carries the NEW header width while the
        buttons are still where the PREVIOUS layout put them. Reading their
        positions inverted the whole feature — the wordmark vanished on a
        widened header and stayed (clipped) on a narrowed one."""
        gui, _c, _s = make_gui()
        needed = gui._brand_title_label.winfo_reqwidth()
        wide = self._header_width_for(gui, needed)
        narrow = self._header_width_for(gui, needed - 1)

        # Stale positions from the opposite layout must not change the outcome.
        monkeypatch.setattr(gui._history_btn, "winfo_x", lambda: 0, raising=False)
        monkeypatch.setattr(gui._brand_frame, "winfo_x", lambda: 0, raising=False)
        gui._fit_brand_wordmark(_Configure(wide))
        assert self._shown(gui) is True

        monkeypatch.setattr(gui._history_btn, "winfo_x", lambda: 10_000, raising=False)
        gui._fit_brand_wordmark(_Configure(narrow))
        assert self._shown(gui) is False

    def test_unlaid_out_header_is_left_alone(self, make_gui, monkeypatch):
        """Before the first layout the header has no width; hiding the wordmark
        on that would drop it on every start-up."""
        gui, _c, _s = make_gui()
        monkeypatch.setattr(
            gui._sidebar_header, "winfo_width", lambda: 1, raising=False
        )
        gui._fit_brand_wordmark()
        assert self._shown(gui) is True

    def test_button_span_matches_the_rendered_layout(self, make_gui):
        """The span replaces reading the first button's position, so it has to
        agree with it once the layout has settled."""
        gui, _c, _s = make_gui()
        gui.update_idletasks()
        header_width = gui._sidebar_header.winfo_width()
        if header_width <= 1 or gui._history_btn.winfo_x() <= 1:
            pytest.skip("header not laid out in this environment")
        assert (
            header_width - gui._header_buttons_span() == gui._history_btn.winfo_x()
        )


class TestPaintBeforeReveal:
    """Windows must be invisible until they have painted.

    CTk widgets draw on the <Configure> events that follow mapping, not at
    construction, so a window shown at the end of its build appears empty and
    fills in over ~0.6 s — the "you can watch it build itself" effect. Every
    window therefore builds transparent and fades in one settle beat later.
    """

    # Opacity can only be asserted where the platform applies it (see
    # _probe_display); the reveal logic itself is checked everywhere.
    _needs_alpha = pytest.mark.skipif(
        not _ALPHA_HONOURED, reason="per-window opacity not applied by this display"
    )

    @staticmethod
    def _alpha(win) -> float:
        return float(win.attributes("-alpha"))

    def test_control_panel_is_built_transparent(self, make_gui):
        gui, _c, _s = make_gui()
        assert gui._reveal_pending is True
        if _ALPHA_HONOURED:
            assert self._alpha(gui) == 0.0

    def test_surface_restore_does_not_show_an_unpainted_window(self, make_gui):
        """_restore_control_window_surface runs from an after_idle() during
        start-up; forcing the window opaque there would undo the guard."""
        gui, _c, _s = make_gui()
        gui._restore_control_window_surface()
        assert gui._reveal_pending is True
        if _ALPHA_HONOURED:
            assert self._alpha(gui) == 0.0

    @_needs_alpha
    def test_surface_restore_still_repairs_a_revealed_window(self, make_gui):
        gui, _c, _s = make_gui()
        gui._reveal_pending = False
        gui.attributes("-alpha", 0.3)  # e.g. left behind by the overlay
        gui._restore_control_window_surface()
        assert self._alpha(gui) == 1.0

    def test_map_of_a_child_widget_does_not_reveal(self, make_gui):
        """<Map> reaches the toplevel's bindtag for every descendant, so the
        first dropdown popup would otherwise reveal the unpainted panel."""
        gui, _c, _s = make_gui()
        scheduled = []
        gui.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))

        class _Event:
            widget = gui.sidebar

        gui._reveal_control_window(_Event())
        assert scheduled == []
        assert gui._reveal_pending is True

    def test_reveal_fades_in_after_the_settle_beat(self, make_gui):
        gui, _c, _s = make_gui()
        scheduled = []
        gui.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))

        gui._reveal_control_window()
        assert [ms for ms, _fn in scheduled] == [gui._REVEAL_SETTLE_MS]
        assert gui._reveal_pending is True  # still hidden until the beat elapses
        if _ALPHA_HONOURED:
            assert self._alpha(gui) == 0.0

        scheduled[0][1]()  # the timer fires
        assert gui._reveal_pending is False
        if _ALPHA_HONOURED:
            assert self._alpha(gui) == 1.0

    def test_reveal_happens_only_once(self, make_gui):
        gui, _c, _s = make_gui()
        gui._reveal_pending = False
        scheduled = []
        gui.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))
        gui._reveal_control_window()
        assert scheduled == []

    def test_build_schedules_a_reveal_backstop(self, make_gui):
        """The <Map> the reveal is keyed to is delivered by CustomTkinter's
        withdraw/deiconify in mainloop(), which is Windows-only — everywhere
        else the root is mapped before our bind exists and the event never
        arrives. Without this timer the panel stayed fully transparent for the
        whole session (reported on macOS: clickable, its dropdowns visible, its
        own surface not)."""
        gui, _c, _s = make_gui()
        scheduled = []
        gui.after = lambda ms, fn=None, *a: scheduled.append((ms, fn))

        gui._finalize_setup()

        assert (gui._REVEAL_BACKSTOP_MS, gui._reveal_control_window) in scheduled
        # Late enough that the <Map> path always wins on Windows.
        assert gui._REVEAL_BACKSTOP_MS > gui._REVEAL_SETTLE_MS


class _LevelSnapshot:
    def __init__(self, rms_dbfs: float, clipping_ratio: float = 0.0):
        self.rms_dbfs = rms_dbfs
        self.clipping_ratio = clipping_ratio


class TestIdleMeterCosts:
    """The level meter polls 20x a second; an unchanged reading must cost no
    redraw (it used to reconfigure a label and three progress bars per tick,
    forever, silent input included)."""

    def test_unchanged_level_does_not_redraw_the_bar(self, make_gui):
        gui, _c, _s = make_gui()
        bar = gui.input_level_bar
        bar.set(0.42)
        calls = []
        for segment in bar._segments:
            segment.set = lambda value, _c=calls: _c.append(value)
        bar.set(0.42)
        assert calls == []
        bar.set(0.43)
        assert len(calls) == len(bar._segments)

    def test_unchanged_readout_does_not_reconfigure_the_label(self, make_gui):
        gui, controller, _s = make_gui()
        controller.get_input_level = lambda: _LevelSnapshot(-24.0)
        calls = []
        gui.input_level_value_label.configure = lambda **kw: calls.append(kw)

        gui._poll_input_level()
        assert len(calls) == 1  # first reading is written
        gui._poll_input_level()
        assert len(calls) == 1  # identical reading: no redraw

        controller.get_input_level = lambda: _LevelSnapshot(-12.0)
        gui._poll_input_level()
        assert len(calls) == 2  # a changed reading still gets through


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


class TestLoopbackHint:
    """Where the platform has no loopback capture (macOS), the input-device
    card explains where system audio comes from instead of leaving the user
    hunting for their speakers in the list."""

    @staticmethod
    def _hints(gui):
        return [
            label
            for label in gui._muted_labels
            if getattr(label, "_text_key", None) == "macos_loopback_hint"
        ]

    def test_shown_where_the_platform_has_no_loopback(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        monkeypatch.setattr(app_gui, "loopback_supported", lambda: False)
        gui, _c, _s = make_gui()

        hints = self._hints(gui)
        assert len(hints) == 1
        assert hints[0].cget("text").strip()  # a real string, not the raw key

    def test_absent_where_loopback_works(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        monkeypatch.setattr(app_gui, "loopback_supported", lambda: True)
        gui, _c, _s = make_gui()

        assert self._hints(gui) == []

    def test_follows_a_gui_language_change(self, make_gui, monkeypatch):
        import gui.app_gui as app_gui

        monkeypatch.setattr(app_gui, "loopback_supported", lambda: False)
        gui, _c, _s = make_gui()

        gui.gui_texts["macos_loopback_hint"] = "Systemton via BlackHole"
        gui._update_all_ui_texts()

        assert self._hints(gui)[0].cget("text") == "Systemton via BlackHole"

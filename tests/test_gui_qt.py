"""Regression tests for the Qt GUI tree (issue #44).

Every test here locks in a defect that reached a real run. They are written
against behaviour, not pixels — the layout assertions check where blocks are
placed, which is what actually broke.

PySide6 is not in requirements.txt while the shipping GUI is still
CustomTkinter, so the whole module skips when it is absent.

Note on headless CI: QT_QPA_PLATFORM=offscreen works for these tests, but on
Windows that platform plugin loads NO system fonts and renders every glyph as
tofu. Geometry still measures, so these pass; anything asserting on rendered
text appearance would not.
"""

from __future__ import annotations

import queue
import sys
import threading

import pytest

pytest.importorskip("PySide6", reason="Qt GUI tree is optional during migration")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_qt.subtitle_window import SubtitleWindow  # noqa: E402
from utils.settings import (  # noqa: E402
    PIPELINE_MODE_STREAMING,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    SUBTITLE_MODES,
)


def cp_module():
    """The control-panel module, imported lazily so this file still collects
    without a display."""
    import gui_qt.control_panel as cp

    return cp


PAIRS = [
    ("Im Namen Allahs, des Allerbarmers, des Barmherzigen.", "بسم الله الرحمن الرحيم"),
    ("Alles Lob gebuehrt Allah ﷻ, dem Herrn der Welten.", "الحمد لله رب العالمين"),
    ("Gibt es einen Schoepfer ausser Allah?", "هل من خالق غير الله؟"),
]


@pytest.fixture(autouse=True)
def pinned_window_settings():
    """Open every panel at its default size, in separate windows.

    ``load_settings()`` hands out one cached object, and a closing panel writes
    its geometry into it — so without this, one test's window size decides the
    next test's column count (and could reach the real settings.json through
    any unstubbed save).

    ``window_style`` for the same reason: a developer whose settings.json says
    "integrated" ran the whole suite against in-app panels, where a secondary
    window is clamped to the control panel instead of opening at its own size.
    Tests that want that style ask for it.
    """
    from utils.settings import load_settings

    settings = load_settings()
    saved = (
        settings.window_geometry,
        settings.window_maximized,
        settings.window_style,
    )
    settings.window_geometry, settings.window_maximized = "", False
    settings.window_style = "windowed"
    yield
    (
        settings.window_geometry,
        settings.window_maximized,
        settings.window_style,
    ) = saved


@pytest.fixture(scope="session")
def qt_app():
    """One QApplication for the session — Qt allows only a single instance."""
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def overlay(qt_app):
    """A sized overlay. Never shown: rendering is not what these test."""
    made: list[SubtitleWindow] = []

    def _make(mode: str, **kwargs) -> SubtitleWindow:
        w = SubtitleWindow(monitor_index=0, subtitle_mode=mode, **kwargs)
        w.resize(1400, 700)
        made.append(w)
        return w

    yield _make
    for w in made:
        w.destroy()


def _settle(app, rounds: int = 5) -> None:
    """Let queued layout work finish.

    A geometry assertion needs more than one pass: the levelling is queued for
    after the layout settles, and the minimum height it sets then needs another
    pass before the widgets have actually moved.
    """
    for _ in range(rounds):
        app.processEvents()


def _visible(w: SubtitleWindow, block) -> bool:
    h = w._measure_block(block)
    return block.y + h > 0 and block.y < w._content_height()


class TestContinuousMode:
    """The mode that showed nothing at all during a live session."""

    def test_first_subtitle_is_visible_immediately(self, overlay):
        # Regression: blocks were placed at y == content_height, i.e. entirely
        # below the visible area, and had to scroll up for seconds first.
        w = overlay(SUBTITLE_MODE_CONTINUOUS, bilingual_mode=True)
        w.add_subtitle(*PAIRS[0][:1], source_text=PAIRS[0][1])
        block = w._blocks[0]
        assert _visible(w, block)
        assert block.y < w._content_height()
        assert w.get_subtitle_backlog_count() == 0

    def test_second_subtitle_queues_below_the_first(self, overlay):
        w = overlay(SUBTITLE_MODE_CONTINUOUS, bilingual_mode=True)
        for de, ar in PAIRS[:2]:
            w.add_subtitle(de, source_text=ar)
        first, second = w._blocks
        assert second.y >= first.y + w._measure_block(first)  # no overlap

    def test_subtitle_after_scrolling_reanchors_to_the_bottom(self, overlay):
        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        w.add_subtitle("Erste Zeile.")
        for _ in range(400):
            w._advance_scroll()
        w.add_subtitle("Zweite Zeile nach dem Scrollen.")
        assert _visible(w, w._blocks[-1])

    def test_offscreen_blocks_are_evicted(self, overlay, qt_app):
        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        for de, _ in PAIRS:
            w.add_subtitle(de)
        for _ in range(4000):
            w._advance_scroll()
        w.render(w.grab())  # force a paint, which performs eviction
        assert w._blocks == []

    def test_backlog_counts_queued_blocks(self, overlay):
        # Regression: the previous model recomputed positions from a running
        # offset each frame, so the backlog was structurally always zero and
        # adaptive catch-up could never engage.
        w = overlay(SUBTITLE_MODE_CONTINUOUS, bilingual_mode=True)
        for de, ar in PAIRS * 5:
            w.add_subtitle(de, source_text=ar)
        assert w.get_subtitle_backlog_count() > 0

    def test_other_modes_report_no_backlog(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC)
        w.add_subtitle("Nur eine Zeile.")
        assert w.get_subtitle_backlog_count() == 0


class TestRealtimeMode:
    """The default streaming mode: a top-down feed that must not run off."""

    @staticmethod
    def _feed(w, text, source):
        """Add a block and let the feed finish sliding to its new position.

        The offset no longer jumps the instant a block lands — it eases there
        over about half a second, which TestRealtimeFeedAnimation covers. These
        tests are about where the feed ENDS UP, so they run the slide out. The
        second render is what evicts: eviction happens during paint, against
        the offset the animation actually reached.
        """
        w.add_subtitle(text, source_text=source)
        w.render(w.grab())
        for _ in range(400):
            if not w._feed_timer.isActive():
                break
            w._step_feed_anim()
        w.render(w.grab())

    def test_feed_shifts_up_and_never_slides_back_down(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        offsets = []
        for i in range(16):
            self._feed(w, f"Zeile {i}: {PAIRS[1][0]}", PAIRS[1][1])
            offsets.append(w._scroll_offset)
        assert offsets[-1] > 0, "feed never shifted despite overflowing"
        # Pairwise: the second sequence is one shorter by construction.
        assert all(b >= a for a, b in zip(offsets, offsets[1:], strict=False))

    def test_newest_block_stays_within_the_content_area(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        for i in range(16):
            self._feed(w, f"Zeile {i}: {PAIRS[1][0]}", PAIRS[1][1])
        heights = [w._measure_block(b) for b in w._blocks]
        top = int(w.height() * 0.06)
        # From the window's own stacking rule rather than the raw constant: the
        # gap above a block is REALTIME_BLOCK_SPACING less whatever blank band
        # that block's first line carries (see _block_gap).
        spacing = sum(
            h + w._block_gap(nxt)
            for h, nxt in zip(heights[:-1], w._blocks[1:], strict=True)
        )
        newest_y = top - w._scroll_offset + spacing
        assert newest_y + heights[-1] <= w._content_height() + 5

    def test_blocks_scrolled_off_the_top_are_evicted(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        for i in range(30):
            self._feed(w, f"Zeile {i}: {PAIRS[0][0]}", PAIRS[0][1])
        assert len(w._blocks) < 30


class TestAdaptiveCatchup:
    def test_ramps_up_capped_and_smoothed(self, overlay):
        w = overlay(
            SUBTITLE_MODE_CONTINUOUS,
            bilingual_mode=True,
            adaptive_catchup=True,
            scroll_speed=1.0,
        )
        for de, ar in PAIRS * 5:
            w.add_subtitle(de, source_text=ar)
        speeds = [w._current_scroll_speed() for _ in range(40)]
        assert speeds[-1] > speeds[0], "catch-up never engaged"
        assert speeds[-1] <= 2.0 + 1e-6, "exceeded the 2x readability cap"
        # EMA-smoothed: the speed change must not be a jolt.
        assert all(b >= a - 1e-9 for a, b in zip(speeds, speeds[1:], strict=False))

    def test_disabling_returns_to_the_base_speed(self, overlay):
        w = overlay(
            SUBTITLE_MODE_CONTINUOUS, adaptive_catchup=True, scroll_speed=1.0
        )
        for de, _ in PAIRS * 5:
            w.add_subtitle(de)
        for _ in range(40):
            w._current_scroll_speed()
        w.set_adaptive_catchup(False)
        for _ in range(80):
            speed = w._current_scroll_speed()
        assert speed == pytest.approx(1.0, abs=0.05)


class TestFooterReserve:
    def test_content_never_extends_under_the_pills(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC, show_footer=True)
        assert w._content_height() < w.height()
        reserved_without_hint = w.reserved_bottom()
        w.set_stopped_hint(True)
        assert w.reserved_bottom() > reserved_without_hint

    def test_no_reserve_when_the_footer_is_off(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC, show_footer=False)
        assert w.reserved_bottom() == 0

    def test_the_pill_font_is_fixed_and_bold(self, overlay):
        # It used to be derived from the subtitle size, so at a large font the
        # disclaimer grew into a banner. The Tk overlay draws it at a constant
        # 14pt bold.
        from gui_qt.subtitle_window import PILL_FONT_PX

        w = overlay(SUBTITLE_MODE_STATIC, show_footer=True, font_size_base=40)
        small = w._pill_font()
        assert small.pixelSize() == PILL_FONT_PX
        assert small.bold()
        for _ in range(4):
            w.increase_font()
        assert w._pill_font().pixelSize() == PILL_FONT_PX


class TestBackdropOpacity:
    """Adjustable only because Qt composites real per-pixel alpha."""

    def test_default_preserves_the_shipped_look(self, overlay):
        # The user reviewed the shipped backdrop against live video and chose
        # to keep it, so the default must stay exactly alpha 190/255.
        from utils.settings import DEFAULT_BACKDROP_OPACITY

        w = overlay(SUBTITLE_MODE_REALTIME)
        assert w.get_backdrop_opacity() == DEFAULT_BACKDROP_OPACITY
        assert w._backdrop().alpha() == 191  # round(75 * 255 / 100)

    def test_zero_is_fully_transparent(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        w.set_backdrop_opacity(0)
        assert w._backdrop().alpha() == 0

    def test_full_is_opaque(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        w.set_backdrop_opacity(100)
        assert w._backdrop().alpha() == 255

    def test_out_of_range_is_clamped(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        w.set_backdrop_opacity(-40)
        assert w.get_backdrop_opacity() == 0
        w.set_backdrop_opacity(400)
        assert w.get_backdrop_opacity() == 100

    def test_transparent_static_still_wins(self, overlay):
        # The static-mode transparent option must not be overridden by a
        # non-zero backdrop opacity.
        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True)
        w.set_backdrop_opacity(100)
        assert w._backdrop().alpha() == 0

    def test_setting_round_trips_through_disk(self, tmp_path, monkeypatch):
        import utils.settings as S

        path = tmp_path / "settings.json"
        monkeypatch.setattr(S, "_settings_path", lambda: path)
        s = S.load_settings(use_cache=False)
        assert s.subtitle_backdrop_opacity == S.DEFAULT_BACKDROP_OPACITY
        s.subtitle_backdrop_opacity = 20
        S.save_settings(s)
        assert S.load_settings().subtitle_backdrop_opacity == 20

    def test_out_of_range_on_disk_is_clamped(self, tmp_path, monkeypatch):
        import json

        import utils.settings as S

        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"subtitle_backdrop_opacity": 999}), encoding="utf-8")
        monkeypatch.setattr(S, "_settings_path", lambda: path)
        loaded = S.load_settings(use_cache=False)
        assert loaded.subtitle_backdrop_opacity == S.BACKDROP_OPACITY_MAX


class TestFontSizeBase:
    def test_base_is_a_divisor_so_larger_font_means_smaller_base(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC, font_size_base=40)
        before = w.get_current_font_size()
        w.increase_font()
        assert w.get_font_size_base() < 40
        assert w.get_current_font_size() > before

    def test_steppers_clamp(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC, font_size_base=40)
        for _ in range(50):
            w.increase_font()
        assert w.get_font_size_base() == 20
        for _ in range(50):
            w.decrease_font()
        assert w.get_font_size_base() == 80


class TestLifecycle:
    def test_hide_drops_the_live_line_and_stops_animating(self, overlay):
        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        w.set_live_text("نص مؤقت", False)
        w.hide()
        assert w._live_text is None
        assert not w._scroll_timer.isActive()

    def test_scroll_timer_runs_only_in_continuous_mode(self, overlay):
        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        assert w._scroll_timer.isActive()
        w.set_subtitle_mode(SUBTITLE_MODE_STATIC)
        assert not w._scroll_timer.isActive()

    def test_always_on_top_never_loses_the_window(self, overlay, qt_app):
        # setWindowFlag recreates the native window: it used to hide the widget
        # (the overlay vanished for good) and it repaints from an empty surface
        # (a white flash). The flag goes to the QWindow instead, so the native
        # window has to survive every toggle.
        from gui_qt.widgets import is_window_on_top

        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        w.show()
        qt_app.processEvents()
        native = int(w.winId())
        for enabled in (True, False, True, True, False):
            w.set_always_on_top(enabled)
            qt_app.processEvents()
            assert w.isVisible(), f"hidden after set_always_on_top({enabled})"
            assert is_window_on_top(w) is enabled
            assert int(w.winId()) == native, "the native window was recreated"
        w.hide()

    def test_the_overlay_is_a_real_window_for_the_taskbar_and_obs(self, overlay):
        # A Qt.Tool is kept out of the taskbar, the alt-tab list and OBS's
        # window-capture list; window capture is how most operators get
        # subtitles into a stream, so the Tk overlay forces the opposite.
        from PySide6.QtCore import Qt

        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        # The window TYPE is a masked value, not a bit — "& Qt.Tool" is true
        # for a plain Qt.Window too, and would pass whatever this is.
        assert (w.windowFlags() & Qt.WindowType_Mask) == Qt.Window
        assert w.windowFlags() & Qt.FramelessWindowHint
        assert w.windowTitle() == "MinbarLive Subtitles"

    def test_a_non_topmost_overlay_stays_clear_of_the_taskbar(self, overlay, qt_app):
        # Only a topmost window paints over the taskbar. Laid out on the full
        # screen without that flag, the taskbar covers the disclaimer pill and
        # the last line of every subtitle.
        from PySide6.QtGui import QGuiApplication

        screen = QGuiApplication.screens()[0]
        if screen.availableGeometry().height() == screen.geometry().height():
            pytest.skip("no taskbar/dock reserved on this screen")
        w = overlay(SUBTITLE_MODE_CONTINUOUS, always_on_top=False)
        w.show()
        qt_app.processEvents()
        assert w.set_always_on_top(False) is None
        bottom_off = w.geometry().y() + w.geometry().height()
        w.set_always_on_top(True)
        qt_app.processEvents()
        bottom_on = w.geometry().y() + w.geometry().height()
        assert bottom_off == screen.availableGeometry().bottom() + 1
        assert bottom_on == screen.geometry().bottom() + 1
        w.hide()

    def test_the_overlay_takes_a_taskbar_button(self, overlay):
        # Qt.WindowDoesNotAcceptFocus sets WS_EX_NOACTIVATE, and Windows keeps
        # such a window OFF the taskbar unless WS_EX_APPWINDOW is forced on too
        # — the flag the Tk overlay sets by hand. Whether the button appeared
        # was then down to when the shell looked, so it came and went between
        # runs. WA_ShowWithoutActivating keeps the half that matters.
        from PySide6.QtCore import Qt

        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        assert not (w.windowFlags() & Qt.WindowDoesNotAcceptFocus)
        assert w.testAttribute(Qt.WA_ShowWithoutActivating)

    @pytest.mark.skipif(
        sys.platform != "win32", reason="WS_EX_NOACTIVATE is a Windows style"
    )
    def test_the_native_window_is_not_ws_ex_noactivate(self, overlay, qt_app):
        # The rule above is a native one, so assert on the native style: a
        # future flag change could reintroduce it without touching the Qt flag
        # this class otherwise checks.
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetWindowLongW.restype = ctypes.c_long
        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        w.show()
        qt_app.processEvents()
        ex_style = user32.GetWindowLongW(int(w.winId()), -20) & 0xFFFFFFFF
        w.hide()
        assert not ex_style & 0x08000000, "WS_EX_NOACTIVATE keeps it off the taskbar"
        assert not ex_style & 0x00000080, "WS_EX_TOOLWINDOW hides it from OBS"


class TestWindowIcon:
    """The taskbar button drew a pale smudge: the shipped .ico carries the full
    vertical lockup — mark, wordmark and tagline — at every one of its sizes."""

    def test_the_icon_covers_the_sizes_windows_asks_for(self, qt_app):
        from gui_qt.icons import ICON_SIZES, app_icon

        icon = app_icon()
        assert icon is not None and not icon.isNull()
        assert set(ICON_SIZES) <= {size.width() for size in icon.availableSizes()}

    def test_the_taskbar_size_is_drawn_and_square(self, qt_app):
        from gui_qt.icons import app_icon

        pixmap = app_icon().pixmap(24, 24)
        # The returned pixmap is in DEVICE pixels — 30x30 on a 125% display.
        assert pixmap.width() == pixmap.height()
        assert round(pixmap.width() / pixmap.devicePixelRatio()) == 24
        image = pixmap.toImage()
        painted = sum(
            1
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 8
        )
        assert painted > 20, "the 24px icon is blank"

    def test_the_mark_is_centred_on_its_square(self):
        # The mark is wider than tall (402x256 for the shipped artwork), so it
        # is scaled by its longer side and centred rather than stretched.
        from config import ICON_PATH_PNG_ON_DARK
        from utils.icons import square_marks

        squares = square_marks(ICON_PATH_PNG_ON_DARK, (32, 64))
        assert [s.size for s in squares] == [(32, 32), (64, 64)]
        for square in squares:
            box = square.getbbox()
            assert box is not None, "nothing drawn"
            above, below = box[1], square.height - box[3]
            assert abs(above - below) <= 1, f"not vertically centred: {box}"

    def test_utils_icons_does_not_pull_tk_into_the_process(self):
        # gui_qt/control_panel.py and gui_qt/icons.py both call logo_mark, and
        # a module-level "import tkinter" there loaded 50-odd Tk modules into
        # the Qt process. Run in a subprocess: the suite imports the Tk tree
        # elsewhere, so sys.modules in THIS process proves nothing.
        import pathlib
        import subprocess

        result = subprocess.run(
            [sys.executable, "-c", "import sys, utils.icons; print('tkinter' in sys.modules)"],
            capture_output=True,
            text=True,
            cwd=pathlib.Path(__file__).resolve().parents[1],
        )
        assert result.stdout.strip() == "False", result.stderr

    def test_the_whole_qt_tree_pulls_no_tk_into_the_process(self):
        # Every window the Qt tree can open, including the popups: the
        # already-running dialog used to be CustomTkinter whatever tree was
        # asked for, which put a live Tcl interpreter beside the Qt one and
        # took the DPI awareness away from Qt.
        import pathlib
        import subprocess

        modules = (
            "gui_qt.app",
            "gui_qt.already_running",
            "gui_qt.api_keys",
            "gui_qt.announce_window",
            "gui_qt.batch_window",
            "gui_qt.control_panel",
            "gui_qt.history_window",
            "gui_qt.onboarding",
            "gui_qt.settings_window",
            "gui_qt.subtitle_window",
            "gui_qt.update_banner",
        )
        code = (
            "import sys, importlib\n"
            f"for name in {modules!r}: importlib.import_module(name)\n"
            "print(sorted(m for m in sys.modules "
            "if m.split('.')[0] in ('tkinter', 'customtkinter', '_tkinter')))"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=pathlib.Path(__file__).resolve().parents[1],
        )
        assert result.stdout.strip() == "[]", result.stdout + result.stderr


class TestNoTextShapingLayer:
    """The migration's core claim: Qt shapes Arabic, we never pre-process it."""

    def test_gui_qt_does_not_use_arabic_reshaper_or_bidi(self):
        import pathlib

        root = pathlib.Path(__file__).resolve().parent.parent / "gui_qt"
        offenders = [
            p.name
            for p in root.glob("*.py")
            if "arabic_reshaper" in p.read_text(encoding="utf-8")
            or "from bidi" in p.read_text(encoding="utf-8")
        ]
        assert offenders == [], f"Qt tree must not pre-shape Arabic: {offenders}"

    def test_arabic_is_stored_and_measured_as_logical_text(self, overlay):
        # The source line must reach the widget unchanged: no reshaping, no
        # reordering, no platform branch.
        w = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=True)
        arabic = "هل من خالق غير الله؟"
        w.add_subtitle("Gibt es einen Schoepfer?", source_text=arabic)
        assert w._blocks[0].source == arabic


class TestSegmentedControl:
    """The Tk panel uses CTkSegmentedButton for every either/or choice."""

    def test_exclusive_selection_and_signal(self, qt_app):
        from gui_qt.widgets import SegmentedControl

        seg = SegmentedControl(["Nie", "Wenn gestoppt", "Immer"], current=0)
        picked: list[int] = []
        seg.changed.connect(picked.append)
        assert seg.current_index() == 0

        seg._buttons[2].click()
        assert picked == [2]
        assert seg.current_index() == 2
        # Exclusive: selecting one must clear the others.
        assert [b.isChecked() for b in seg._buttons] == [False, False, True]

    def test_corner_rounding_property_marks_the_ends(self, qt_app):
        from gui_qt.widgets import SegmentedControl

        seg = SegmentedControl(["a", "b", "c"])
        assert [b.property("seg") for b in seg._buttons] == ["first", "middle", "last"]
        # Keep a reference: an unreferenced widget is collected and its C++
        # object deleted before the assertion runs.
        single = SegmentedControl(["only"])
        assert single._buttons[0].property("seg") == "only"

    def test_programmatic_set_does_not_emit(self, qt_app):
        # set_current_index is used to sync state; emitting would risk a loop
        # with handlers that write back.
        from gui_qt.widgets import SegmentedControl

        seg = SegmentedControl(["a", "b"])
        fired: list[int] = []
        seg.changed.connect(fired.append)
        seg.set_current_index(1)
        assert seg.current_index() == 1
        assert fired == []


class TestSubtitleModeLabels:
    def test_translated_label_never_leaks_into_settings(self, qt_app, monkeypatch):
        # Regression guard: the combo shows "Echtzeit" but Settings must
        # receive "realtime".
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)

        class FakeController:
            pass

        panel = cp.ControlPanel(FakeController())
        try:
            # Realtime is offered only under the real-time strategy, so put the
            # panel there before asserting the full list.
            panel.settings.pipeline_mode = PIPELINE_MODE_STREAMING
            panel._refresh_mode_combo()
            labels = [
                panel.mode_combo.itemText(i) for i in range(panel.mode_combo.count())
            ]
            values = [
                panel.mode_combo.itemData(i) for i in range(panel.mode_combo.count())
            ]
            assert values == list(SUBTITLE_MODES)
            for i, mode in enumerate(SUBTITLE_MODES):
                panel.mode_combo.setCurrentIndex(i)
                panel._persist()
                assert panel.settings.subtitle_mode == mode
            # Labels only differ from raw values when a translation exists;
            # at minimum they must round-trip by data, not text.
            assert len(labels) == len(SUBTITLE_MODES)
        finally:
            panel.close()


class TestDeviceHotSwap:
    """Changing the device mid-session must swap it, not silently do nothing."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        # Keyring access is machine-dependent; keep the test hermetic.
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)

        class FakeController:
            def __init__(self):
                self.swaps: list[int] = []
                self.restarts: list[int] = []
                self.refuse = False
                # _finish_start hands these to the pipeline bridge, which
                # drains them on worker threads. Real queues, never fed.
                self.translation_queue = queue.Queue()
                self.error_queue = queue.Queue()
                self.interim_queue = queue.Queue()

            def change_input_device(self, idx):
                self.swaps.append(idx)
                return not self.refuse

            def restart(self, input_device=None):
                self.restarts.append(input_device)

        controller = FakeController()
        p = cp.ControlPanel(controller)
        yield p, controller
        p.close()

    def test_restoring_the_saved_device_is_not_a_user_change(self, panel):
        p, controller = panel
        assert controller.swaps == []
        assert controller.restarts == []

    @staticmethod
    def _other_index(p) -> int:
        """An index that differs from the current one.

        The saved device is restored at construction, so a hardcoded index can
        already be selected — setCurrentIndex would then be a no-op and emit
        nothing, which looks like the feature is broken.
        """
        return 0 if p.device_combo.currentIndex() != 0 else 1

    def test_no_swap_while_stopped(self, panel):
        p, controller = panel
        if p.device_combo.count() < 2:
            pytest.skip("needs at least two input devices")
        p.device_combo.setCurrentIndex(self._other_index(p))
        assert controller.swaps == []

    def test_hot_swaps_while_running(self, panel):
        p, controller = panel
        if p.device_combo.count() < 2:
            pytest.skip("needs at least two input devices")
        p._running = True
        target = self._other_index(p)
        p.device_combo.setCurrentIndex(target)
        assert controller.swaps == [p.device_indices[target]]
        assert controller.restarts == []

    def test_refused_swap_falls_back_to_restart(self, panel):
        p, controller = panel
        if p.device_combo.count() < 2:
            pytest.skip("needs at least two input devices")
        p._running = True
        controller.refuse = True
        target = self._other_index(p)
        p.device_combo.setCurrentIndex(target)
        assert controller.restarts == [p.device_indices[target]]

    def test_a_device_picked_while_connecting_is_not_lost(self, panel):
        """The reported bug: pick another device during "Connecting…" and no
        sound arrives until you switch away and back.

        Start captures its device before spawning the worker, and this handler
        used to return early because _running is still False — so the pipeline
        connected on the old device while the panel showed the new one."""
        p, controller = panel
        if p.device_combo.count() < 2:
            pytest.skip("needs at least two input devices")
        p._starting = True
        p._started_device = p.device_indices[p.device_combo.currentIndex()]
        target = self._other_index(p)
        p.device_combo.setCurrentIndex(target)
        # Not applied yet: swapping under a connect races the start thread.
        assert controller.swaps == []
        assert p._pending_device == p.device_indices[target]

        p._start_error = None
        p._finish_start()
        assert p._running
        assert controller.swaps == [p.device_indices[target]]
        assert p._pending_device is None

    def test_the_same_device_picked_while_connecting_is_not_reswapped(self, panel):
        p, controller = panel
        p._starting = True
        current = p.device_indices[p.device_combo.currentIndex()]
        p._started_device = current
        p._pending_device = current
        p._start_error = None
        p._finish_start()
        assert controller.swaps == []  # it is already the running device

    def test_a_failed_start_drops_the_parked_device(self, panel, monkeypatch):
        import gui_qt.control_panel as cp

        # _finish_start reports a failed start with a REAL modal dialog, which
        # a test run must never put on the developer's desktop.
        shown: list[str] = []
        monkeypatch.setattr(
            cp, "show_message", lambda *a, **k: shown.append(a[2] if len(a) > 2 else "")
        )
        p, controller = panel
        p._starting = True
        p._started_device = 0
        p._pending_device = 1
        p._start_error = RuntimeError("no key")
        p._finish_start()
        assert shown  # the operator is told, rather than left at "Connecting…"
        # Left set, it would fire against whatever the NEXT session started on.
        assert p._pending_device is None
        assert controller.swaps == []


class TestControlPanelLayout:
    """The panel-parity fixes: card grid, log panel, chrome, free resizing."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        # The default hide policy opens the overlay even while stopped; a real
        # frameless always-on-top window in a test run is neither needed here
        # nor welcome.
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        # The stored log state decides the column count now (the log shares the
        # window instead of widening it), so pin it rather than inheriting
        # whatever this machine's settings.json happens to say.
        if not p._log_collapsed:
            p._toggle_log_panel()
        p.resize(1200, 800)
        qt_app.processEvents()
        yield p
        p.close()

    def test_card_grid_reflows_with_the_window(self, panel, qt_app):
        # 1 / 2 / 3 columns, at the same thresholds the Tk panel uses.
        # A hidden widget never receives resizeEvent, so the reflow is driven
        # directly — that is exactly what the handler does, and showing a real
        # control-panel window during a test run is not worth the parity.
        for width, expected in ((1200, 3), (900, 2), (520, 1)):
            panel.resize(width, 800)
            panel._relayout_columns()
            assert panel._columns == expected, f"{width}px should give {expected}"

    def test_a_column_is_never_narrower_than_its_cards_need(self, panel):
        # The horizontal scrollbar is off, so a threshold that lets a column
        # below its minimum clips the card instead of scrolling it.
        needs = [c.minimumSizeHint().width() for c in (panel.col_a, panel.col_b)]
        margins = panel.grid.contentsMargins()
        chrome = margins.left() + margins.right() + panel.grid.horizontalSpacing()
        assert cp_module()._COL2_MIN_W >= sum(needs) + chrome

    def test_opening_the_log_gives_the_cards_one_column(self, panel):
        assert panel._log_collapsed
        panel.resize(1200, 800)
        panel._relayout_columns()
        assert panel._columns == 3
        panel._toggle_log_panel()
        assert panel._columns == 1

    def test_the_log_opens_inside_a_window_that_can_hold_it(self, panel, qt_app):
        # It shares the window rather than bolting 420px onto the side; only a
        # window too narrow for both is widened.
        panel.resize(1400, 800)
        qt_app.processEvents()
        panel._toggle_log_panel()
        assert panel.width() == 1400
        assert panel.sidebar.width() == cp_module()._SIDEBAR_W_WITH_LOG

    def test_a_narrow_window_grows_to_fit_the_log(self, panel, qt_app):
        panel.resize(600, 800)
        qt_app.processEvents()
        panel._toggle_log_panel()
        assert panel.width() >= (
            cp_module()._SIDEBAR_W_WITH_LOG + cp_module()._LOG_PANEL_MIN_W
        )

    def test_advanced_opens_only_when_it_has_a_column_to_itself(self, panel):
        panel.resize(1200, 800)
        panel._relayout_columns()
        assert panel.advanced_card.is_expanded()
        panel.resize(900, 800)
        panel._relayout_columns()
        assert not panel.advanced_card.is_expanded()

    def test_each_card_group_is_placed_exactly_once(self, panel):
        panel.resize(1200, 800)
        panel._relayout_columns()
        placed = {
            panel.grid.itemAt(i).widget() for i in range(panel.grid.count())
        }
        assert placed == {panel.col_a, panel.col_b, panel.col_c}

    def test_window_can_shrink_below_the_cards_natural_width(self, panel, qt_app):
        # Combos otherwise demand their longest entry and pin the window open.
        panel.resize(430, 460)
        qt_app.processEvents()
        assert panel.width() == 430

    def test_log_panel_toggles_and_persists(self, panel):
        start = panel._log_collapsed
        panel._toggle_log_panel()
        assert panel._log_collapsed is not start
        assert panel.log_panel.isVisible() is not start
        assert panel.settings.log_panel_collapsed is panel._log_collapsed

    def test_log_lines_reach_the_panel(self, panel):
        from utils.logging import log_queue

        log_queue.put("PROBE LINE")
        panel._drain_logs()
        assert "PROBE LINE" in panel.log_text.toPlainText()

    def test_window_and_header_carry_the_logo(self, panel):
        assert not panel.windowIcon().isNull()
        pixmap = panel.logo_label.pixmap()
        assert pixmap is not None and not pixmap.isNull()

    def test_action_labels_do_not_double_their_glyph(self, panel):
        assert panel.start_btn.text().count("\u25b6") == 1

    def test_input_meter_reads_the_controller(self, panel):
        class Snapshot:
            rms_dbfs = -12.0
            clipping_ratio = 0.0

        panel.controller.get_input_level = lambda: Snapshot()
        panel._poll_input_level()
        assert panel.level_bar.value() > 0.5
        assert "dBFS" in panel.level_value.text()


class TestSourceLanguageChoices:
    """Real-time transcription cannot auto-detect, so "Automatic" must go."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    @staticmethod
    def _entries(panel) -> list[str]:
        from utils.settings import language_canonical_name

        return [
            language_canonical_name(panel.source_combo.itemText(i))
            for i in range(panel.source_combo.count())
        ]

    def test_streaming_hides_automatic(self, panel):
        panel.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        panel._refresh_source_combo()
        assert "Automatic" not in self._entries(panel)

    def test_segmented_offers_automatic(self, panel):
        from utils.settings import PIPELINE_MODE_SEGMENTED

        panel.settings.pipeline_mode = PIPELINE_MODE_SEGMENTED
        panel._refresh_source_combo()
        assert "Automatic" in self._entries(panel)

    def test_a_stored_automatic_is_replaced_when_streaming(self, panel):
        panel.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        panel.settings.source_language = "Automatic"
        panel._refresh_source_combo()
        assert panel.settings.source_language != "Automatic"


class TestSubtitleHideMode:
    """The 3-way policy has to actually open and close the overlay."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)

        class FakeOverlay:
            def __init__(self):
                self.text = None
                self.destroyed = False

            def set_stopped_hint(self, visible):
                pass

            def set_always_on_top(self, enabled):
                pass

            def set_live_text(self, text, settled=False):
                pass

            def set_announcement(self, text):
                self.text = text

            def clear_announcement(self):
                self.text = None

            def destroy(self):
                self.destroyed = True

        def fake_ensure(self):
            if self.subtitle_window is None:
                self.subtitle_window = FakeOverlay()

        monkeypatch.setattr(cp.ControlPanel, "_ensure_subtitle_window", fake_ensure)

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    def test_never_keeps_the_overlay_open_while_stopped(self, panel):
        panel.settings.subtitle_hide_mode = "never"
        panel._apply_subtitle_hide_mode()
        assert panel.subtitle_window is not None

    def test_always_closes_it(self, panel):
        panel.settings.subtitle_hide_mode = "always"
        panel._apply_subtitle_hide_mode()
        assert panel.subtitle_window is None

    def test_stopped_closes_it_only_while_stopped(self, panel):
        panel.settings.subtitle_hide_mode = "stopped"
        panel._running = False
        panel._apply_subtitle_hide_mode()
        assert panel.subtitle_window is None
        panel._running = True
        panel._apply_subtitle_hide_mode()
        assert panel.subtitle_window is not None

    def test_an_announcement_can_be_shown_while_stopped(self, panel):
        # "The talk starts in 10 minutes" is exactly the message you want
        # BEFORE starting, so the overlay is created on demand.
        panel.settings.subtitle_hide_mode = "always"
        panel._apply_subtitle_hide_mode()
        panel.show_announcement("Beginnt in 10 Minuten")
        assert panel.subtitle_window is not None
        assert panel.subtitle_window.text == "Beginnt in 10 Minuten"

    def test_clearing_it_closes_the_overlay_again(self, panel):
        panel.settings.subtitle_hide_mode = "always"
        panel._apply_subtitle_hide_mode()
        panel.show_announcement("Kurz")
        panel.clear_announcement()
        assert panel.subtitle_window is None


class TestAnnouncementSurvivesARebuiltOverlay:
    """An "until stopped" message must outlive the window it is drawn on.

    Its own fixture: this one patches the overlay CLASS rather than
    _ensure_subtitle_window, because the re-assertion under test lives inside
    that method.
    """

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)

        class FakeOverlay:
            def __init__(self, **_kwargs):
                self.text = None

            def set_announcement(self, text):
                self.text = text

            def clear_announcement(self):
                self.text = None

            def set_always_on_top(self, enabled):
                pass

            def set_stopped_hint(self, visible):
                pass

            def show(self):
                pass

            def destroy(self):
                pass

        monkeypatch.setattr(cp, "SubtitleWindow", FakeOverlay)

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    def test_a_rebuilt_overlay_gets_the_message_back(self, panel):
        panel.settings.subtitle_hide_mode = "never"
        panel._apply_subtitle_hide_mode()
        panel.show_announcement("Bitte Handys stummschalten")
        first = panel.subtitle_window
        panel._teardown_subtitle_window()  # a monitor or height rebuild
        panel._ensure_subtitle_window()
        assert panel.subtitle_window is not first
        assert panel.subtitle_window.text == "Bitte Handys stummschalten"

    def test_a_cleared_announcement_does_not_come_back(self, panel):
        panel.settings.subtitle_hide_mode = "never"
        panel._apply_subtitle_hide_mode()
        panel.show_announcement("Kurz")
        panel.clear_announcement()
        panel._teardown_subtitle_window()
        panel._ensure_subtitle_window()
        assert panel.subtitle_window.text is None


class TestSessionStartFeedback:
    """A Start can take tens of seconds; the panel has to say so."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(cp, "ensure_keys", lambda *a, **k: True)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            def __init__(self):
                self.released = threading.Event()
                self.translation_queue = queue.Queue()

            def start(self, **_kwargs):
                self.released.wait(5)

            def stop(self):
                pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.controller.released.set()
        p.close()

    def test_start_shows_connecting_and_disables_both_buttons(self, panel, qt_app):
        panel.on_start()
        qt_app.processEvents()
        assert panel._starting is True
        assert panel.status_pill.objectName() == "pill_connecting"
        assert panel.status_pill.text() == panel._clean_label("connecting", "x")
        # Start would queue a second session; there is nothing to stop yet.
        assert not panel.start_btn.isEnabled()
        assert not panel.stop_btn.isEnabled()

    def test_the_pipeline_starts_off_the_gui_thread(self, panel, qt_app):
        # If controller.start() ran inline the window would be frozen for its
        # whole duration — on_start must return while it is still blocked.
        panel.on_start()
        assert not panel.controller.released.is_set()
        assert panel._starting is True

    def test_a_failed_start_falls_back_to_stopped(self, panel, qt_app, monkeypatch):
        panel.controller.start = lambda **_k: 1 / 0
        monkeypatch.setattr(cp_module(), "show_message", lambda *a, **k: None)
        panel.on_start()
        for _ in range(40):
            qt_app.processEvents()
            if not panel._starting:
                break
            threading.Event().wait(0.05)
        assert panel._starting is False
        assert panel._running is False
        assert panel.status_pill.objectName() == "pill_stopped"


class TestAdvancedCard:
    """The Advanced card's collapse and its "Default" lock."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(cp, "ensure_keys", lambda *a, **k: True)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    def test_default_locks_the_provider_not_just_the_model(self, panel):
        # A ticked "Default" that still let the engine be changed left the box
        # ticked above a non-default provider.
        panel.use_default_transcription.setChecked(True)
        panel.use_default_translation.setChecked(True)
        assert not panel.transcription_provider_combo.isEnabled()
        assert not panel.transcription_model_combo.isEnabled()
        assert not panel.provider_combo.isEnabled()
        assert not panel.model_combo.isEnabled()

    def test_unticking_default_hands_the_provider_back(self, panel):
        panel.use_default_transcription.setChecked(False)
        assert panel.transcription_provider_combo.isEnabled()
        panel.use_default_translation.setChecked(False)
        assert panel.provider_combo.isEnabled()

    def test_the_card_collapses_while_it_shares_a_column(self, panel):
        panel.resize(900, 800)
        panel._relayout_columns()
        assert panel._columns == 2
        panel.advanced_card.set_expanded(False)
        # isHidden(), not isVisible(): the panel is never shown in these tests,
        # so every descendant is invisible and isVisible() proves nothing.
        assert panel.advanced_card.content.isHidden()
        assert panel.advanced_card.arrow_label.text() == "▾"
        panel.advanced_card.set_expanded(True)
        assert not panel.advanced_card.content.isHidden()
        assert panel.advanced_card.arrow_label.text() == "▴"

    def test_in_three_columns_the_card_is_pinned_open(self, panel):
        # Its column holds nothing else, so collapsing it empties the column.
        panel.resize(1200, 800)
        panel._relayout_columns()
        assert panel._columns == 3
        assert not panel.advanced_card.is_collapsible()
        assert panel.advanced_card.arrow_label.isHidden()
        panel.advanced_card.set_expanded(False)  # a header click
        assert panel.advanced_card.is_expanded()

    def test_the_collapse_moves_to_other_settings_in_three_columns(self, panel):
        panel.resize(1200, 800)
        panel._relayout_columns()
        # Closed to start with: it is the longest, least-touched group, and
        # closing it is what shortens a pinned-open card.
        assert panel.other_settings.is_collapsible()
        assert not panel.other_settings.is_expanded()
        assert panel.other_settings.panel.isHidden()
        panel.other_settings.set_expanded(True)
        assert not panel.other_settings.panel.isHidden()

    def test_other_settings_is_a_plain_section_below_three_columns(self, panel):
        for width in (900, 520):
            panel.resize(width, 800)
            panel._relayout_columns()
            assert not panel.other_settings.is_collapsible()
            assert panel.other_settings.is_expanded()
            assert panel.other_settings.button.isHidden()
            assert not panel.other_settings.heading.isHidden()

    def test_window_on_top_sits_under_its_heading_above_the_checkboxes(self, panel):
        body = panel.other_settings.body
        order = [
            body.itemAt(i).widget().text()
            for i in range(body.count())
            if body.itemAt(i).widget() is not None
            and hasattr(body.itemAt(i).widget(), "text")
        ]
        assert panel.other_settings.heading.text() == panel._t(
            "other_settings", "Other settings"
        )
        aot = order.index(panel._t("window_on_top_label", "Window always on top"))
        first_check = order.index(panel._other_checks["show_footer"].text())
        assert aot < first_check


class TestStartStopFocus:
    """Starting a session must not light up an unrelated control."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        p.show()
        qt_app.processEvents()
        yield p
        p.close()

    def test_start_does_not_hand_the_focus_ring_to_the_monitor_combo(
        self, panel, qt_app
    ):
        # Disabling the button that was just clicked moves focus to the NEXT
        # widget in the tab chain, and the subtitle-screen dropdown then wore
        # the accent ring as if the operator had selected it.
        # QWidget.hasFocus() additionally requires an ACTIVE window, which a
        # test window is not; the application's focus widget is the state this
        # is actually about.
        from PySide6.QtCore import Qt

        panel.start_btn.setFocus(Qt.MouseFocusReason)
        qt_app.processEvents()
        assert QApplication.focusWidget() is panel.start_btn

        panel._starting = True
        panel._sync_running_state()
        qt_app.processEvents()
        assert QApplication.focusWidget() is not panel.monitor_combo

        panel._starting, panel._running = False, True
        panel._sync_running_state()
        qt_app.processEvents()
        assert QApplication.focusWidget() is not panel.monitor_combo

    def test_focus_moves_to_the_button_that_is_now_live(self, panel, qt_app):
        # Stop is the only action left once a session is up, so the ring goes
        # there rather than nowhere when it can.
        from PySide6.QtCore import Qt

        panel._running = True
        panel._sync_running_state()
        panel.start_btn.setEnabled(True)
        panel.start_btn.setFocus(Qt.MouseFocusReason)
        qt_app.processEvents()
        panel._sync_running_state()
        qt_app.processEvents()
        assert QApplication.focusWidget() is panel.stop_btn


class TestSlidersIgnoreTheWheel:
    """Both sliders drive the audience overlay live, so a stray wheel while
    scrolling the panel would resize or fade what the room is looking at."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    def test_the_wheel_changes_neither_slider(self, panel, qt_app):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtGui import QWheelEvent

        for slider in (panel.height_slider, panel.opacity_slider):
            before = slider.value()
            event = QWheelEvent(
                QPoint(10, 10),
                slider.mapToGlobal(QPoint(10, 10)),
                QPoint(0, 120),
                QPoint(0, 120),
                Qt.NoButton,
                Qt.NoModifier,
                Qt.NoScrollPhase,
                False,
            )
            qt_app.sendEvent(slider, event)
            assert slider.value() == before
            # Ignored rather than swallowed, so the page behind scrolls.
            assert not event.isAccepted()

    def test_dragging_still_works(self, panel, qt_app):
        # Only the wheel is refused; the slider is not read-only.
        panel.height_slider.setValue(42)
        assert panel.height_slider.value() == 42


class TestLevelMeterZones:
    """The zone map must tile, not overlap.

    Asserted on the geometry, not on rendered pixels: the seam was one pixel
    wide, which is also the width of a legitimate fractional device-pixel edge
    on a scaled display — a pixel probe cannot tell the two apart (tried).
    """

    def test_consecutive_zones_share_an_edge_exactly(self):
        from gui_qt.widgets import AudioLevelBar as Bar

        bounds = (0.0, Bar.GREEN_END, Bar.RED_START, 1.0)
        # Odd widths included: they are where rounding used to disagree.
        for width in (37, 60, 101, 200, 275, 512):
            spans = [
                Bar.band_span(0, width, a, b)
                for a, b in zip(bounds, bounds[1:], strict=False)
            ]
            for (_, end), (start, _) in zip(spans, spans[1:], strict=False):
                assert end == start, f"width {width}: {end} != {start}"
            assert spans[0][0] == 0 and spans[-1][1] == width

    def test_a_zone_never_reaches_into_the_next(self):
        from gui_qt.widgets import AudioLevelBar as Bar

        # The translucent zone map is drawn amber-then-red; one pixel of
        # overlap composited twice and showed as a dark seam.
        amber = Bar.band_span(0, 275, Bar.GREEN_END, Bar.RED_START)
        red = Bar.band_span(0, 275, Bar.RED_START, 1.0)
        assert amber[1] <= red[0]


class TestNoStrayTopLevelWindows:
    """Little boxes flashed across the screen before the panel opened."""

    def test_a_card_never_shows_a_parentless_widget(self, qt_app):
        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Card

        apply_theme(qt_app, "dark")
        before = {w for w in qt_app.topLevelWidgets() if w.isVisible()}
        # A parentless widget that is shown IS a top-level window. Every
        # setVisible in a constructor therefore has to come after the widget
        # has been put into a layout.
        card = Card("⚙", "Erweitert", collapsible=True, expanded=False)
        after = {w for w in qt_app.topLevelWidgets() if w.isVisible()}
        assert after - before == set()
        assert card.symbol_label.parentWidget() is not None
        assert card.arrow_label.parentWidget() is not None
        card.deleteLater()


class TestCardPadding:
    """A collapsed card must not read as lopsided."""

    def test_a_collapsed_card_is_padded_evenly(self, qt_app):
        from PySide6.QtWidgets import QLabel

        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Card

        apply_theme(qt_app, "dark")
        # Standalone, not the panel's card: a child of a live layout is resized
        # by its parent the moment it is shown, so measuring one there measures
        # the layout, not the padding rule.
        card = Card("⚙", "Erweitert", collapsible=True, expanded=False)
        card.body.addWidget(QLabel("body"))
        card.add_stretch()
        card.resize(420, card.sizeHint().height())
        card.show()
        qt_app.processEvents()
        badge = card.symbol_label
        top = badge.mapTo(card, badge.rect().topLeft()).y()
        bottom = card.height() - (top + badge.height())
        assert top == bottom, f"collapsed card padded {top} above, {bottom} below"
        card.hide()
        card.deleteLater()


class TestEqualColumnHeights:
    """Side by side the three cards end level; stacked they keep their own."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        if not p._log_collapsed:
            p._toggle_log_panel()
        p.resize(1400, 980)
        p.show()
        qt_app.processEvents()
        yield p
        p.close()

    def test_three_columns_end_level(self, panel, qt_app):
        panel.resize(1400, 980)
        qt_app.processEvents()
        assert panel._columns == 3
        bottoms = {
            card.geometry().y() + card.geometry().height()
            for _box, card in panel._column_tails
        }
        assert len(bottoms) == 1, f"columns end at {sorted(bottoms)}"

    def test_the_cards_stop_at_the_tallest_one_not_at_the_window(
        self, panel, qt_app
    ):
        # The scroll area makes cards_host viewport-tall; without a stretch row
        # under the content the cards swallowed that surplus and ran to the
        # bottom of the window.
        panel.resize(1400, 1200)
        qt_app.processEvents()
        host = panel.cards_host
        bottom = max(
            card.mapTo(host, card.rect().bottomLeft()).y()
            for _box, card in panel._column_tails
        )
        assert bottom < host.height() - 100, (
            f"cards reach {bottom} of {host.height()} — they filled the window"
        )

    def test_one_column_keeps_natural_heights(self, panel, qt_app):
        panel.resize(600, 980)
        qt_app.processEvents()
        assert panel._columns == 1
        for _box, card in panel._column_tails:
            assert card.height() == card.sizeHint().height()

    def _top(self, card):
        host = card.window().cards_host
        return card.mapTo(host, card.rect().topLeft()).y()

    def test_two_columns_stack_the_right_column_tightly(self, panel, qt_app):
        # Levelling the bottoms here means padding Advanced away from the card
        # above it — a gap that grows every time column A does.
        panel.resize(900, 900)
        qt_app.processEvents()
        assert panel._columns == 2
        language = panel._column_tails[1][1]
        host = panel.cards_host
        gap = self._top(panel.advanced_card) - (
            language.mapTo(host, language.rect().bottomLeft()).y()
        )
        spacing = panel.grid.verticalSpacing()
        # The row spacing, give or take the pixel the grid's rounding leaves in
        # the row above. Bottom-aligning instead put a hundred here.
        assert spacing <= gap <= spacing + 2, f"{gap}px between the cards"

    def _bottom(self, card):
        host = card.window().cards_host
        return card.mapTo(host, card.rect().bottomLeft()).y()

    def test_two_columns_end_on_one_line(self, panel, qt_app):
        # A few pixels apart reads as a mistake, so the shorter column's last
        # card takes the difference.
        for size in ((900, 900), (900, 700), (900, 1400)):
            panel.resize(*size)
            _settle(qt_app)
            assert panel._columns == 2, size
            display = panel._column_tails[0][1]
            assert self._bottom(display) == self._bottom(panel.advanced_card), size

    def test_an_opened_appearance_section_is_not_absorbed(self, panel, qt_app):
        # Levelling THAT much would inflate a collapsed header into an empty
        # box; the columns simply end where they end instead.
        panel.resize(900, 900)
        _settle(qt_app)
        panel.typography.set_expanded(True)
        _settle(qt_app)
        advanced = panel.advanced_card
        assert advanced.height() == advanced.sizeHint().height()
        panel.typography.set_expanded(False)
        _settle(qt_app)
        # ...and levelling comes back once the section is closed again.
        assert self._bottom(panel._column_tails[0][1]) == self._bottom(advanced)

    def test_a_collapsed_advanced_is_padded_above_not_inflated(
        self, panel, qt_app
    ):
        # It was the shorter column's last card, so it took the levelling slack
        # into its own height — a header strip stretched into a tall empty box,
        # which is the one thing collapsing it is for. The spacer above it takes
        # the slack now, so the strip keeps its height and the bottoms still
        # line up (the rule three columns already used).
        panel.resize(900, 900)
        _settle(qt_app)
        assert panel._columns == 2
        advanced = panel.advanced_card
        advanced.set_expanded(False)
        panel._level_two_column_bottoms()
        _settle(qt_app)
        assert not advanced.is_expanded()
        assert advanced.height() == advanced.sizeHint().height(), "inflated"
        assert self._bottom(panel._column_tails[0][1]) == self._bottom(advanced)

    def test_opening_the_appearance_expander_does_not_slide_advanced_away(
        self, panel, qt_app
    ):
        # It lives in the left column, which spans both rows, and its extra
        # height has to land in the row BELOW Advanced rather than push
        # Advanced down the window.
        #
        # Advanced may still move by the levelling slack, which is capped: once
        # a collapsed card is padded from above rather than inflated, keeping
        # the two columns on one line IS a change of its top edge. What must
        # never happen is it travelling by the section's own height.
        panel.resize(900, 900)
        _settle(qt_app)
        assert panel._columns == 2
        before = self._top(panel.advanced_card)
        panel.typography.set_expanded(True)
        _settle(qt_app)
        drift = abs(self._top(panel.advanced_card) - before)
        assert drift <= cp_module()._LEVEL_FILL_MAX_PX, f"slid {drift}px"
        panel.typography.set_expanded(False)
        _settle(qt_app)
        assert self._top(panel.advanced_card) == before

    def test_always_on_top_covers_the_control_panel(self, panel, qt_app):
        from gui_qt.widgets import is_window_on_top
        from utils.settings import ALWAYS_ON_TOP_MODES

        panel.show()
        qt_app.processEvents()
        native = int(panel.winId())
        for mode in ("always", "never"):
            panel._on_aot_changed(ALWAYS_ON_TOP_MODES.index(mode))
            qt_app.processEvents()
            expected = mode == "always"
            assert is_window_on_top(panel) is expected
            assert panel.isVisible(), f"panel hidden after mode {mode}"
            # Recreating the native window is what made the panel flash white.
            assert int(panel.winId()) == native, f"recreated for mode {mode}"


class TestControlRowHeights:
    """Everything sharing a row with a dropdown is as tall as the dropdown."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        from gui_qt.theme import apply_theme

        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )
        # The dropdown's height comes from the stylesheet, so an unthemed panel
        # would measure Qt's default metrics rather than the app's.
        apply_theme(qt_app, "dark")

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        if not p._log_collapsed:
            p._toggle_log_panel()
        p.resize(940, 900)
        p.typography.set_expanded(True)  # the colour buttons live in here
        p.show()
        _settle(qt_app)
        yield p
        p.close()

    def test_the_small_buttons_match_the_dropdowns(self, panel):
        from PySide6.QtWidgets import QPushButton

        from gui_qt.widgets import CONTROL_H

        assert panel.mode_combo.height() == CONTROL_H
        widgets = [
            panel.font_stepper.minus,
            panel.font_stepper.plus,
            panel._color_pick_btns["translation_text_color"],
            panel._color_reset_btns["translation_text_color"],
            panel.hide_segment,  # a full-width row control, same rhythm
            *[b for b in panel.findChildren(QPushButton) if b.text() in ("?", "⇄")],
        ]
        assert widgets  # the "?" buttons must actually have been found
        for widget in widgets:
            assert widget.height() == CONTROL_H, widget.text()

    def test_the_swap_button_lines_up_with_the_language_dropdowns(self, panel):
        from PySide6.QtWidgets import QPushButton

        swap = next(b for b in panel.findChildren(QPushButton) if b.text() == "⇄")
        host = panel.cards_host

        def top(widget):
            return widget.mapTo(host, widget.rect().topLeft()).y()

        # It used to be pushed down by a hand-measured spacer, which left it
        # sitting a few pixels above the dropdowns it belongs to.
        assert top(swap) == top(panel.source_combo) == top(panel.target_combo)


class TestWindowGeometryMemory:
    """The panel reopens at the size, place and state it was closed at."""

    @pytest.fixture
    def make_panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        made = []

        def _make():
            p = cp.ControlPanel(FakeController())
            made.append(p)
            return p

        yield _make
        for p in made:
            p.close()

    def test_a_closed_size_and_place_come_back(self, make_panel, qt_app):
        first = make_panel()
        first.show()
        first.setGeometry(220, 140, 1010, 700)
        qt_app.processEvents()
        first.close()
        assert first.settings.window_geometry == "1010x700+220+140"

        second = make_panel()
        assert (second.width(), second.height()) == (1010, 700)
        assert (second.x(), second.y()) == (220, 140)

    def test_a_maximized_panel_comes_back_maximized(self, make_panel, qt_app):
        from PySide6.QtCore import Qt

        first = make_panel()
        first.show()
        first.setGeometry(220, 140, 1010, 700)
        qt_app.processEvents()
        first.showMaximized()
        qt_app.processEvents()
        first.close()
        assert first.settings.window_maximized
        # The maximized box is not stored: it would reopen a screen-sized
        # *normal* window with nothing to restore down to.
        assert first.settings.window_geometry == "1010x700+220+140"

        second = make_panel()
        assert second.windowState() & Qt.WindowMaximized

    def test_a_geometry_off_every_screen_is_dropped(self, make_panel):
        panel = make_panel()
        panel.settings.window_geometry = "900x600+-9000+-9000"
        assert not panel._restore_window_geometry()

    def test_a_geometry_below_the_minimum_is_dropped(self, make_panel):
        panel = make_panel()
        panel.settings.window_geometry = "100x80+100+100"
        assert not panel._restore_window_geometry()

    def test_garbage_is_dropped(self, make_panel):
        panel = make_panel()
        for value in ("", "zoomed", "900x600", "900x600+10"):
            panel.settings.window_geometry = value
            assert not panel._restore_window_geometry(), value


class TestSubtitleAppearance:
    """The collapsible typography controls ported from gui/typography.py."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    def test_it_starts_collapsed(self, panel):
        assert not panel.typography.is_expanded()
        assert not panel.typography.panel.isVisible()

    def test_the_size_is_shown_against_the_translation_size(self, panel):
        panel.settings.font_size_base = 40
        panel.settings.source_font_size_base = 40 / 0.7
        assert panel._source_font_percent_text() == "70%"

    def test_stepping_the_size_keeps_it_in_range(self, panel):
        from utils.settings import SOURCE_FONT_SIZE_BASE_MAX

        for _ in range(40):
            panel._step_source_font(+5.0)
        assert panel.settings.source_font_size_base == SOURCE_FONT_SIZE_BASE_MAX

    def test_the_main_font_step_drags_the_original_size_with_it(self, panel):
        panel.settings.font_size_base = 40
        panel.settings.source_font_size_base = 40 / 0.7
        before = panel._source_font_percent_text()
        panel._step_font(smaller=True)
        assert panel._source_font_percent_text() == before

    def test_reset_is_offered_only_for_an_overridden_colour(self, panel):
        panel.settings.translation_text_color = ""
        panel._refresh_typography()
        assert not panel._color_reset_btns["translation_text_color"].isEnabled()
        panel.settings.translation_text_color = "#FF0000"
        panel._refresh_typography()
        assert panel._color_reset_btns["translation_text_color"].isEnabled()
        assert "#FF0000" in panel._color_pick_btns["translation_text_color"].text()

    def test_backdrop_opacity_lives_on_the_panel(self, panel):
        panel.opacity_slider.setValue(30)
        assert panel.settings.subtitle_backdrop_opacity == 30
        assert panel.opacity_value.text() == "30%"


class TestControlChrome:
    """Check marks and dropdown chevrons are painted, not stylesheet shapes."""

    @staticmethod
    def _accent_pixels(image, limit: int = 34) -> int:
        count = 0
        for x in range(min(image.width(), limit)):
            for y in range(image.height()):
                c = image.pixelColor(x, y)
                if (
                    abs(c.red() - 0x15) < 30
                    and abs(c.green() - 0x80) < 30
                    and abs(c.blue() - 0x3D) < 30
                ):
                    count += 1
        return count

    def test_a_checked_box_draws_a_white_tick_on_accent(self, qt_app):
        from PySide6.QtWidgets import QCheckBox

        from gui_qt.theme import apply_theme

        apply_theme(qt_app, "light")
        box = QCheckBox("Originaltext anzeigen")
        box.setChecked(True)
        box.resize(240, 30)
        image = box.grab().toImage()
        white = sum(
            1
            for x in range(min(image.width(), 34))
            for y in range(image.height())
            if image.pixelColor(x, y).red() > 240
            and image.pixelColor(x, y).green() > 240
            and image.pixelColor(x, y).blue() > 240
        )
        assert self._accent_pixels(image) > 100, "indicator is not accent-filled"
        assert white > 15, "no check mark inside the indicator"

    def test_an_unchecked_box_is_not_filled(self, qt_app):
        from PySide6.QtWidgets import QCheckBox

        from gui_qt.theme import apply_theme

        apply_theme(qt_app, "light")
        box = QCheckBox("Aus")
        box.resize(240, 30)
        assert self._accent_pixels(box.grab().toImage()) < 20

    def test_the_dropdown_paints_a_chevron_not_a_block(self, qt_app):
        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch", "English"])
        combo.resize(240, 44)
        image = combo.grab().toImage()
        # Row widths across the arrow: a chevron narrows towards its point; the
        # CSS border-triangle this replaced rendered as a solid rectangle.
        rows: dict[int, int] = {}
        for x in range(image.width() - 34, image.width()):
            for y in range(image.height()):
                c = image.pixelColor(x, y)
                if (
                    c.red() < 190
                    and c.green() < 190
                    and c.blue() < 200
                    and c.alpha() > 200
                ):
                    rows[y] = rows.get(y, 0) + 1
        assert rows, "no arrow drawn at all"
        widths = [rows[y] for y in sorted(rows)]
        assert min(widths) < max(widths), f"arrow is a solid block: {widths}"

    @staticmethod
    def _fill(combo) -> str:
        """The dropdown's own fill colour, sampled clear of text and chevron."""
        image = combo.grab().toImage()
        # Fractions of the image, so the device pixel ratio cannot shift the
        # sample off the widget (it silently did during development).
        return image.pixelColor(
            int(image.width() * 0.68), image.height() // 2
        ).name()

    @staticmethod
    def _set_hover(widget, on: bool) -> None:
        # Qt derives :hover from WA_UnderMouse; setting it beats moving the
        # real cursor in a test.
        from PySide6.QtCore import Qt

        widget.setAttribute(Qt.WA_UnderMouse, on)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def test_a_hovered_dropdown_lights_up(self, qt_app):
        from gui_qt.theme import apply_theme, current_colors
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch", "English"])
        combo.resize(240, 44)
        assert self._fill(combo) == current_colors()["entry"]
        self._set_hover(combo, True)
        assert self._fill(combo) == current_colors()["panel_soft"]

    def test_a_disabled_dropdown_stays_inert(self, qt_app):
        from gui_qt.theme import apply_theme, current_colors
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch"])
        combo.setEnabled(False)
        combo.resize(240, 44)
        self._set_hover(combo, True)
        assert self._fill(combo) == current_colors()["entry"]

    def test_the_level_meter_shows_its_zones_while_silent(self, qt_app):
        # The operator needs to see how much headroom is left before amber and
        # red, not discover the boundaries by clipping.
        from gui_qt.theme import apply_theme, current_colors
        from gui_qt.widgets import AudioLevelBar

        apply_theme(qt_app, "light")
        bar = AudioLevelBar(height=14)
        bar.resize(300, 14)
        bar.set_value(0.0)
        image = bar.grab().toImage()
        dpr = image.devicePixelRatio()

        def at(fraction: float) -> str:
            return image.pixelColor(
                int(300 * fraction * dpr), int(7 * dpr)
            ).name()

        track = current_colors()["panel_soft"]
        # Green is deliberately NOT washed: doing so made a silent meter read
        # as an already 70%-full bar.
        assert at(0.20) == track
        assert at(0.78) != track, "amber zone is invisible until reached"
        assert at(0.95) != track, "red zone is invisible until reached"
        assert at(0.78) != at(0.95), "amber and red are indistinguishable"

    def test_scrolling_over_a_closed_combo_changes_nothing(self, qt_app):
        # Qt's default switches the selection on wheel, so scrolling the panel
        # past a dropdown silently changed the language, model or input device.
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QWheelEvent

        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown([f"Sprache {i}" for i in range(8)])
        combo.setCurrentIndex(3)
        for delta in (-120, 120):
            event = QWheelEvent(
                QPointF(10, 10),
                QPointF(10, 10),
                QPoint(0, 0),
                QPoint(0, delta),
                Qt.NoButton,
                Qt.NoModifier,
                Qt.NoScrollPhase,
                False,
            )
            qt_app.sendEvent(combo, event)
            assert combo.currentIndex() == 3
            # Ignored, so the scroll area behind receives it and the page moves.
            assert not event.isAccepted()

    def test_picking_an_entry_releases_the_focus_ring(self, qt_app):
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        window = QWidget()
        combo = Dropdown(["Deutsch", "English"])
        QVBoxLayout(window).addWidget(combo)
        window.show()
        try:
            combo.setFocus()
            # focusWidget(), not hasFocus(): the latter is False whenever the
            # window is not the ACTIVE one, which in a full-suite run depends
            # on whichever test showed a window last.
            assert window.focusWidget() is combo
            combo.activated.emit(1)  # what a real pick emits
            assert window.focusWidget() is None
        finally:
            window.close()

    def test_the_popup_caps_how_many_rows_it_shows(self, qt_app):
        # A dozen-plus languages/models/devices otherwise open a popup taller
        # than the window it belongs to.
        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown([f"Sprache {i}" for i in range(14)])
        combo.resize(240, 44)
        combo.show()
        try:
            combo.showPopup()
            view = combo.view()
            rows = view.viewport().height() / view.sizeHintForRow(0)
            assert rows <= Dropdown._MAX_VISIBLE_ITEMS + 0.35, (
                f"popup shows {rows:.1f} rows"
            )
        finally:
            combo.hidePopup()
            combo.close()

    def test_opening_a_popup_emits_no_font_warning(self, qt_app):
        # A stylesheet "font-size: Npx" leaves QFont.pointSize() at -1, and
        # QComboBox::showPopup feeds that back into QFont::setPointSize. The
        # base size therefore lives in the application font, in points.
        from PySide6.QtCore import qInstallMessageHandler

        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        assert qt_app.font().pointSizeF() > 0, "base font is not point-sized"

        messages: list[str] = []
        previous = qInstallMessageHandler(
            lambda mode, ctx, msg: messages.append(msg)
        )
        combo = Dropdown(["Deutsch", "English"])
        combo.resize(240, 44)
        combo.show()
        try:
            combo.showPopup()
        finally:
            combo.hidePopup()
            combo.close()
            qInstallMessageHandler(previous)
        assert not [m for m in messages if "PointSize" in m], messages

    def test_the_popup_styles_hover_and_selection(self):
        # Declaring ::item at all makes QStyleSheetStyle paint the rows, and it
        # then ignores the view's selection-background-color — so both states
        # need explicit rules or the open list has no highlight at all.
        from gui_qt.theme import stylesheet

        sheet = stylesheet("light")
        hover = sheet.index("QComboBox QAbstractItemView::item:hover")
        selected = sheet.index("QComboBox QAbstractItemView::item:selected")
        # Equal specificity, so the later rule wins: the selected row must keep
        # its accent fill while the pointer is over it.
        assert selected > hover

    def test_focus_outranks_hover_on_dropdowns(self):
        # Qt follows CSS specificity, so the two-pseudo-state hover rule beats a
        # plain ":focus" and pointing at a focused combo erased its accent ring.
        # The ":enabled:focus" twin ties on specificity and must come LATER.
        from gui_qt.theme import stylesheet

        sheet = stylesheet("light")
        assert sheet.index("QComboBox:enabled:focus") > sheet.index(
            "QComboBox:enabled:hover"
        )


def row_text(window, index: int) -> str:
    """Title + detail line of one history list row, as the delegate has them."""
    from gui_qt.history_window import RowDelegate

    item = window.entry_list.item(index)
    return f"{item.text()}\n{item.data(RowDelegate.SUB_ROLE)}"


def row_tag(window, index: int) -> str:
    from gui_qt.history_window import RowDelegate

    return window.entry_list.item(index).data(RowDelegate.TAG_ROLE)


class TestHistoryWindow:
    """Rendering only — parsing is utils/history.py and already covered."""

    @pytest.fixture
    def history(self, qt_app, monkeypatch):
        import gui_qt.history_window as hw
        from utils.history import HistoryEntry, HistorySession

        sessions = [
            HistorySession(
                date="2026-07-30",
                path="a.txt",
                start_time="10:00",
                end_time="10:30",
                duration_minutes=30,
                active_seconds=1200,
                language_pair="AR → GE",
                entry_count=2,
                has_summary=True,
            ),
            HistorySession(
                date="2026-07-29",
                path="b.txt",
                start_time="11:00",
                end_time="11:05",
                duration_minutes=5,
                active_seconds=120,
                language_pair="AR → EN",
                entry_count=1,
            ),
        ]
        files = {
            "a.txt": [
                HistoryEntry("10:00:01", "AR", "بسم الله"),
                HistoryEntry("10:00:02", "GE", "Im Namen Allahs"),
            ],
            # Same-language run: the pair is identical and must render once.
            "b.txt": [
                HistoryEntry("11:00:01", "AR", "الحمد لله"),
                HistoryEntry("11:00:02", "AR", "الحمد لله"),
            ],
        }
        monkeypatch.setattr(hw, "list_history_sessions", lambda: sessions)
        monkeypatch.setattr(hw, "parse_history_file", lambda p: files[p])
        monkeypatch.setattr(
            hw, "read_summary", lambda p: "Kurzfassung." if p == "a.txt" else None
        )

        made = []

        def _make():
            w = hw.HistoryWindow(lambda key, fallback="": fallback)
            made.append(w)
            return w

        yield _make, sessions
        for w in made:
            w.close()

    def test_lists_one_row_per_session(self, history):
        make, sessions = history
        w = make()
        assert w.entry_list.count() == len(sessions)
        assert "2026-07-30" in row_text(w, 0)

    def test_rows_never_widen_the_list(self, history):
        # The rows carry long lines ("... GE → EN, AR → EN, AR → GE · 52").
        # Asking for their full width made the item boxes wider than the
        # viewport, so the text ran out of its own rounded box. The delegate
        # elides to whatever width the row got, so it asks for none.
        from PySide6.QtWidgets import QStyleOptionViewItem

        make, _ = history
        w = make()
        delegate = w.entry_list.itemDelegate()
        option = QStyleOptionViewItem()
        option.font = w.entry_list.font()
        index = w.entry_list.model().index(0, 0)
        hint = delegate.sizeHint(option, index)
        assert hint.width() == 0
        # ...and tall enough for both lines, which one item widget was not:
        # the stylesheet's item padding left it 16px for a 40px row.
        assert hint.height() >= 2 * w.entry_list.fontMetrics().height()

    def test_opens_at_the_windowed_size(self, history):
        # Tk opens this viewer at 900x560; the port asked for 1180x720, which
        # covered the panel behind it.
        import gui_qt.history_window as hw

        make, _ = history
        w = make()
        assert (w.width(), w.height()) == (
            hw.HISTORY_WINDOW_W,
            hw.HISTORY_WINDOW_H,
        )

    def test_the_two_panes_do_not_touch(self, history):
        # The splitter handle measured 0px wide here — the stylesheet's
        # QSplitter::handle width never reached it, and the panes ended up
        # overlapping by a pixel — so the list border sat directly against the
        # transcript border. The gap lives in the right pane's own margin.
        import gui_qt.history_window as hw

        make, _ = history
        w = make()
        assert w.detail.parentWidget().layout().contentsMargins().left() >= hw.PANE_GAP

    def test_a_saved_summary_is_marked_on_the_row(self, history):
        import gui_qt.history_window as hw

        make, _ = history
        w = make()
        assert row_text(w, 0).startswith(hw.SUMMARY_MARK)
        assert not row_text(w, 1).startswith(hw.SUMMARY_MARK)

    def test_delete_is_the_danger_button(self, history):
        # Deleting a record is irreversible; it must not read like Copy.
        make, _ = history
        w = make()
        assert w.delete_btn.objectName() == "danger"
        assert w.summarise_btn.objectName() == "accent"

    def test_summarise_is_hidden_where_there_is_no_transcript(
        self, history, monkeypatch
    ):
        import gui_qt.history_window as hw

        monkeypatch.setattr(hw, "list_log_files", lambda: [])
        make, _ = history
        w = make()
        assert not w.summarise_btn.isHidden()
        w._on_tab(3)  # Log
        assert w.summarise_btn.isHidden()

    def test_first_session_is_selected_and_rendered(self, history):
        make, _ = history
        w = make()
        assert w.entry_list.currentRow() == 0
        text = w.detail.toPlainText()
        assert "بسم الله" in text and "Im Namen Allahs" in text

    def test_summary_is_shown_above_the_transcript(self, history):
        make, _ = history
        w = make()
        text = w.detail.toPlainText()
        assert text.index("Kurzfassung.") < text.index("بسم الله")

    def test_identical_pair_renders_once(self, history):
        # Same-language runs log transcription and translation identically;
        # showing both would read as the text being duplicated.
        make, _ = history
        w = make()
        w.entry_list.setCurrentRow(1)
        assert w.detail.toPlainText().count("الحمد لله") == 1

    def test_selecting_another_session_switches_the_transcript(self, history):
        make, _ = history
        w = make()
        first = w.detail.toPlainText()
        w.entry_list.setCurrentRow(1)
        assert w.detail.toPlainText() != first

    def test_empty_state(self, qt_app, monkeypatch):
        import gui_qt.history_window as hw

        monkeypatch.setattr(hw, "list_history_sessions", lambda: [])

        w = hw.HistoryWindow(lambda key, fallback="": fallback)
        try:
            assert w.entry_list.count() == 0
            assert w.detail.toPlainText() == ""
        finally:
            w.close()

    def test_unreadable_session_does_not_raise(self, history, monkeypatch):
        import gui_qt.history_window as hw

        make, _ = history

        def boom(path):
            raise OSError("unreadable")

        monkeypatch.setattr(hw, "parse_history_file", boom)
        w = make()  # must build and select without propagating the error
        assert w.detail.toPlainText() != ""


class TestHistoryBatchTab:
    """The Batch tab previews the run's own output, in the format it holds."""

    @pytest.fixture
    def batch(self, qt_app, monkeypatch, tmp_path):
        import gui_qt.history_window as hw
        from utils.history import BatchRun

        srt = tmp_path / "both.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo\n", encoding="utf-8-sig")
        runs = [
            BatchRun(
                date="2026-07-19",
                time="01:45",
                source_name="Do You Really Know Allah.mp4",
                path=str(tmp_path / "both.txt"),
                duration_minutes=10,
                active_seconds=600,
                language_pair="AU → GE",
                entry_count=83,
                has_summary=False,
                formats=["srt", "txt"],
            ),
            BatchRun(
                date="2026-07-11",
                time="18:11",
                source_name="1.mp3",
                path=str(tmp_path / "text-only.txt"),
                duration_minutes=1,
                active_seconds=60,
                language_pair="AR → GE",
                entry_count=10,
                has_summary=False,
                formats=["txt"],
            ),
        ]
        monkeypatch.setattr(hw, "list_batch_runs", lambda: runs)
        monkeypatch.setattr(hw, "parse_history_file", lambda _p: [])
        monkeypatch.setattr(hw, "read_batch_languages", lambda _p: ("Arabic", "German"))
        monkeypatch.setattr(hw, "list_history_sessions", lambda: [])

        made = []

        def _make():
            w = hw.HistoryWindow(lambda key, fallback="": fallback)
            w._on_tab(1)  # Batch
            made.append(w)
            return w

        yield _make, runs
        for w in made:
            w.close()

    def test_format_toggle_defaults_to_srt(self, batch):
        make, _ = batch
        w = make()
        assert not w.format_bar.isHidden()
        assert w.format_buttons["srt"].isChecked()
        assert "00:00:00,000" in w.detail.toPlainText()

    def test_switching_to_txt_shows_the_transcript(self, batch):
        make, _ = batch
        w = make()
        w._on_format("txt")
        assert w.format_buttons["txt"].isChecked()
        assert "00:00:00,000" not in w.detail.toPlainText()

    def test_a_single_format_run_hides_the_toggle(self, batch):
        # Nothing to choose between, so the toggle is noise.
        make, _ = batch
        w = make()
        w.entry_list.setCurrentRow(1)
        assert w.format_bar.isHidden()

    def test_the_toggle_does_not_leak_onto_another_tab(self, batch, monkeypatch):
        import gui_qt.history_window as hw

        monkeypatch.setattr(hw, "list_log_files", lambda: [])
        make, _ = batch
        w = make()
        assert not w.format_bar.isHidden()
        w._on_tab(3)  # Log — an SRT/TXT switch means nothing here
        assert w.format_bar.isHidden()

    def test_the_available_formats_are_tagged_on_the_row(self, batch):
        make, _ = batch
        w = make()
        assert row_tag(w, 0) == "SRT+TXT"

    def test_a_long_filename_keeps_its_extension(self, batch):
        # Elided in the middle, not at the end: ".mp4" is what tells the two
        # runs of the same lecture apart.
        from PySide6.QtCore import Qt

        from gui_qt.history_window import RowDelegate

        make, runs = batch
        w = make()
        item = w.entry_list.item(0)
        assert item.text().endswith(runs[0].source_name)  # stored whole
        assert item.data(RowDelegate.ELIDE_ROLE) == Qt.ElideMiddle

    def test_it_can_open_straight_onto_this_tab(self, qt_app, batch):
        # "Show in history" after a batch run must land on the run, not on the
        # session list — as it does in the Tk viewer.
        import gui_qt.history_window as hw

        _make, runs = batch
        w = hw.HistoryWindow(lambda key, fallback="": fallback, initial_tab="batch")
        try:
            assert w._tab == "batch"
            assert w.entry_list.count() == len(runs)
        finally:
            w.close()


class TestHistoryCostTab:
    """The Kosten tab: a spend chart over a per-session breakdown."""

    @pytest.fixture
    def cost(self, qt_app, monkeypatch):
        import gui_qt.history_window as hw

        sessions = [
            {
                "id": "s2",
                "started_at": "2026-07-28T14:08:00+00:00",
                "ended_at": "2026-07-28T14:12:00+00:00",
                "total_cost_usd": "0.1704",
                "fully_priced": True,
                "providers": {
                    "openai": {
                        "requests": 82,
                        "cost_usd": "0.1704",
                        "fully_priced": True,
                        "models": {
                            "gpt-5.2": {
                                "requests": 82,
                                "cost_usd": "0.1704",
                                "roles": ["translation"],
                                "fully_priced": True,
                            }
                        },
                    }
                },
            },
            {
                "id": "s1",
                "started_at": "2026-07-27T17:19:00+00:00",
                "ended_at": "2026-07-27T17:21:00+00:00",
                "total_cost_usd": "0.0331",
                "fully_priced": False,
                "providers": {
                    "gemini": {
                        "requests": 4,
                        "cost_usd": "0.0331",
                        "fully_priced": False,
                        "models": {},
                    }
                },
            },
        ]
        monkeypatch.setattr(hw, "list_cost_sessions", lambda: sessions)
        monkeypatch.setattr(hw, "list_history_sessions", lambda: [])

        made = []

        def _make():
            w = hw.HistoryWindow(lambda key, fallback="": fallback)
            w._on_tab(2)  # Cost
            made.append(w)
            return w

        yield _make, sessions
        for w in made:
            w.close()

    def test_the_breakdown_is_rendered(self, cost):
        # It was blank: the 30-day header was formatted with "sessions=" while
        # every translation of that string carries "{count}", so the KeyError
        # took the whole detail pane down.
        make, _ = cost
        w = make()
        text = w.detail.toPlainText()
        assert "OpenAI" in text and "gpt-5.2" in text

    def test_the_chart_is_shown_only_on_this_tab(self, cost):
        make, _ = cost
        w = make()
        assert not w.cost_chart.isHidden()
        w._on_tab(0)  # History (empty in this fixture)
        assert w.cost_chart.isHidden()

    def test_the_thirty_day_header_formats(self, cost):
        # The chart paints it; formatting it must not raise on any translation.
        from utils.cost_display import cost_window_total

        make, sessions = cost
        w = make()
        window = cost_window_total(sessions, days=30)
        header = w._t(
            "cost_last_30_days", "Last 30 days: {total} · {count} sessions"
        ).format(total=window.total, count=window.sessions)
        assert "2" in header

    def test_an_estimated_session_is_tagged(self, cost):
        make, _ = cost
        w = make()
        assert row_tag(w, 1) == "~"
        assert row_tag(w, 0) is None

    def test_clicking_a_bar_selects_that_session(self, cost):
        make, sessions = cost
        w = make()
        assert w.entry_list.currentRow() == 0
        w.cost_chart.selected.emit("s1")
        assert w.entry_list.currentRow() == 1


class TestAlreadyRunningDialog:
    """main.py's single-instance guard, under --qt.

    It used to show the CustomTkinter dialog whatever tree was asked for,
    which put Tk in a Qt-only process and set the process DPI awareness to
    per-monitor v1 before Qt could ask for the v2 context it wants.
    """

    @pytest.fixture
    def dialog(self, qt_app):
        from gui_qt.already_running import AlreadyRunningDialog

        made = AlreadyRunningDialog({})
        yield made
        made.close()

    def test_launch_anyway_accepts(self, dialog):
        from PySide6.QtWidgets import QDialog

        dialog.launch_btn.click()
        assert dialog.result() == QDialog.Accepted

    def test_cancel_rejects_and_answers_the_keyboard(self, dialog):
        from PySide6.QtWidgets import QDialog

        # Starting a second instance is what this dialog exists to prevent, so
        # the safe option is the one Enter and Esc land on.
        assert dialog.cancel_btn.isDefault()
        dialog.cancel_btn.click()
        assert dialog.result() == QDialog.Rejected

    def test_qt_mode_never_reaches_the_tk_dialog(self, monkeypatch):
        import types

        import main

        monkeypatch.setattr(main, "_QT_MODE", True)
        calls = []
        monkeypatch.setitem(
            sys.modules,
            "gui_qt.already_running",
            types.SimpleNamespace(
                show_already_running_dialog=lambda: calls.append(1) or True
            ),
        )
        # customtkinter must not even be imported on this path.
        monkeypatch.setitem(sys.modules, "customtkinter", None)
        assert main._show_already_running_dialog() is True
        assert calls == [1]


def _wait_for(app, predicate, timeout: float = 5.0) -> None:
    """Pump the event loop until ``predicate`` holds.

    The work runs on a worker thread and reports back as a queued signal, so
    the result only lands while events are being delivered.
    """
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition never became true")


class TestUpdateBanner:
    """The check_for_updates setting had a checkbox in the Qt settings window
    and nothing behind it."""

    @pytest.fixture
    def banner(self, qt_app, monkeypatch):
        import gui_qt.update_banner as ub
        from utils.update_check import UpdateInfo

        ub.reset_cache()
        monkeypatch.setattr(
            ub,
            "check_for_update",
            lambda: UpdateInfo(version="9.9.9", url="https://example.invalid/r"),
        )
        made = ub.UpdateBanner(lambda key, fallback="": fallback)
        yield made, ub
        made.close()
        ub.reset_cache()

    def test_hidden_until_a_newer_release_answers(self, banner):
        made, _ub = banner
        assert made.isHidden()

    def test_it_shows_the_version_it_found(self, banner, qt_app):
        made, _ub = banner
        made.start_check(True)
        _wait_for(qt_app, lambda: not made.isHidden())
        assert "9.9.9" in made.label.text()

    def test_opting_out_makes_no_request(self, banner, monkeypatch):
        made, ub = banner
        calls = []
        monkeypatch.setattr(ub, "check_for_update", lambda: calls.append(1))
        made.start_check(False)
        assert calls == []
        assert made.isHidden()

    def test_dismissing_hides_it_for_the_session(self, banner, qt_app):
        made, _ub = banner
        made.start_check(True)
        _wait_for(qt_app, lambda: not made.isHidden())
        made.close_btn.click()
        assert made.isHidden()

    def test_the_answer_is_reused_after_a_rebuild(self, banner, qt_app):
        # A GUI-language switch rebuilds the panel; a fresh request per rebuild
        # would be waste, and the banner should come straight back.
        made, ub = banner
        made.start_check(True)
        _wait_for(qt_app, lambda: not made.isHidden())
        calls = []
        ub.check_for_update = lambda: calls.append(1)
        second = ub.UpdateBanner(lambda key, fallback="": fallback)
        try:
            second.start_check(True)
            assert calls == []
            assert "9.9.9" in second.label.text()
        finally:
            second.close()


class TestBatchWindow:
    """The pipeline is batch/processor.py; these cover the window around it."""

    @pytest.fixture
    def batch(self, qt_app, monkeypatch):
        import sys
        import types

        import gui_qt.batch_window as bw
        from utils.settings import load_settings

        calls: dict = {}

        def fake_process_file(
            input_path, progress_callback=None, cancel_event=None, **kwargs
        ):
            calls.update(kwargs)
            calls["input_path"] = input_path
            for i in range(1, 4):
                if cancel_event is not None and cancel_event.is_set():
                    return None
                if progress_callback:
                    progress_callback(i, 3)
            return input_path + ".de.srt"

        # Stub the module the worker imports lazily, so no ffmpeg or API is
        # hit. It must carry FfmpegNotFoundError too: the worker imports it to
        # tell "install ffmpeg" apart from a real failure.
        class FfmpegNotFoundError(RuntimeError):
            pass

        monkeypatch.setitem(
            sys.modules,
            "batch.processor",
            types.SimpleNamespace(
                process_file=fake_process_file,
                FfmpegNotFoundError=FfmpegNotFoundError,
            ),
        )
        w = bw.BatchWindow(lambda k, f="": f, load_settings())
        yield w, calls
        w.close()

    def test_start_is_disabled_until_a_file_is_chosen(self, batch):
        w, _ = batch
        assert not w.start_btn.isEnabled()

    def test_it_opens_at_the_windowed_size(self, batch):
        # The port asked for a fixed 640x700 and towered over the Tk window.
        # Width is the Tk card's; the height follows the content and must not
        # be the sizeHint, which reserves a line a wrapped label never uses.
        import gui_qt.batch_window as bw

        w, _ = batch
        assert w.width() == bw.BATCH_WINDOW_W
        assert w.height() == w._natural_height()

    def test_the_controls_are_grouped_into_cards(self, batch):
        # Not one frame around the whole window: every other window in this
        # tree groups its controls into cards, and one big box read as the odd
        # one out.
        from PySide6.QtWidgets import QFrame

        w, _ = batch
        cards = [
            f
            for f in w.body.findChildren(QFrame)
            if f.objectName() == "card" and f.parent() is w.body
        ]
        assert len(cards) == 3

    def test_start_never_scrolls_away(self, batch):
        # The action bar is outside the scroll area, so however tall the cards
        # get, Start stays on screen.
        w, _ = batch
        assert w.action_bar.parent() is w
        assert not w.scroll.isAncestorOf(w.start_btn)
        assert w.scroll.isAncestorOf(w.pick_btn)

    def test_more_settings_grows_the_window_and_shrinks_it_back(self, batch, qt_app):
        # Shown, and with an event pass after each toggle: a widget's show/hide
        # invalidates the layout through a POSTED event, so a measure in the
        # same call reads the previous state — which is how the window came to
        # grow when the panel opened and stay tall when it closed.
        w, _ = batch
        w.show()
        qt_app.processEvents()
        before = w.height()
        w.more.set_expanded(True)
        _settle(qt_app)
        assert w.height() > before
        w.more.set_expanded(False)
        _settle(qt_app)
        assert w.height() == before

    def test_the_progress_bar_is_always_on_screen(self, batch):
        # It used to appear only once a run started, so the window jumped a row
        # taller at the moment the user was watching it.
        w, _ = batch
        assert not w.progress.isHidden()
        w._input_path = "khutbah.mp3"
        w._on_start()
        w.worker._thread.join(timeout=5)
        assert not w.progress.isHidden()

    def test_show_in_history_is_always_clickable(self, batch):
        # Past runs are in the history viewer whether or not one finished in
        # THIS window; disabling it until then hid a working feature.
        w, _ = batch
        assert w.history_btn.isEnabled()
        assert not w.folder_btn.isEnabled()  # nothing written yet

    def test_bilingual_subtitles_default_on(self, batch):
        # The Tk window defaults it on, and it decides what the .srt contains.
        w, _ = batch
        assert w._bilingual_srt()

    def test_a_transcript_only_run_disables_the_subtitle_choice(self, batch):
        # No SRT is written, so "original + translation" would decide nothing.
        w, _ = batch
        w.output_segment.set_current_index(1)  # .txt
        w._sync_bilingual_state()
        assert not w.bilingual_segment.isEnabled()
        assert not w._bilingual_srt()
        w.output_segment.set_current_index(0)  # .srt
        w._sync_bilingual_state()
        assert w.bilingual_segment.isEnabled()

    def test_the_picker_button_carries_the_chosen_file(self, batch):
        w, _ = batch
        assert w.clear_btn.isHidden()  # nothing to clear yet
        w._input_path = "C:/rec/khutbah.mp3"
        w._sync_file_row()
        assert w.pick_btn.text() == "khutbah.mp3"
        assert not w.clear_btn.isHidden()
        w._on_clear()
        assert w.clear_btn.isHidden()

    def test_a_long_filename_keeps_its_extension(self, batch):
        w, _ = batch
        w._input_path = "C:/rec/" + "The episode is out now, catch it while it is gone.m4a"
        assert w._file_button_text().endswith(".m4a")
        assert w._file_button_text().startswith("The episode")

    def test_a_missing_ffmpeg_offers_the_download(self, batch, monkeypatch):
        # Anything that is not already a 16 kHz WAV goes through ffmpeg. The
        # port reported the raw exception, which is a dead end; the Tk window
        # offers to fetch it once, with consent.
        w, _ = batch
        offered = []
        monkeypatch.setattr(
            w, "_offer_ffmpeg_download", lambda: offered.append(1) or True
        )
        monkeypatch.setattr(sys, "platform", "win32")
        w._on_ffmpeg_missing()
        assert offered == [1]
        # The offer was accepted, so nothing is reported as an error yet.
        assert w.status.objectName() != "status_error"

    def test_declining_the_download_explains_what_to_install(self, batch, monkeypatch):
        w, _ = batch
        monkeypatch.setattr(w, "_offer_ffmpeg_download", lambda: False)
        w._on_ffmpeg_missing()
        assert w.status.objectName() == "status_error"
        assert "ffmpeg" in w.status.text()

    def test_the_run_resumes_once_ffmpeg_is_downloaded(self, batch):
        # The whole point of the offer: the user asked for a run, not for a
        # download. The join matters — the emitting thread is still alive, and
        # _on_start's "already running" guard would skip the restart.
        w, calls = batch
        w._input_path = "khutbah.mp3"
        w._on_download_finished()
        w.worker.join(timeout=5)
        assert calls.get("input_path") == "khutbah.mp3"

    def test_the_status_line_reports_the_outcome_in_colour(self, batch):
        # Through an object name, not a widget stylesheet: an id rule in the
        # app sheet outranks one, and it would survive a theme switch stale.
        w, _ = batch
        w._on_finished("khutbah.mp3.de.srt")
        assert w.status.objectName() == "status_ok"
        w._on_failed("boom")
        assert w.status.objectName() == "status_error"
        w._on_finished("")  # cancelled: the processor writes nothing
        assert w.status.objectName() == "status_warn"

    def test_output_format_maps_from_the_segment(self, batch):
        w, _ = batch
        for index, expected in enumerate(("srt", "txt", "both")):
            w.output_segment.set_current_index(index)
            assert w._output_format() == expected

    def test_options_reach_the_processor(self, batch, qt_app):
        w, calls = batch
        w._input_path = "khutbah.mp3"
        w.output_segment.set_current_index(2)
        w.bilingual_segment.set_current_index(1)
        w._on_start()
        # The worker runs on its own thread; wait for it rather than sleeping.
        w.worker._thread.join(timeout=5)
        assert calls["input_path"] == "khutbah.mp3"
        assert calls["output_format"] == "both"
        assert calls["bilingual_srt"] is True

    def test_cancel_sets_the_event(self, batch):
        w, _ = batch
        w._input_path = "khutbah.mp3"
        w._on_start()
        w._on_cancel()
        assert w.worker.cancel_event.is_set()

    def test_closing_cancels_a_running_job(self, batch, monkeypatch):
        # Otherwise the run continues and writes files after the window is gone.
        # Needs a job that is still running when close() lands, so this stub
        # blocks until cancelled rather than returning immediately.
        import sys
        import types

        w, _ = batch
        started = threading.Event()

        def blocking_process_file(input_path, progress_callback=None,
                                  cancel_event=None, **kwargs):
            started.set()
            cancel_event.wait(timeout=5)
            return None

        monkeypatch.setitem(
            sys.modules,
            "batch.processor",
            types.SimpleNamespace(
                process_file=blocking_process_file,
                # Same module surface as the fixture's stub, different run.
                FfmpegNotFoundError=sys.modules[
                    "batch.processor"
                ].FfmpegNotFoundError,
            ),
        )
        w._input_path = "khutbah.mp3"
        w._on_start()
        assert started.wait(timeout=5), "worker never started"
        w.close()
        assert w.worker.cancel_event.is_set()


class TestAnnounceWindow:
    @pytest.fixture
    def announce(self, qt_app, monkeypatch):
        import gui_qt.announce_window as aw
        from utils.settings import load_settings

        monkeypatch.setattr(aw, "save_settings", lambda s: None)

        class FakePanel:
            """Stands in for the control panel: it owns the overlay's
            lifecycle, so the window asks it rather than holding one."""

            def __init__(self):
                self.text = None

            def show_announcement(self, t):
                self.text = t

            def clear_announcement(self):
                self.text = None

            def has_active_announcement(self):
                return bool(self.text)

        settings = load_settings()
        settings.announcement_history = ["Zweite Nachricht."]
        settings.announcement_favorites = ["Bitte Handys stummschalten."]
        overlay = FakePanel()
        w = aw.AnnounceWindow(lambda k, f="": f, settings, overlay)
        yield w, settings, overlay
        w.close()

    def test_send_reaches_the_overlay_and_is_remembered(self, announce):
        w, settings, overlay = announce
        w.text.setPlainText("Test-Ankuendigung")
        w.send_announcement()
        assert overlay.text == "Test-Ankuendigung"
        assert settings.announcement_history[0] == "Test-Ankuendigung"

    def test_history_is_capped_and_deduplicated(self, announce):
        from config import ANNOUNCEMENT_HISTORY_MAX

        w, settings, _ = announce
        for i in range(ANNOUNCEMENT_HISTORY_MAX + 3):
            w.text.setPlainText(f"Nachricht {i}")
            w.send_announcement()
        assert len(settings.announcement_history) == ANNOUNCEMENT_HISTORY_MAX
        w.text.setPlainText("Nachricht 0")
        w.send_announcement()
        assert settings.announcement_history.count("Nachricht 0") == 1

    def test_empty_text_sends_nothing(self, announce):
        w, _, overlay = announce
        w.text.setPlainText("   ")
        w.send_announcement()
        assert overlay.text is None

    def test_until_stopped_arms_no_timer(self, announce):
        from config import ANNOUNCEMENT_DURATIONS_SECONDS

        w, _, _ = announce
        last = len(ANNOUNCEMENT_DURATIONS_SECONDS) - 1
        assert ANNOUNCEMENT_DURATIONS_SECONDS[last] == 0
        w.duration_combo.setCurrentIndex(last)
        w.text.setPlainText("Bleibt stehen")
        w.send_announcement()
        assert not w._auto_clear.isActive()

    def test_timed_announcement_arms_the_timer(self, announce):
        w, settings, _ = announce
        w.duration_combo.setCurrentIndex(0)
        w.text.setPlainText("Kurz")
        w.send_announcement()
        assert w._auto_clear.isActive()
        assert settings.announcement_duration_index == 0

    def test_stop_clears_the_overlay_and_the_timer(self, announce):
        w, _, overlay = announce
        w.duration_combo.setCurrentIndex(0)
        w.text.setPlainText("Kurz")
        w.send_announcement()
        w.stop_announcement()
        assert overlay.text is None
        assert not w._auto_clear.isActive()

    def test_favorites_toggle_and_cap(self, announce, monkeypatch):
        import gui_qt.announce_window as aw
        from config import ANNOUNCEMENT_FAVORITES_MAX

        w, settings, _ = announce
        monkeypatch.setattr(aw, "show_message", lambda *a, **k: None)
        settings.announcement_favorites = []
        for i in range(ANNOUNCEMENT_FAVORITES_MAX):
            w._favorite(f"Favorit {i}")
        assert len(settings.announcement_favorites) == ANNOUNCEMENT_FAVORITES_MAX
        w._favorite("Einer zu viel")  # refused at the cap
        assert "Einer zu viel" not in settings.announcement_favorites
        w._unfavorite("Favorit 0")
        assert "Favorit 0" not in settings.announcement_favorites

    def test_favoriting_removes_the_recent_copy(self, announce):
        # Otherwise the same text is pinned AND rotating in Recent, which is
        # what "it's double there" was.
        w, settings, _ = announce
        settings.announcement_history = ["Bitte Handys stummschalten.", "Andere"]
        settings.announcement_favorites = []
        w._favorite("Bitte Handys stummschalten.")
        assert settings.announcement_history == ["Andere"]

    def test_deleting_a_recent_drops_it(self, announce):
        w, settings, _ = announce
        settings.announcement_history = ["Weg damit", "Bleibt"]
        w._delete_recent("Weg damit")
        assert settings.announcement_history == ["Bleibt"]

    def test_sending_a_favorite_does_not_add_it_to_recents(self, announce):
        w, settings, _ = announce
        settings.announcement_favorites = ["Gepinnt"]
        settings.announcement_history = []
        w.text.setPlainText("Gepinnt")
        w.send_announcement()
        assert settings.announcement_history == []

    def test_the_window_follows_its_lists_in_BOTH_directions(self, announce, qt_app):
        # The reported bug: the height was read before the layout had been told
        # a row was added, so the window lagged one entry behind — it opened
        # too short for its own content and never shrank again when entries
        # were deleted.
        from gui_qt.window_size import SECONDARY_MAX_H

        w, settings, _ = announce
        w.show()
        _settle(qt_app)
        heights = []
        for count in range(6):
            settings.announcement_history = [f"Nachricht {i}" for i in range(count)]
            w._refresh_lists()
            _settle(qt_app)
            heights.append(w.height())
            expected = min(w._natural_height(), SECONDARY_MAX_H)
            assert w.height() == expected, f"lagging at {count} entries"
        # Growing, then all the way back to where it started.
        assert heights[-1] > heights[1]
        settings.announcement_history = []
        w._refresh_lists()
        _settle(qt_app)
        assert w.height() == heights[0]

    def test_send_stays_reachable_however_long_the_lists_get(self, announce, qt_app):
        from config import ANNOUNCEMENT_FAVORITES_MAX, ANNOUNCEMENT_HISTORY_MAX

        w, settings, _ = announce
        settings.announcement_favorites = [
            f"Favorit {i}" for i in range(ANNOUNCEMENT_FAVORITES_MAX)
        ]
        settings.announcement_history = [
            f"Nachricht {i}" for i in range(ANNOUNCEMENT_HISTORY_MAX)
        ]
        w.show()
        w._refresh_lists()
        _settle(qt_app)
        bottom = w.send_btn.mapTo(w, w.send_btn.rect().bottomLeft()).y()
        assert bottom <= w.height()
        # …because the buttons are outside the scroll area, not because the
        # window grew without limit.
        assert not w.scroll.isAncestorOf(w.send_btn)

    def test_full_lists_scroll_instead_of_filling_the_screen(self, announce, qt_app):
        # Five favourites and three recents came to ~1080px — the height of the
        # screen, for a box you type one line into.
        from gui_qt.window_size import SECONDARY_MAX_H

        w, settings, _ = announce
        settings.announcement_favorites = [f"Favorit {i}" for i in range(5)]
        settings.announcement_history = [f"Nachricht {i}" for i in range(20)]
        w.show()
        w._refresh_lists()
        _settle(qt_app)
        assert w._natural_height() > SECONDARY_MAX_H  # it would have grown past it
        assert w.height() == SECONDARY_MAX_H
        assert w.scroll.verticalScrollBar().maximum() > 0  # the rest is reachable
        screen = w.screen()
        if screen is not None:
            assert w.height() <= screen.availableGeometry().height()

    def test_sending_takes_the_window_back_to_the_front(self, announce, qt_app):
        # Sending can CREATE the always-on-top overlay, which comes up over
        # this window; the Tk one lifts itself afterwards and so must this.
        w, _settings, overlay = announce
        raised = []
        overlay.bring_to_front = raised.append
        w.show()
        _settle(qt_app)
        w.text.setPlainText("Gleich geht es los")
        w.send_announcement()
        assert raised == [w]

    def test_favorites_are_excluded_from_recents(self, announce):
        # A favourite would otherwise occupy both lists.
        w, settings, _ = announce
        settings.announcement_favorites = ["Doppelt"]
        settings.announcement_history = ["Doppelt", "Einmalig"]
        w._refresh_lists()
        # Each row is a widget holding [text button, star, delete]; the text
        # button carries the full message as its tooltip.
        rows = w._recent_rows
        labels = []
        for i in range(rows.count()):
            widget = rows.itemAt(i).widget()
            layout = widget.layout() if widget is not None else None
            if layout is not None and layout.count():
                labels.append(layout.itemAt(0).widget().toolTip())
        assert "Doppelt" not in labels
        assert "Einmalig" in labels


class TestThemedDialogs:
    """Message boxes are the app's own dialog, not the platform's.

    QMessageBox draws system chrome that ignores the theme, hard-codes English
    button labels, and plays the Windows alert sound (the icon is what triggers
    it) — the Tk tree replaced them long ago.
    """

    @pytest.fixture
    def texts(self):
        from gui_qt.i18n import load_gui_translations

        de = load_gui_translations("de")
        return lambda key, fallback="": de.get(key, fallback)

    def test_the_buttons_speak_the_gui_language(self, qt_app, texts):
        from gui_qt.dialogs import MessageDialog

        dialog = MessageDialog(None, "Titel", "Nachricht", translate=texts)
        assert dialog.ok_btn.text() == "OK"
        confirm = MessageDialog(
            None, "Titel", "Nachricht", confirm=True, translate=texts
        )
        assert confirm.yes_btn.text() == "Ja"
        assert confirm.no_btn.text() == "Nein"
        for w in (dialog, confirm):
            w.close()

    def test_a_confirm_reports_the_answer(self, qt_app, texts):
        from PySide6.QtWidgets import QDialog

        from gui_qt.dialogs import MessageDialog

        dialog = MessageDialog(None, "T", "M", confirm=True, translate=texts)
        dialog.no_btn.click()
        assert dialog.result() != QDialog.Accepted
        dialog = MessageDialog(None, "T", "M", confirm=True, translate=texts)
        dialog.yes_btn.click()
        assert dialog.result() == QDialog.Accepted

    def test_a_destructive_confirm_defaults_to_no(self, qt_app, texts):
        from gui_qt.dialogs import MessageDialog

        dialog = MessageDialog(
            None, "T", "M", confirm=True, default_yes=False, translate=texts
        )
        assert dialog.no_btn.isDefault()
        assert not dialog.yes_btn.isDefault()
        dialog.close()

    def test_the_severity_decides_the_glyph_colour(self, qt_app):
        from PySide6.QtWidgets import QLabel

        from gui_qt.dialogs import MessageDialog

        names = {}
        for kind in ("info", "warn", "error"):
            dialog = MessageDialog(None, "T", "M", kind=kind)
            icon = next(
                w
                for w in dialog.findChildren(QLabel)
                if w.objectName().startswith("dialog_icon")
            )
            names[kind] = icon.objectName()
            dialog.close()
        assert len(set(names.values())) == 3

    def test_a_long_message_is_not_clipped(self, qt_app):
        from gui_qt.dialogs import MessageDialog

        short = MessageDialog(None, "T", "Kurz")
        long = MessageDialog(None, "T", "Sehr lang. " * 40)
        assert long.height() > short.height()
        # …and the height is the content's, not a fixed box it overflows.
        assert long.height() == long.layout().totalHeightForWidth(long.width())
        for w in (short, long):
            w.close()

    def test_no_qmessagebox_is_left_in_the_qt_tree(self):
        # A single leftover brings back the system sound and the English
        # buttons, and it is invisible until someone hits that code path.
        import pathlib
        import re

        root = pathlib.Path(__file__).resolve().parents[1] / "gui_qt"
        offenders = []
        for path in root.glob("*.py"):
            if path.name in ("dialogs.py", "theme.py"):
                continue  # the replacement itself, and one stylesheet selector
            for number, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                if re.search(r"\bQMessageBox\b", line) and not line.lstrip().startswith(
                    ("#", "``", '"')
                ):
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, offenders


class TestOnboardingWizard:
    """First-run setup. Everything runs against a temp settings file."""

    @pytest.fixture
    def wizard(self, qt_app, tmp_path, monkeypatch):
        import gui_qt.onboarding as ob
        import providers
        import utils.settings as S

        monkeypatch.setattr(S, "_settings_path", lambda: tmp_path / "settings.json")
        # resolve_provider_by_keys consults the real keyring, so on a machine
        # with keys stored the default always wins and the test proves nothing.
        monkeypatch.setattr(providers, "has_configured_key", lambda pid: False)
        monkeypatch.setattr(providers, "has_usable_key", lambda pid: False)
        monkeypatch.setattr(ob, "get_stored_api_key", lambda pid: None)
        saved: dict[str, str] = {}
        monkeypatch.setattr(
            ob, "save_api_key", lambda pid, key: saved.setdefault(pid, key) or True
        )

        w = ob.OnboardingWizard(qt_app)
        yield w, saved, S
        w.close()

    @staticmethod
    def _complete(w, provider_id=None, key=None):
        """Walk every step, optionally entering a key for one provider."""
        w.stack.setCurrentIndex(3)
        if provider_id:
            idx = w.provider_combo.findData(provider_id)
            w.provider_combo.setCurrentIndex(idx)
            w.key_edit.setText(key or "")
        w.stack.setCurrentIndex(4)
        w.disclaimer_check.setChecked(True)
        w._finish()

    def test_finish_is_blocked_until_the_disclaimer_is_accepted(self, wizard):
        w, _, _ = wizard
        w.stack.setCurrentIndex(w.stack.count() - 1)
        w._sync_nav()
        assert not w.next_btn.isEnabled()
        w.disclaimer_check.setChecked(True)
        assert w.next_btn.isEnabled()

    def test_completing_writes_the_flags(self, wizard):
        w, _, S = wizard
        self._complete(w)
        s = S.load_settings(use_cache=False)
        assert s.onboarding_completed is True
        assert s.disclaimer_accepted is True

    def test_one_appearance_answer_drives_both_windows(self, wizard):
        from utils.settings import THEME_MODES

        w, _, S = wizard
        w.theme_segment._buttons[THEME_MODES.index("light")].click()
        self._complete(w)
        s = S.load_settings(use_cache=False)
        assert s.theme_mode == "light"
        assert s.subtitle_theme_mode == "light"

    def test_lands_on_realtime_streaming(self, wizard):
        from utils.settings import PIPELINE_MODE_STREAMING

        w, _, S = wizard
        self._complete(w)
        assert S.load_settings(use_cache=False).pipeline_mode == PIPELINE_MODE_STREAMING

    def test_keys_decide_the_provider_not_the_dropdown(self, wizard):
        # Browsing to a provider without entering its key must not select it.
        w, _, S = wizard
        self._complete(w, provider_id="anthropic", key="sk-ant-test")
        s = S.load_settings(use_cache=False)
        assert s.ai_provider == "anthropic"
        # A non-default provider must not sit behind a ticked "Standard".
        assert s.use_default_translation_model is False

    def test_default_provider_when_no_key_is_entered(self, wizard):
        from utils.settings import DEFAULT_AI_PROVIDER

        w, _, S = wizard
        self._complete(w)
        s = S.load_settings(use_cache=False)
        assert s.ai_provider == DEFAULT_AI_PROVIDER
        assert s.use_default_translation_model is True

    def test_realtime_engine_follows_the_chosen_provider(self, wizard):
        # The key just entered must be the one the pipeline authenticates with.
        w, _, S = wizard
        self._complete(w, provider_id="openai", key="sk-openai-test")
        s = S.load_settings(use_cache=False)
        assert s.transcription_provider == "openai_realtime"

    def test_anthropic_falls_back_to_a_streaming_engine(self, wizard):
        from utils.settings import STREAMING_TRANSCRIPTION_PROVIDERS

        # Anthropic has no realtime engine of its own.
        w, _, S = wizard
        self._complete(w, provider_id="anthropic", key="sk-ant-test")
        s = S.load_settings(use_cache=False)
        assert s.transcription_provider in STREAMING_TRANSCRIPTION_PROVIDERS

    def test_entered_key_is_persisted(self, wizard):
        w, saved, _ = wizard
        self._complete(w, provider_id="anthropic", key="sk-ant-test")
        assert saved.get("anthropic") == "sk-ant-test"

    def test_run_onboarding_no_ops_once_completed(self, wizard, qt_app):
        import gui_qt.onboarding as ob

        w, _, _ = wizard
        self._complete(w)
        assert ob.run_onboarding(qt_app) is True


class TestWizardInputMeter:
    """The device step carries the control panel's level meter.

    A dead or far too quiet microphone should be caught during setup, not
    during the first sermon — the same reason the Tk wizard has one.
    """

    class FakeSnapshot:
        rms_dbfs = -18.0
        clipping_ratio = 0.0

    class FakeController:
        def __init__(self):
            self.started: list = []
            self.stopped = 0
            self.running = False

        def get_input_level(self):
            return TestWizardInputMeter.FakeSnapshot()

        def is_input_level_test_running(self):
            return self.running

        def start_input_level_test(self, index=None):
            self.started.append(index)
            self.running = True

        def stop_input_level_test(self):
            self.stopped += 1
            self.running = False

    @pytest.fixture
    def wizard(self, qt_app, monkeypatch):
        import gui_qt.onboarding as ob

        monkeypatch.setattr(ob, "save_settings", lambda s: None)
        controller = self.FakeController()
        w = ob.OnboardingWizard(qt_app, controller)
        yield w, controller
        w.close()

    def test_the_meter_reads_the_controller(self, wizard):
        w, _ = wizard
        w._poll_level()
        assert w.level_bar.value() > 0.5
        assert "dBFS" in w.level_value.text()

    def test_entering_the_device_step_starts_a_preview(self, wizard):
        import gui_qt.onboarding as ob

        w, controller = wizard
        w.stack.setCurrentIndex(ob._DEVICE_STEP)
        assert controller.started, "no preview started on the device step"

    def test_leaving_the_step_releases_the_device(self, wizard):
        import gui_qt.onboarding as ob

        w, controller = wizard
        w.stack.setCurrentIndex(ob._DEVICE_STEP)
        w.stack.setCurrentIndex(ob._DEVICE_STEP + 1)
        assert controller.stopped >= 1, "preview kept the input device open"

    def test_the_test_button_toggles_the_preview(self, wizard):
        w, controller = wizard
        w._toggle_level_test()
        assert controller.running
        w._toggle_level_test()
        assert not controller.running

    def test_a_failing_device_does_not_break_browsing(self, wizard, monkeypatch):
        # Auto-previews must stay silent: the user is only picking from a list.
        w, controller = wizard

        def boom(index=None):
            raise RuntimeError("device busy")

        monkeypatch.setattr(controller, "start_input_level_test", boom)
        w._start_level_preview(auto=True)  # must not raise or dialog

    def test_it_builds_without_a_controller(self, qt_app, monkeypatch):
        import gui_qt.onboarding as ob

        monkeypatch.setattr(ob, "save_settings", lambda s: None)
        w = ob.OnboardingWizard(qt_app)
        try:
            w._start_level_preview(auto=True)  # no controller: a no-op
            w._stop_level_capture()
        finally:
            w.close()

    def test_the_preview_runs_until_it_is_stopped(self, wizard):
        # Unlike the control panel's meter, this one is never on a timer:
        # setup is when someone talks into the mic to watch the bar move.
        w, controller = wizard
        w._start_level_preview(auto=True)
        assert controller.running
        assert w._level_timer.isActive()
        assert not hasattr(w, "_level_auto_stop"), "an auto-stop timer is back"

    def test_switching_device_keeps_the_preview_on_the_new_one(self, wizard):
        w, controller = wizard
        if w.device_combo.count() < 2:
            pytest.skip("needs two input devices")
        w._start_level_preview(auto=True)
        before = len(controller.started)
        w.device_combo.setCurrentIndex(1)
        assert len(controller.started) == before + 1
        assert controller.running

    def test_switching_device_does_not_reopen_a_stopped_preview(self, wizard):
        w, controller = wizard
        if w.device_combo.count() < 2:
            pytest.skip("needs two input devices")
        w._stop_level_capture()
        before = len(controller.started)
        w.device_combo.setCurrentIndex(1)
        assert len(controller.started) == before
        assert not controller.running


class TestWizardProviderList:
    @pytest.fixture
    def wizard(self, qt_app, monkeypatch):
        import gui_qt.onboarding as ob

        monkeypatch.setattr(ob, "save_settings", lambda s: None)
        w = ob.OnboardingWizard(qt_app)
        yield w
        w.close()

    def test_deepgram_is_offered_so_its_key_can_be_entered(self, wizard):
        # It has no translation capability, so it is not in PROVIDER_CHOICES —
        # but it IS a real-time transcription engine, and the wizard is where
        # keys are entered.
        ids = [
            wizard.provider_combo.itemData(i)
            for i in range(wizard.provider_combo.count())
        ]
        assert "deepgram" in ids

    def test_only_the_shipped_default_is_tagged(self, wizard):
        from utils.settings import DEFAULT_AI_PROVIDER

        tagged = [
            wizard.provider_combo.itemData(i)
            for i in range(wizard.provider_combo.count())
            if "(" in wizard.provider_combo.itemText(i)
        ]
        assert tagged == [DEFAULT_AI_PROVIDER]

    def test_a_deepgram_key_never_becomes_the_translation_provider(self, wizard):
        # Deepgram cannot translate; picking it must only store its key.
        from utils.settings import DEFAULT_AI_PROVIDER

        index = wizard.provider_combo.findData("deepgram")
        wizard.provider_combo.setCurrentIndex(index)
        wizard.key_edit.setText("dg-test-key")
        wizard._capture_current_key()
        assert wizard._provider_keys["deepgram"] == "dg-test-key"
        from providers import resolve_provider_by_keys

        assert resolve_provider_by_keys({"deepgram": "dg-test-key"}) == (
            DEFAULT_AI_PROVIDER
        )

    def test_deepgram_has_key_help_links(self, wizard):
        index = wizard.provider_combo.findData("deepgram")
        wizard.provider_combo.setCurrentIndex(index)
        assert wizard.key_help_btn.isEnabled()
        assert wizard.key_site_btn.isEnabled()

    def _note_texts(self, wizard) -> list[str]:
        layout = wizard._notes_layout
        return [
            layout.itemAt(i).widget().label.text() for i in range(layout.count())
        ]

    def test_deepgram_warns_that_it_only_transcribes(self, wizard):
        # A Deepgram key alone is never a working setup: no translation model
        # and no embedding model, so the caveat has to be on this step.
        wizard.provider_combo.setCurrentIndex(
            wizard.provider_combo.findData("deepgram")
        )
        notes = self._note_texts(wizard)
        assert len(notes) == 1
        assert "Deepgram" in notes[0]

    def test_notes_follow_the_selected_provider(self, wizard):
        wizard.provider_combo.setCurrentIndex(
            wizard.provider_combo.findData("deepgram")
        )
        wizard.provider_combo.setCurrentIndex(
            wizard.provider_combo.findData("anthropic")
        )
        notes = self._note_texts(wizard)
        assert len(notes) == 2, "the Deepgram note outlived its selection"
        assert not any("Deepgram" in note for note in notes)

    def test_every_provider_hints_at_its_key_format(self, wizard):
        # Only OpenAI used to show one, so the other fields looked like they
        # wanted something different.
        from providers import KEY_PLACEHOLDERS

        for pid, expected in KEY_PLACEHOLDERS.items():
            index = wizard.provider_combo.findData(pid)
            if index < 0:
                continue
            wizard.provider_combo.setCurrentIndex(index)
            assert wizard.key_edit.placeholderText() == expected, pid

    def test_the_show_toggle_reveals_the_key(self, wizard):
        from PySide6.QtWidgets import QLineEdit

        assert wizard.key_edit.echoMode() == QLineEdit.Password
        wizard.show_key_check.setChecked(True)
        assert wizard.key_edit.echoMode() == QLineEdit.Normal
        wizard.show_key_check.setChecked(False)
        assert wizard.key_edit.echoMode() == QLineEdit.Password


class TestWizardLanguageSwitch:
    """Step 1 changes the GUI language, so every later step must follow it."""

    @pytest.fixture
    def wizard(self, qt_app, monkeypatch):
        import gui_qt.onboarding as ob

        monkeypatch.setattr(ob, "save_settings", lambda s: None)
        w = ob.OnboardingWizard(qt_app)
        w.gui_lang_combo.setCurrentIndex(w.gui_lang_combo.findData("en"))
        yield w
        w.close()

    def test_later_steps_are_relabelled(self, wizard):
        from gui_qt.i18n import load_gui_translations

        de = load_gui_translations("de")
        wizard.gui_lang_combo.setCurrentIndex(wizard.gui_lang_combo.findData("de"))
        # A label from a step the user has not reached yet.
        assert wizard.disclaimer_check.text() == de["wizard_disclaimer_accept"]
        assert wizard.title_label.text() == de["wizard_title"]
        assert wizard.next_btn.text() == de["wizard_next"]

    def test_the_theme_segment_is_relabelled(self, wizard):
        from gui_qt.i18n import load_gui_translations
        from utils.settings import THEME_MODES

        de = load_gui_translations("de")
        wizard.gui_lang_combo.setCurrentIndex(wizard.gui_lang_combo.findData("de"))
        labels = [btn.text() for btn in wizard.theme_segment._buttons]
        assert labels == [de[f"theme_{m}"] for m in THEME_MODES]

    def test_a_typed_key_survives_the_switch(self, wizard):
        # Re-labelling must not be a rebuild: Back from the key step is a
        # normal way to reach the language step.
        wizard.stack.setCurrentIndex(3)
        wizard.key_edit.setText("sk-typed")
        wizard.gui_lang_combo.setCurrentIndex(wizard.gui_lang_combo.findData("de"))
        assert wizard.key_edit.text() == "sk-typed"


class TestPipelineBridge:
    def test_queue_items_become_qt_signals(self, qt_app):
        from PySide6.QtCore import QTimer

        from gui_qt.pipeline_bridge import PipelineBridge

        class FakeController:
            def __init__(self):
                self.translation_queue = queue.Queue()
                self.error_queue = queue.Queue()

            def get_live_transcript(self):
                return ("interim", False)

        controller = FakeController()
        bridge = PipelineBridge(controller)
        received: list[tuple] = []
        bridge.translation.connect(lambda t, s: received.append((t, s)))
        bridge.start(streaming=False, show_interim=False)

        controller.translation_queue.put(("Erste", "أولى"))
        controller.translation_queue.put(("Zweite", None))  # same-language case

        # Pump the event loop so queued cross-thread signals are delivered.
        QTimer.singleShot(900, qt_app.quit)
        qt_app.exec()
        bridge.stop()

        assert received == [("Erste", "أولى"), ("Zweite", None)]

    def test_audio_device_lost_is_routed_separately(self, qt_app):
        from PySide6.QtCore import QTimer

        from gui_qt.pipeline_bridge import PipelineBridge

        class FakeController:
            def __init__(self):
                self.translation_queue = queue.Queue()
                self.error_queue = queue.Queue()

            def get_live_transcript(self):
                return ("", False)

        controller = FakeController()
        bridge = PipelineBridge(controller)
        lost: list[bool] = []
        bridge.audio_device_lost.connect(lambda: lost.append(True))
        bridge.start(streaming=False, show_interim=False)
        controller.error_queue.put("audio_device_lost:12")

        QTimer.singleShot(900, qt_app.quit)
        qt_app.exec()
        bridge.stop()

        assert lost == [True]


class TestApiKeyActivation:
    def test_stored_key_is_activated_into_the_client(self, monkeypatch):
        # Regression: a stored key is not enough — the OpenAI client keeps its
        # key in module state and raises until set_api_key() is called, so
        # has_usable_key() could be True while Start failed with
        # "OpenAI API key is not configured."
        import gui_qt.api_keys as api_keys
        import providers.openai.client as client

        activated: list[str] = []
        monkeypatch.setattr(api_keys, "get_stored_api_key", lambda p: "sk-test-key")
        monkeypatch.setattr(client, "set_api_key", lambda k: activated.append(k))

        api_keys.activate_stored_keys()
        assert activated == ["sk-test-key"]

    def test_nothing_activated_when_no_key_is_stored(self, monkeypatch):
        import gui_qt.api_keys as api_keys
        import providers.openai.client as client

        activated: list[str] = []
        monkeypatch.setattr(api_keys, "get_stored_api_key", lambda p: None)
        monkeypatch.setattr(client, "set_api_key", lambda k: activated.append(k))

        api_keys.activate_stored_keys()
        assert activated == []


def _panel(monkeypatch):
    """A control panel with nothing that reaches the disk or the speakers.

    The overlay is suppressed rather than faked: the default hide policy opens
    one even while stopped, and these tests care about the panel's own windows.
    """
    import gui_qt.control_panel as cp

    monkeypatch.setattr(cp, "save_settings", lambda s: None)
    monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
    monkeypatch.setattr(cp.ControlPanel, "_ensure_subtitle_window", lambda self: None)

    class FakeController:
        pass

    return cp.ControlPanel(FakeController())


class TestSecondaryWindowSizing:
    """Settings, batch and announcement are one window at three sizes of
    content — they used to open 560, 480 and 520 wide under three separate
    height caps, which read as three unrelated dialogs."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.announce_window as aw
        import gui_qt.settings_window as sw

        # The batch window is absent: it persists nothing of its own.
        for module in (aw, sw):
            monkeypatch.setattr(module, "save_settings", lambda s: None)
        p = _panel(monkeypatch)
        p.resize(1200, 900)
        yield p
        p.close_secondary_windows()
        p.close()

    def test_the_three_windows_share_one_width(self, panel, qt_app):
        from gui_qt.window_size import SECONDARY_WINDOW_W

        panel.open_settings()
        panel.open_batch()
        panel.open_announce()
        _settle(qt_app)
        widths = {
            name: getattr(panel, name).width()
            for name in ("_settings_window", "_batch_window", "_announce_window")
        }
        assert set(widths.values()) == {SECONDARY_WINDOW_W}, widths

    def test_none_of_them_grows_past_the_shared_cap(self, panel, qt_app):
        from gui_qt.window_size import SECONDARY_MAX_H

        panel.open_settings()
        panel.open_batch()
        panel.open_announce()
        _settle(qt_app)
        for name in ("_settings_window", "_batch_window", "_announce_window"):
            assert getattr(panel, name).height() <= SECONDARY_MAX_H, name

    def test_the_collapsed_batch_window_still_fits_under_the_cap(self, panel, qt_app):
        # Why the cap is 760 and not the announcement window's old 700: the
        # batch cards come to ~720 with More settings closed, so a tighter cap
        # opened it already scrolled and left the expander unable to grow the
        # window at all — it could only ever add a scrollbar.
        from gui_qt.window_size import SECONDARY_MAX_H

        panel.open_batch()
        _settle(qt_app)
        w = panel._batch_window
        assert w._natural_height() <= SECONDARY_MAX_H
        assert w.height() == w._natural_height()
        before = w.height()
        w.more.set_expanded(True)
        _settle(qt_app)
        assert w.height() > before

    def test_a_host_with_less_room_shrinks_the_window(self, qt_app):
        # The rule an in-app panel goes through: the host declares the room it
        # has and the window re-measures itself against it.
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QWidget

        from gui_qt.window_size import SECONDARY_WINDOW_W, content_size

        w = QWidget()
        assert content_size(w, 400) == QSize(SECONDARY_WINDOW_W, 400)
        w.host_max_size = QSize(360, 300)
        assert content_size(w, 400) == QSize(360, 300)
        w.deleteLater()


class TestSettingsWindowKeys:
    """The API-key card. Remove used to act on providers that had no key."""

    @pytest.fixture
    def settings_win(self, qt_app, monkeypatch):
        import gui_qt.settings_window as sw

        monkeypatch.setattr(sw, "save_settings", lambda s: None)
        p = _panel(monkeypatch)
        p.open_settings()
        yield p._settings_window, sw
        p.close_secondary_windows()
        p.close()

    @staticmethod
    def _select(win, provider: str) -> None:
        win.key_provider_combo.setCurrentIndex(
            win.key_provider_combo.findData(provider)
        )

    def test_remove_is_dead_for_a_provider_with_no_key(self, settings_win, monkeypatch):
        # Regression: Remove was enabled for any chosen provider, so picking one
        # that had never held a key still asked for confirmation and then
        # reported "API key removed."
        win, sw = settings_win
        monkeypatch.setattr(sw, "has_usable_key", lambda p: False)
        self._select(win, "anthropic")
        assert not win.remove_key_btn.isEnabled()
        assert win.change_key_btn.isEnabled()  # you can still ADD one

    def test_remove_is_live_once_a_key_exists(self, settings_win, monkeypatch):
        win, sw = settings_win
        monkeypatch.setattr(sw, "has_usable_key", lambda p: True)
        self._select(win, "anthropic")
        assert win.remove_key_btn.isEnabled()

    def test_both_are_dead_while_no_provider_is_chosen(self, settings_win):
        win, _ = settings_win
        assert win.key_provider_combo.currentIndex() == 0
        assert not win.change_key_btn.isEnabled()
        assert not win.remove_key_btn.isEnabled()
        assert win.key_status.text() == "—"

    def test_a_key_removed_elsewhere_is_not_removed_again(
        self, settings_win, monkeypatch
    ):
        # The keychain can change under an open window (another MinbarLive, or
        # the OS credential manager), so the handler re-checks rather than
        # trusting the button state it was drawn with.
        win, sw = settings_win
        monkeypatch.setattr(sw, "has_usable_key", lambda p: True)
        self._select(win, "anthropic")

        cleared: list[str] = []
        monkeypatch.setattr(sw, "clear_api_key", lambda p: cleared.append(p))
        monkeypatch.setattr(sw, "ask_yes_no", lambda *a, **k: True)
        monkeypatch.setattr(sw, "show_message", lambda *a, **k: None)
        monkeypatch.setattr(sw, "has_usable_key", lambda p: False)  # vanished

        win._on_remove_key()
        assert cleared == []
        assert not win.remove_key_btn.isEnabled()

    def test_a_session_only_key_counts_as_saved(self, settings_win, monkeypatch):
        # has_usable_key, not get_stored_api_key: with no keychain a key entered
        # this session is live but unstored, and "no key saved" made the
        # operator re-enter a key that was already working.
        win, sw = settings_win
        monkeypatch.setattr(sw, "has_usable_key", lambda p: True)
        self._select(win, "openai")
        assert win.key_status.text() != "—"
        assert win.remove_key_btn.isEnabled()


class TestIntegratedWindows:
    """Secondary windows presented inside the control panel (window_style)."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.announce_window as aw
        import gui_qt.settings_window as sw

        for module in (aw, sw):
            monkeypatch.setattr(module, "save_settings", lambda s: None)
        p = _panel(monkeypatch)
        p.settings.window_style = "integrated"
        p.resize(1200, 900)
        p.show()
        _settle(qt_app)
        yield p
        p.close_secondary_windows()
        p.close()

    def test_the_size_rule_clamps_to_the_control_panel(self):
        from PySide6.QtCore import QSize

        from gui_qt.modal_host import MIN_PANEL_H, MIN_PANEL_W, clamped_panel_size

        # Room to spare: the panel gets the size it asks for.
        assert clamped_panel_size(QSize(520, 700), QSize(1400, 1000)) == QSize(520, 700)
        # A small control panel shrinks it, leaving a dim margin.
        assert clamped_panel_size(QSize(520, 700), QSize(600, 500)) == QSize(520, 450)
        # …but never below the point where nothing inside is usable.
        assert clamped_panel_size(QSize(520, 700), QSize(100, 100)) == QSize(
            MIN_PANEL_W, MIN_PANEL_H
        )

    def test_a_presented_window_is_a_child_not_a_window(self, panel, qt_app):
        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        assert not win.isWindow()
        assert win.parentWidget() is panel
        assert panel.modal_host.is_presented(win)
        assert panel.modal_host._backdrop.isVisible()

    def test_it_is_centred_over_the_control_panel(self, panel, qt_app):
        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        assert win.geometry().center().x() == pytest.approx(panel.width() // 2, abs=1)
        assert win.geometry().center().y() == pytest.approx(panel.height() // 2, abs=1)

    def test_the_panel_rule_actually_reaches_the_widget(self, panel, qt_app):
        from PySide6.QtGui import QPalette

        from gui_qt.theme import apply_theme

        # The panel is only styled if there IS a sheet: nothing applies one on
        # the way to a ControlPanel, only gui_qt/app.py does.
        apply_theme(qt_app, "dark")
        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        # Qt does not re-evaluate the stylesheet when an objectName changes, so
        # without the explicit re-polish in present() the #modal_panel rule is
        # never applied and the panel keeps an opaque, square background — which
        # is what hid its border and radius for the whole of the migration.
        assert win.objectName() == "modal_panel"
        assert win.palette().color(QPalette.Window).alpha() == 0

    def test_a_panel_gets_a_rounded_surface_behind_it(self, panel, qt_app):
        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        surface = panel.modal_host._panels[-1].surface
        # The rounding lives on this frame, not on the dialog: a QDialog takes
        # its background through the palette, which ignores border-radius.
        assert surface is not None
        assert surface.objectName() == "panel_surface"
        assert surface.geometry() == win.geometry()
        children = panel.children()
        assert children.index(surface) < children.index(win)

    def test_the_surface_follows_the_panel(self, panel, qt_app):
        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        surface = panel.modal_host._panels[-1].surface
        panel.resize(900, 700)  # re-centres and may re-measure the panel
        _settle(qt_app)
        assert surface.geometry() == win.geometry()

    def test_closing_a_panel_takes_its_surface_with_it(self, panel, qt_app):
        from PySide6.QtWidgets import QFrame

        panel.open_settings()
        _settle(qt_app)
        host = panel.modal_host
        assert panel.findChildren(QFrame, "panel_surface")
        panel._settings_window.close()
        _settle(qt_app)
        assert not host.active
        # Left behind, it would sit on the app as a blank rounded rectangle.
        assert not panel.findChildren(QFrame, "panel_surface")

    def test_escape_closes_the_panel(self, panel, qt_app):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent

        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        win.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        _settle(qt_app)
        assert not win.isVisible()
        assert not panel.modal_host.active
        assert not panel.modal_host._backdrop.isVisible()

    def test_a_click_on_the_dim_closes_the_topmost_panel_only(self, panel, qt_app):
        panel.open_settings()
        panel.open_batch()
        _settle(qt_app)
        host = panel.modal_host
        assert [p.win for p in host._panels] == [
            panel._settings_window,
            panel._batch_window,
        ]
        host._on_backdrop_click()
        _settle(qt_app)
        # The batch panel goes; the settings panel underneath stays, and the
        # dim stays with it rather than following the window that closed.
        assert not panel._batch_window.isVisible()
        assert panel._settings_window.isVisible()
        assert host._backdrop.isVisible()
        host._on_backdrop_click()
        _settle(qt_app)
        assert not host.active
        assert not host._backdrop.isVisible()

    def test_panels_follow_the_control_panel_being_resized(self, panel, qt_app):
        panel.open_settings()
        _settle(qt_app)
        win = panel._settings_window
        panel.resize(620, 520)
        _settle(qt_app)
        assert win.height() <= int(520 * 0.9)
        assert win.geometry().center().y() == pytest.approx(panel.height() // 2, abs=1)
        panel.resize(1200, 900)
        _settle(qt_app)
        assert win.geometry().center().y() == pytest.approx(panel.height() // 2, abs=1)

    def test_a_timed_announcement_is_not_stranded_by_a_teardown(self, panel, qt_app):
        # A window-style or GUI-language change DESTROYS the announcement
        # window, and the auto-clear timer went with it — a "30 seconds"
        # message then stayed on the overlay for good.
        panel.open_announce()
        _settle(qt_app)
        win = panel._announce_window
        win.text.setPlainText("Bitte Handys stummschalten")
        win.duration_combo.setCurrentIndex(1)  # a timed message, not "until stopped"
        win.send_announcement()
        assert panel.has_active_announcement()
        assert win._auto_clear.isActive()

        panel.close_secondary_windows()
        _settle(qt_app)
        assert not panel.has_active_announcement()

    def test_an_until_stopped_announcement_survives_a_teardown(self, panel, qt_app):
        # It owns no timer, so nothing is lost by destroying the window — and
        # the message is meant to stay up until someone stops it.
        from config import ANNOUNCEMENT_DURATIONS_SECONDS

        panel.open_announce()
        _settle(qt_app)
        win = panel._announce_window
        win.text.setPlainText("Freitagsgebet 13:30")
        win.duration_combo.setCurrentIndex(ANNOUNCEMENT_DURATIONS_SECONDS.index(0))
        win.send_announcement()
        assert not win._auto_clear.isActive()

        panel.close_secondary_windows()
        _settle(qt_app)
        assert panel.has_active_announcement()

    def test_switching_style_saves_it_and_rebuilds_the_windows(
        self, qt_app, monkeypatch
    ):
        import gui_qt.settings_window as sw
        from gui_qt.settings_window import _STYLE_SEGMENTS

        monkeypatch.setattr(sw, "save_settings", lambda s: None)
        p = _panel(monkeypatch)
        p.settings.window_style = "integrated"
        p.open_settings()
        win = p._settings_window
        # Looked up rather than written as a literal: the segment ORDER is a
        # presentation choice that has already been flipped once, and a
        # hard-coded index turns that into a test failure instead of a fact.
        assert win.window_style_segment.current_index() == _STYLE_SEGMENTS.index(
            "integrated"
        )

        reopened: list[bool] = []
        monkeypatch.setattr(
            p, "reopen_secondary_windows", lambda: reopened.append(True)
        )
        win.window_style_segment._buttons[
            _STYLE_SEGMENTS.index("windowed")
        ].click()
        assert p.settings.window_style == "windowed"
        assert not p.uses_integrated_windows()
        assert reopened == [True]  # a window cannot change style in place
        p.close_secondary_windows()
        p.close()

    def test_exiting_with_a_panel_open_is_quiet(self):
        # Regression: the host connected destroyed(), which only ever fires
        # during teardown — by then the control panel has taken the backdrop
        # with it, and every exit with a panel open printed
        # "RuntimeError: Internal C++ object (_Backdrop) already deleted"
        # out of a Qt slot.
        import os
        import pathlib
        import subprocess

        code = (
            "import gui_qt.control_panel as cp\n"
            "from PySide6.QtWidgets import QApplication\n"
            "cp.save_settings = lambda s: None\n"
            "cp.activate_stored_keys = lambda: None\n"
            "cp.ControlPanel._ensure_subtitle_window = lambda self: None\n"
            "app = QApplication([])\n"
            "p = cp.ControlPanel(type('C', (), {})())\n"
            "p.settings.window_style = 'integrated'\n"
            "p.resize(1000, 800); p.show()\n"
            "for _ in range(8): app.processEvents()\n"
            "p.open_settings()\n"
            "for _ in range(8): app.processEvents()\n"
            "p.close()\n"
        )
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=pathlib.Path(__file__).resolve().parents[1],
            env=env,
        )
        assert "Traceback" not in result.stderr, result.stderr
        assert result.returncode == 0, result.stderr


class TestSessionTracking:
    """The cost record and the inactivity guard.

    Neither existed in the Qt tree until s30: the Costs tab stayed empty for
    every ``--qt`` session, and a default-on "stop when idle" checkbox did
    nothing at all.
    """

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(cp, "ensure_keys", lambda *a, **k: True)
        # The overlay is a real top-level window; nothing here needs one.
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )
        # Every dialog reachable from here is an error report, and an
        # unattended modal would hang the run.
        monkeypatch.setattr(cp, "show_message", lambda *a, **k: None)

        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(
            cp, "begin_cost_session", lambda: calls.append(("begin", ""))
        )
        monkeypatch.setattr(
            cp, "cancel_cost_session", lambda: calls.append(("cancel", ""))
        )
        monkeypatch.setattr(
            cp,
            "end_cost_session",
            lambda status="completed": calls.append(("end", status)),
        )
        monkeypatch.setattr(
            cp, "flush_cost_history", lambda: calls.append(("flush", ""))
        )

        class FakeController:
            def __init__(self):
                self.idle = 0.0
                self.stops = 0
                # The bridge drains both the moment a start succeeds.
                self.translation_queue = queue.Queue()
                self.error_queue = queue.Queue()

            def start(self, input_device=None):
                pass

            def stop(self):
                self.stops += 1

            def get_live_transcript(self):
                return "", False

            def seconds_since_last_activity(self):
                return self.idle

        controller = FakeController()
        p = cp.ControlPanel(controller)
        yield p, controller, calls
        p.close()

    @staticmethod
    def _mark_started(p) -> None:
        """Apply a successful start without running the worker thread."""
        p._start_error = None
        p._starting = True
        p._finish_start()

    # -- cost record ------------------------------------------------------
    def test_start_opens_a_cost_session(self, panel):
        p, _controller, calls = panel
        p.on_start()
        assert ("begin", "") in calls

    def test_failed_start_drops_the_cost_session(self, panel):
        p, _controller, calls = panel
        # No usage was billed for a start that never ran, so the session is
        # cancelled rather than written out as a zero-cost record.
        p._start_error = RuntimeError("no")
        p._starting = True
        p._finish_start()
        assert ("cancel", "") in calls
        assert not any(kind == "end" for kind, _ in calls)

    def test_successful_start_runs_the_session_timers(self, panel):
        p, _controller, _calls = panel
        self._mark_started(p)
        assert p._running
        assert p._inactivity_timer.isActive()
        assert p._cost_flush_timer.isActive()

    def test_stop_closes_the_cost_session_and_the_timers(self, panel):
        p, _controller, calls = panel
        self._mark_started(p)
        p._stop_error = None
        p._stopping = True
        p._finish_stop()
        assert ("end", "completed") in calls
        assert not p._inactivity_timer.isActive()
        assert not p._cost_flush_timer.isActive()

    def test_failed_stop_keeps_the_session_open(self, panel):
        p, _controller, calls = panel
        self._mark_started(p)
        p._stop_error = RuntimeError("stuck")
        p._stopping = True
        p._finish_stop()
        # A stop that failed has completed nothing, and the pipeline may well
        # still be up: the panel stays running so Stop can be retried, and the
        # cost record stays open.
        assert p._running
        assert not any(kind == "end" for kind, _ in calls)

    def test_close_writes_the_record(self, panel):
        p, _controller, calls = panel
        self._mark_started(p)
        p.close()
        assert ("end", "closed") in calls

    def test_flush_only_while_running(self, panel):
        p, _controller, calls = panel
        p._flush_cost_session()
        assert not any(kind == "flush" for kind, _ in calls)
        p._running = True
        p._flush_cost_session()
        assert ("flush", "") in calls

    # -- inactivity guard -------------------------------------------------
    def test_idle_session_is_stopped(self, panel, monkeypatch):
        import gui_qt.control_panel as cp

        p, controller, _calls = panel
        p._running = True
        p.settings.auto_stop_inactivity = True
        controller.idle = cp.AUTO_STOP_INACTIVITY_SECONDS + 1
        stopped: list[bool] = []
        monkeypatch.setattr(type(p), "on_stop", lambda self: stopped.append(True))
        p._check_inactivity()
        assert stopped == [True]

    def test_busy_session_is_left_alone(self, panel, monkeypatch):
        import gui_qt.control_panel as cp

        p, controller, _calls = panel
        p._running = True
        p.settings.auto_stop_inactivity = True
        controller.idle = cp.AUTO_STOP_INACTIVITY_SECONDS - 1
        stopped: list[bool] = []
        monkeypatch.setattr(type(p), "on_stop", lambda self: stopped.append(True))
        p._check_inactivity()
        assert stopped == []

    def test_the_checkbox_actually_disables_it(self, panel, monkeypatch):
        import gui_qt.control_panel as cp

        p, controller, _calls = panel
        p._running = True
        p.settings.auto_stop_inactivity = False
        controller.idle = cp.AUTO_STOP_INACTIVITY_SECONDS * 10
        stopped: list[bool] = []
        monkeypatch.setattr(type(p), "on_stop", lambda self: stopped.append(True))
        p._check_inactivity()
        assert stopped == []

    def test_a_controller_that_raises_does_not_stop_the_session(
        self, panel, monkeypatch
    ):
        p, controller, _calls = panel
        p._running = True
        p.settings.auto_stop_inactivity = True

        def _boom():
            raise RuntimeError("no timestamp")

        controller.seconds_since_last_activity = _boom
        stopped: list[bool] = []
        monkeypatch.setattr(type(p), "on_stop", lambda self: stopped.append(True))
        p._check_inactivity()
        assert stopped == []


class TestAutoStartOnLaunch:
    """"Start on launch" persisted its checkbox and then did nothing."""

    @staticmethod
    def _build(qt_app, monkeypatch, *, auto_start: bool):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        # The wait exists so the window can paint first; no reason to spend it.
        monkeypatch.setattr(cp, "_AUTO_START_DELAY_MS", 0)
        started: list[bool] = []
        monkeypatch.setattr(
            cp.ControlPanel, "on_start", lambda self: started.append(True)
        )

        from utils.settings import load_settings

        settings = load_settings()
        previous, settings.auto_start = settings.auto_start, auto_start
        try:
            p = cp.ControlPanel(type("C", (), {})())
            for _ in range(6):
                qt_app.processEvents()
            p.close()
        finally:
            settings.auto_start = previous
        return started

    def test_on(self, qt_app, monkeypatch):
        assert self._build(qt_app, monkeypatch, auto_start=True) == [True]

    def test_off(self, qt_app, monkeypatch):
        assert self._build(qt_app, monkeypatch, auto_start=False) == []


class TestLiveStreamRestart:
    """A streaming socket fixes the source language and the transcription model
    at connect. Changing either mid-session used to save the setting and leave
    the stream on the old one, so the control confirmed a change that had not
    landed."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui_qt.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(cp, "ensure_keys", lambda *a, **k: True)
        monkeypatch.setattr(cp, "show_message", lambda *a, **k: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            def __init__(self):
                self.restarts: list[object] = []
                self.done = threading.Event()
                self.fail = False

            def restart(self, input_device=None):
                self.restarts.append(input_device)
                self.done.set()
                if self.fail:
                    raise RuntimeError("socket refused")

        controller = FakeController()
        p = cp.ControlPanel(controller)
        yield p, controller
        p.close()

    @staticmethod
    def _drive(qt_app, controller, p, timeout: float = 5.0) -> None:
        """Let the restart worker finish and its outcome be applied."""
        controller.done.wait(timeout)
        tick = threading.Event()
        for _ in range(500):
            qt_app.processEvents()
            if not p._starting:
                return
            tick.wait(0.01)

    def test_streaming_session_reconnects(self, panel, qt_app):
        p, controller = panel
        p._running = True
        p.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        p._restart_pipeline_for_live_change()
        self._drive(qt_app, controller, p)
        assert controller.restarts
        assert p._running

    def test_segmented_session_does_not(self, panel):
        p, controller = panel
        p._running = True
        p.settings.pipeline_mode = "segmented"
        p._restart_pipeline_for_live_change()
        assert controller.restarts == []
        assert not p._starting

    def test_stopped_panel_does_not(self, panel):
        p, controller = panel
        p._running = False
        p.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        p._restart_pipeline_for_live_change()
        assert controller.restarts == []

    def test_stop_is_inert_while_reconnecting(self, panel):
        p, _controller = panel
        p._running, p._starting = True, True
        p._sync_running_state()
        # Stopping halfway through a reopen races the restart worker.
        assert not p.stop_btn.isEnabled()
        assert not p.start_btn.isEnabled()

    def test_a_failed_reconnect_reports_the_session_gone(self, panel, qt_app):
        p, controller = panel
        p._running = True
        controller.fail = True
        p.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        p._restart_pipeline_for_live_change()
        self._drive(qt_app, controller, p)
        # restart() can fail after its own stop() already ran, so the pipeline
        # really is down; leaving the panel "running" would strand the operator.
        assert not p._running
        assert not p._starting


class TestHistoryNarrowLayout:
    """The viewer has to survive being made narrow.

    Side by side it needs ~765 px. As a separate window the WM held it there,
    but as an in-app panel it is resized as a child widget, which bypasses that
    minimum — inside a control panel under ~620 px wide, Copy and Save… were
    laid out past its right edge and could not be clicked at all. The Tk viewer
    reflows here instead (gui/history_view.py _layout_history_responsive).
    """

    @pytest.fixture
    def history(self, qt_app, monkeypatch):
        import gui_qt.history_window as hw

        monkeypatch.setattr(hw, "list_history_sessions", list)
        window = hw.HistoryWindow(lambda key, fallback="": fallback)
        window.show()
        _settle(qt_app)
        yield window
        window.close()

    @staticmethod
    def _buttons(window):
        return (
            window.summarise_btn,
            window.delete_btn,
            window.copy_btn,
            window.export_btn,
        )

    def test_the_breakpoint_is_measured_not_assumed(self, history):
        # It is the sum of the list, the margins and four TRANSLATED labels, so
        # a hard-coded number would be wrong in some GUI language.
        assert history._wide_min_w > 0
        assert history.minimumWidth() < history._wide_min_w

    def test_it_stays_side_by_side_while_there_is_room(self, history, qt_app):
        from PySide6.QtCore import Qt

        history.resize(history._wide_min_w + 120, 560)
        _settle(qt_app)
        assert history.splitter.orientation() == Qt.Horizontal
        # One row: Summarise, a stretch, and the three secondary actions.
        assert history._action_bottom.count() == 0

    def test_narrow_stacks_the_panes_and_wraps_the_actions(self, history, qt_app):
        from PySide6.QtCore import Qt

        history.resize(history._wide_min_w - 60, 560)
        _settle(qt_app)
        assert history.splitter.orientation() == Qt.Vertical
        # Summarise alone above; Delete / Copy / Save… sharing the row below.
        assert history._action_top.count() == 1
        assert history._action_bottom.count() == 3

    def test_it_can_be_made_narrower_than_the_wide_layout_needs(self, history, qt_app):
        # The regression this guards: with the window's floor left at the wide
        # arrangement's minimum, it could never be dragged narrow enough to
        # REACH the mode that lowers it.
        history.resize(400, 420)
        _settle(qt_app)
        assert history.width() == 400

    def test_every_action_stays_inside_the_window(self, history, qt_app):
        for width in (900, 760, 620, 500, 420):
            history.resize(width, 460)
            _settle(qt_app)
            for button in self._buttons(history):
                corner = button.mapTo(history, button.rect().bottomRight())
                assert corner.x() <= history.width(), (
                    f"{button.text()!r} is {corner.x() - history.width()}px past "
                    f"the right edge at {width}px wide"
                )
                assert corner.y() <= history.height()

    def test_an_in_app_panel_keeps_its_actions_reachable(self, qt_app, monkeypatch):
        import gui_qt.history_window as hw

        monkeypatch.setattr(hw, "list_history_sessions", list)
        panel = _panel(monkeypatch)
        panel.settings.window_style = "integrated"
        # The control panel's own minimum — the worst case the host can hand a
        # panel, and where Save… used to sit 50px beyond the edge.
        panel.resize(420, 420)
        panel.show()
        _settle(qt_app)
        try:
            panel.open_history()
            _settle(qt_app)
            window = panel._history_window
            for button in self._buttons(window):
                corner = button.mapTo(panel, button.rect().bottomRight())
                assert corner.x() <= panel.width()
        finally:
            panel.close_secondary_windows()
            panel.close()


class TestBatchRunLock:
    """Everything that decides what a run produces is frozen while one runs.

    The worker was handed its arguments at Start, so a change made mid-run
    cannot reach it — it would only leave the window describing a job it is not
    running. The Tk window locks the same set (gui/batch_view.py
    _batch_option_combos).
    """

    @pytest.fixture
    def batch(self, qt_app):
        import gui_qt.batch_window as bw
        from utils.settings import load_settings

        # No save_settings stub: the batch window is configured independently
        # of the live app and writes nothing back (see its module docstring).
        window = bw.BatchWindow(lambda key, fallback="": fallback, load_settings())
        window.show()
        _settle(qt_app)
        yield window
        window.worker.is_running = lambda: False
        window.close()

    @staticmethod
    def _run(window, running: bool) -> None:
        window.worker.is_running = lambda: running
        window._sync_file_row()

    def test_the_config_controls_are_live_before_a_run(self, batch):
        assert all(control.isEnabled() for control in batch._config_controls())

    def test_a_run_freezes_them(self, batch):
        batch._input_path = "lecture.mp3"
        self._run(batch, True)
        assert not any(control.isEnabled() for control in batch._config_controls())

    def test_finishing_gives_them_back(self, batch):
        batch._input_path = "lecture.mp3"
        self._run(batch, True)
        self._run(batch, False)
        assert all(control.isEnabled() for control in batch._config_controls())

    def test_the_transcript_only_rule_survives_the_thaw(self, batch):
        # A blanket re-enable would hand back the bilingual segment for an
        # output format that writes no SRT to be bilingual about.
        batch.output_segment.set_current_index(1)  # transcript only
        batch._sync_bilingual_state()
        assert not batch.bilingual_segment.isEnabled()
        batch._input_path = "lecture.mp3"
        self._run(batch, True)
        self._run(batch, False)
        assert not batch.bilingual_segment.isEnabled()

    def test_a_frozen_bilingual_segment_still_reports_the_running_job(self, batch):
        # _bilingual_srt used to read isEnabled(), which the run lock clears —
        # it would then report "translation only" for a bilingual run.
        batch.output_segment.set_current_index(0)  # subtitles
        batch.bilingual_segment.set_current_index(1)  # original + translation
        assert batch._bilingual_srt()
        batch._input_path = "lecture.mp3"
        self._run(batch, True)
        assert batch._bilingual_srt()


class TestBatchFilePicker:
    """The picker's filter is the only thing deciding what a user can SEE:
    batch/processor.py has no allowlist, it hands everything to ffmpeg. A
    format missing from the filter looks unsupported when it is not, and
    "switch to All files" is not something an AV volunteer knows to do."""

    def test_it_offers_every_format_the_tk_picker_did(self):
        from gui_qt.batch_window import _MEDIA_EXTENSIONS

        tk_offered = {
            "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "mp4", "mkv",
            "mov", "webm", "avi", "m4v", "wmv", "flv", "ts", "mpg", "mpeg",
        }
        assert tk_offered <= set(_MEDIA_EXTENSIONS)

    def test_the_patterns_are_wildcards_qt_understands(self):
        from gui_qt.batch_window import _MEDIA_EXTENSIONS

        # Built into "*.ext *.ext" — a stray dot or space silently filters out
        # everything, which reads as "no media files in this folder".
        assert all(
            extension.isalnum() and extension.islower()
            for extension in _MEDIA_EXTENSIONS
        )


class TestFontStacks:
    """Naming a family this machine does not have makes Qt build its alias
    table on the first layout and say so — macOS printed "Replace uses of
    missing font family "Segoe UI"" on every launch, because one Windows
    family was hardcoded for all three platforms."""

    def test_every_requested_family_exists(self, qt_app):
        from PySide6.QtGui import QFontDatabase

        from gui_qt.theme import app_families

        missing = [f for f in app_families() if not QFontDatabase.hasFamily(f)]
        assert not missing, f"requesting fonts this machine lacks: {missing}"

    def test_the_application_font_is_the_platform_stack(self, qt_app):
        from gui_qt.theme import app_families, apply_theme

        apply_theme(qt_app, "light")
        assert qt_app.font().families() == app_families()

    def test_arabic_is_measured_with_a_family_that_has_arabic(self, qt_app):
        from gui_qt.fonts import arabic_families, source_font, subtitle_font

        arabic = "بل لا يشعرون"
        assert subtitle_font(40, text=arabic).families() == arabic_families()
        assert source_font(40, arabic).families() == arabic_families()

    def test_latin_keeps_the_interface_stack(self, qt_app):
        from gui_qt.fonts import source_font, subtitle_font, ui_families

        german = "Vielmehr merken sie es nicht."
        assert subtitle_font(40, text=german).families() == ui_families()
        assert source_font(40, german).families() == ui_families()

    def test_an_honorific_does_not_make_a_german_line_arabic(self, qt_app):
        # ﷺ/ﷻ are Arabic-block code points the translator inserts into
        # otherwise-Latin lines; classing those as Arabic would restyle them.
        from gui_qt.fonts import subtitle_font, ui_families

        assert subtitle_font(40, text="dass Allah ﷻ es sagt.").families() == (
            ui_families()
        )


class TestInkOverhang:
    """Qt reports the metrics of the family it was ASKED for and paints missing
    glyphs from whichever family has them. Arabic drawn in a Latin-only family
    therefore measures against a descent its glyphs go straight through, and
    the original overlapped its translation (seen on Linux, where the UI family
    has no Arabic at all)."""

    def test_a_blocks_height_covers_the_ink_it_draws(self, overlay):
        from PySide6.QtGui import QFontMetrics

        from gui_qt.fonts import subtitle_font

        w = overlay(SUBTITLE_MODE_STATIC)
        for text in ("بل لا يشعرون أنه فتنة.", "Vielmehr merken sie es nicht."):
            font = subtitle_font(w._translation_px(), text=text)
            _layout, height = w._layout_text(text, font)
            fm = QFontMetrics(font)
            ink_bottom = fm.ascent() + fm.tightBoundingRect(text).bottom()
            assert height >= ink_bottom, f"{text!r} draws below its own box"

    def test_the_measured_overhang_reaches_the_block_height(self, overlay, monkeypatch):
        # The guard only engages where the metrics under-report, which no
        # Windows font does — so the wiring is pinned directly.
        w = overlay(SUBTITLE_MODE_STATIC)
        from gui_qt.fonts import subtitle_font
        from gui_qt.subtitle_window import SubtitleWindow

        font = subtitle_font(40, text="Vielmehr merken sie es nicht.")
        monkeypatch.setattr(SubtitleWindow, "_ink", staticmethod(lambda t, f: (0, 0)))
        flat = w._layout_text("Vielmehr merken sie es nicht.", font)[1]
        monkeypatch.setattr(SubtitleWindow, "_ink", staticmethod(lambda t, f: (0, 12)))
        assert w._layout_text("Vielmehr merken sie es nicht.", font)[1] == flat + 12


class TestParagraphDirection:
    """A trailing full stop belongs at the END of the sentence, which for
    Arabic is its LEFT edge. QTextOption defaults its text direction to
    LEFT-TO-RIGHT rather than to "work it out", so every line was laid out as
    an LTR paragraph: the words still ran right-to-left (bidi does that inside
    the paragraph regardless), but the terminator — a neutral character —
    attached to the paragraph and landed on the right."""

    @staticmethod
    def _direction(w, text: str) -> str:
        from gui_qt.fonts import subtitle_font

        layout, _height = w._layout_text(text, subtitle_font(40, text=text))
        line = layout.lineAt(0)
        # Where the first logical character sits vs. where the cursor lands
        # after the last one. In an RTL paragraph the last one is further left.
        return "rtl" if line.cursorToX(len(text))[0] < line.cursorToX(0)[0] else "ltr"

    def test_an_arabic_sentence_is_an_rtl_paragraph(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC)
        assert self._direction(w, "وكل بدعة ضلالة.") == "rtl"

    def test_arabic_opening_on_a_neutral_is_still_rtl(self, overlay):
        # The live line elides with a leading ellipsis, and a quoted verse
        # opens on a quotation mark — neither is a strong character, so the
        # paragraph direction has to come from the first Arabic letter after.
        w = overlay(SUBTITLE_MODE_STATIC)
        assert self._direction(w, "…وكل بدعة ضلالة.") == "rtl"
        assert self._direction(w, '"وكل بدعة ضلالة."') == "rtl"

    def test_german_stays_ltr(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC)
        assert self._direction(w, "Und die Geschichte widerlegt das.") == "ltr"
        # Including with an honorific in the middle of it.
        assert self._direction(w, "dass Allah ﷻ es erzählt hat.") == "ltr"


class TestLiveLine:
    """The in-progress transcript. It was handed ``_font_size_base``, which is
    a DIVISOR and not a pixel size — so it drew at a size unrelated both to the
    subtitles around it and to the height the feed had reserved for it, which
    was already being measured at the translation size."""

    def test_it_draws_at_the_translation_size(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, font_size_base=60)
        w.set_live_text("Das ist der laufende Text", False)
        assert w._live_font().pixelSize() == w._translation_px()

    def test_the_reserved_height_matches_the_font_it_draws_with(self, overlay):
        from PySide6.QtGui import QFontMetrics

        from config import REALTIME_LIVE_MAX_ROWS

        w = overlay(SUBTITLE_MODE_REALTIME, font_size_base=60)
        w.set_live_text("هؤلاء لا يشعرون أنه فتنة.", False)
        expected = QFontMetrics(w._live_font()).height() * REALTIME_LIVE_MAX_ROWS
        assert w._live_line_height() == expected

    def test_arabic_renders_bold_and_upright_like_the_tk_overlay(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        w.set_live_text("هؤلاء لا يشعرون", False)
        arabic = w._live_font()
        assert arabic.bold() and not arabic.italic()
        w.set_live_text("Das ist der laufende Text", False)
        latin = w._live_font()
        assert latin.italic() and not latin.bold()


class TestFeedEndsAtTheFooter:
    """The feed is anchored to the bottom of the content area, so anything
    reserved below the last block holds the whole feed that far off the
    footer. It used to charge a full REALTIME_BLOCK_SPACING after the newest
    block for a follower that does not exist."""

    @staticmethod
    def _settle(w) -> None:
        w.render(w.grab())
        for _ in range(2000):
            if not w._feed_timer.isActive():
                break
            w._step_feed_anim()
        w.render(w.grab())

    def _newest_bottom(self, w) -> float:
        heights = [w._measure_block(b) for b in w._blocks]
        stacked = sum(
            h + w._block_gap(nxt)
            for h, nxt in zip(heights[:-1], w._blocks[1:], strict=True)
        )
        return int(w.height() * 0.06) - w._scroll_offset + stacked + heights[-1]

    def test_the_newest_block_reaches_the_bottom_of_the_content_area(self, overlay):
        from config import REALTIME_BLOCK_SPACING

        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True, show_footer=True)
        for i in range(16):
            w.add_subtitle(f"Zeile {i}: {PAIRS[1][0]}", source_text=PAIRS[1][1])
            self._settle(w)
        bottom = self._newest_bottom(w)
        # Both sides: it must not run under the pills, and it must not stop a
        # block's worth of empty space short of them either.
        assert bottom <= w._content_height() + 5
        assert bottom > w._content_height() - REALTIME_BLOCK_SPACING

    def test_the_live_line_takes_the_place_the_gap_reserved(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True, show_footer=True)
        for i in range(16):
            w.add_subtitle(f"Zeile {i}: {PAIRS[1][0]}", source_text=PAIRS[1][1])
            self._settle(w)
        without = self._newest_bottom(w)
        w.set_live_text("هؤلاء لا يشعرون أنه فتنة.", False)
        self._settle(w)
        # The blocks slide up by exactly the room the live line needs, so it
        # ends where the newest block was — the feed does not grow a second gap.
        assert self._newest_bottom(w) + w._live_gap() + w._live_line_height() == (
            pytest.approx(without, abs=2)
        )

    def test_the_pills_keep_their_clearance(self, overlay):
        from gui_qt.subtitle_window import FOOTER_MARGIN, PILL_CLEARANCE

        w = overlay(SUBTITLE_MODE_STATIC, show_footer=True)
        # The Tk overlay reserves the pill, 10 px under it and 8 px above it;
        # without that last part the feed's bottom line sits on the disclaimer.
        assert w.reserved_bottom() == (
            w._pill_height() + FOOTER_MARGIN + PILL_CLEARANCE
        )


class TestDropdownPopup:
    """macOS answers SH_ComboBox_UseNativePopup with 1, and Qt then opens an
    NSMenu: unthemed, positioned over the box rather than under it, and — since
    a menu-style popup ignores maxVisibleItems — as long as the list happens to
    be. A KDE or Adwaita desktop style can answer the same way."""

    def test_the_style_refuses_the_platform_popup(self, qt_app):
        from PySide6.QtWidgets import QStyle

        from gui_qt.theme import apply_theme

        apply_theme(qt_app, "light")
        style = qt_app.style()
        assert style.styleHint(QStyle.SH_ComboBox_Popup) == 0
        assert style.styleHint(QStyle.SH_ComboBox_UseNativePopup) == 0

    def test_the_popup_is_a_plain_item_view(self, qt_app):
        from PySide6.QtWidgets import QListView

        from gui_qt.theme import apply_theme
        from gui_qt.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch", "English"])
        try:
            # The stylesheet dresses `QComboBox QAbstractItemView`; a view the
            # platform style substitutes is not necessarily one of those.
            assert isinstance(combo.view(), QListView)
        finally:
            combo.close()

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

PAIRS = [
    ("Im Namen Allahs, des Allerbarmers, des Barmherzigen.", "بسم الله الرحمن الرحيم"),
    ("Alles Lob gebuehrt Allah ﷻ, dem Herrn der Welten.", "الحمد لله رب العالمين"),
    ("Gibt es einen Schoepfer ausser Allah?", "هل من خالق غير الله؟"),
]


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

    def test_feed_shifts_up_and_never_slides_back_down(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        offsets = []
        for i in range(16):
            w.add_subtitle(f"Zeile {i}: {PAIRS[1][0]}", source_text=PAIRS[1][1])
            w.render(w.grab())
            offsets.append(w._scroll_offset)
        assert offsets[-1] > 0, "feed never shifted despite overflowing"
        # Pairwise: the second sequence is one shorter by construction.
        assert all(b >= a for a, b in zip(offsets, offsets[1:], strict=False))

    def test_newest_block_stays_within_the_content_area(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        for i in range(16):
            w.add_subtitle(f"Zeile {i}: {PAIRS[1][0]}", source_text=PAIRS[1][1])
            w.render(w.grab())
        heights = [w._measure_block(b) for b in w._blocks]
        top = int(w.height() * 0.06)
        spacing = sum(h + 34 for h in heights[:-1])
        newest_y = top - w._scroll_offset + spacing
        assert newest_y + heights[-1] <= w._content_height() + 5

    def test_blocks_scrolled_off_the_top_are_evicted(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        for i in range(30):
            w.add_subtitle(f"Zeile {i}: {PAIRS[0][0]}", source_text=PAIRS[0][1])
            w.render(w.grab())
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
        p.resize(1200, 800)
        qt_app.processEvents()
        yield p
        p.close()

    def test_card_grid_reflows_with_the_window(self, panel, qt_app):
        # 1 / 2 / 3 columns, at the same thresholds the Tk panel uses.
        # A hidden widget never receives resizeEvent, so the reflow is driven
        # directly — that is exactly what the handler does, and showing a real
        # control-panel window during a test run is not worth the parity.
        for width, expected in ((1200, 3), (860, 2), (520, 1)):
            panel.resize(width, 800)
            panel._relayout_columns()
            assert panel._columns == expected, f"{width}px should give {expected}"

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
        assert "2026-07-30" in w.entry_list.item(0).text()

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

        # Stub the module the worker imports lazily, so no ffmpeg or API is hit.
        monkeypatch.setitem(
            sys.modules,
            "batch.processor",
            types.SimpleNamespace(process_file=fake_process_file),
        )
        w = bw.BatchWindow(lambda k, f="": f, load_settings())
        yield w, calls
        w.close()

    def test_start_is_disabled_until_a_file_is_chosen(self, batch):
        w, _ = batch
        assert not w.start_btn.isEnabled()

    def test_output_format_maps_from_the_segment(self, batch):
        w, _ = batch
        for index, expected in enumerate(("srt", "txt", "both")):
            w.output_combo.setCurrentIndex(index)
            assert w._output_format() == expected

    def test_options_reach_the_processor(self, batch, qt_app):
        w, calls = batch
        w._input_path = "khutbah.mp3"
        w.output_combo.setCurrentIndex(2)
        w.bilingual_check.setChecked(True)
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
            types.SimpleNamespace(process_file=blocking_process_file),
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
        monkeypatch.setattr(aw.QMessageBox, "information", lambda *a, **k: None)
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

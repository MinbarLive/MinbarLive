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

import pytest

pytest.importorskip("PySide6", reason="Qt GUI tree is optional during migration")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui_qt.subtitle_window import SubtitleWindow  # noqa: E402
from utils.settings import (  # noqa: E402
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

"""Regression tests for the Qt GUI (issue #44).

Every test here locks in a defect that reached a real run. They are written
against behaviour, not pixels — the layout assertions check where blocks are
placed, which is what actually broke.

The module skips if PySide6 is missing, and the control-panel half imports
lazily (``cp_module()``) so the file still collects without a display.

Note on headless runs: ``QT_QPA_PLATFORM=offscreen`` is not a substitute for a
real display. Four tests fail there and pass on the real platform, and on
Windows that plugin loads NO system fonts and renders every glyph as tofu —
geometry still measures, so these pass, but anything asserting on rendered text
appearance would not.
"""

from __future__ import annotations

import queue
import sys
import threading
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="PySide6 not installed")

import shiboken6  # noqa: E402
from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gui.subtitle_window import SubtitleWindow  # noqa: E402
from utils.settings import (  # noqa: E402
    PIPELINE_MODE_STREAMING,
    STATIC_LIFT_PERCENT_MAX,
    STATIC_LIFT_PERCENT_MIN,
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    SUBTITLE_MODES,
    WINDOW_HEIGHT_PERCENT_MAX,
    WINDOW_HEIGHT_PERCENT_MIN,
)


def cp_module():
    """The control-panel module, imported lazily so this file still collects
    without a display."""
    import gui.control_panel as cp

    return cp


def card_grid_module():
    """The card-grid module, for the width thresholds it owns."""
    import gui.card_grid as card_grid

    return card_grid


PAIRS = [
    ("Im Namen Allahs, des Allerbarmers, des Barmherzigen.", "بسم الله الرحمن الرحيم"),
    ("Alles Lob gebuehrt Allah ﷻ, dem Herrn der Welten.", "الحمد لله رب العالمين"),
    ("Gibt es einen Schoepfer ausser Allah?", "هل من خالق غير الله؟"),
]

# Long enough to WRAP at the overlay's width, and with a short trailing line on
# the source — which is the shape that exposed both static-mode bugs: the block
# outgrew a lowered window, and the translation's backdrop landed on the
# source's last line.
_LONG_DE = (
    "Alles Lob gebuehrt Allah, dem Herrn der Welten, und der Segen und Friede "
    "seien auf dem Gesandten Allahs, seiner Familie und all seinen Gefaehrten, "
    "und auf denen, die ihnen in Rechtschaffenheit folgen bis zum Tage des "
    "Gerichts, und wir bitten Allah um Aufrichtigkeit im Wort und in der Tat."
)
_LONG_AR = (
    "الحمد لله رب العالمين والصلاة والسلام على رسول الله وعلى آله وصحبه "
    "أجمعين ومن تبعهم بإحسان إلى يوم الدين ونسأل الله الإخلاص في القول والعمل"
)


def _long_block(translation: str = _LONG_DE, source: str | None = _LONG_AR):
    from gui.subtitle_window import Block

    return Block(translation, source)


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


@pytest.fixture(autouse=True)
def destroy_leftover_widgets(qt_app):
    """Destroy every widget a test leaves behind (issue #52).

    ``close()`` only hides a widget, and dropping the last Python reference
    does not free it either — a panel's own signal connections hold it
    through a cycle ``gc.collect()`` does not break — so the suite kept every
    widget it ever built: 223 per control-panel test, ~4900 alive by the time
    the chrome tests ran. That made it quadratic, because
    ``QApplication.setStyleSheet`` re-polishes every live widget: each
    ``apply_theme`` paid for everything the earlier tests had left behind,
    and the twelve ``TestControlChrome`` tests went from 0.98 s as a class on
    their own to 7 s *each*. (``apply_theme``'s own ``allWidgets()`` loop is
    not the cost — 3 ms against setStyleSheet's 1851 ms at 1118 widgets.)

    Autouse rather than 64 fixture teardowns, because most of the slow tests
    build their widgets inline with no fixture to hook.

    Two things this has to get right, both of which cost a crash to learn:

    * ``deleteLater`` plus an explicit drain. ``processEvents()`` does not
      deliver ``DeferredDelete``, and a probe using it concluded the widgets
      could not be freed at all, which was wrong.
    * Only the parentless windows PYTHON built. ``topLevelWidgets()`` is
      everything carrying the window flag, Qt's own included: an opened
      drop-down leaves a popup container parented to its ``Dropdown`` and, on
      Windows, a parentless helper window still marked visible after
      ``hidePopup()``. Destroying either took Qt's combo machinery with it —
      a reproducible access violation on the *next* ``showPopup()``.
      ``createdByPython`` is the line: what Python built, the test owns.
    """
    yield
    for widget in qt_app.topLevelWidgets():
        if widget.parent() is None and shiboken6.createdByPython(widget):
            widget.deleteLater()
    qt_app.sendPostedEvents(None, QEvent.DeferredDelete)


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


def _click(widget) -> None:
    """A left-click release on the widget itself.

    Sent rather than posted, and straight at the widget, because what is under
    test is the widget's own ``mouseReleaseEvent`` — a banner's bar opening a
    URL. A child button would consume a real click before it got there, which is
    the behaviour those tests rely on and must not simulate away.
    """
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QMouseEvent

    center = QPointF(widget.rect().center())
    event = QMouseEvent(
        QEvent.MouseButtonRelease,
        center,
        widget.mapToGlobal(center),
        Qt.LeftButton,
        Qt.NoButton,
        Qt.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def _settle(app, rounds: int = 5) -> None:
    """Let queued layout work finish.

    A geometry assertion needs more than one pass: the levelling is queued for
    after the layout settles, and the minimum height it sets then needs another
    pass before the widgets have actually moved.
    """
    for _ in range(rounds):
        app.processEvents()


def _sized_height(w) -> int:
    """The height ``w`` is supposed to have — the rule, not a number.

    gui/window_size.content_size takes the smallest of the content's natural
    height, SECONDARY_MAX_H, and a share of the screen. Asserting the first two
    only holds on a screen tall enough for them: CI's Windows runner is
    1024x768, where the screen share caps every one of these windows at 662 and
    seven tests that were green for months went red at once.
    """
    from gui.window_size import content_size

    return content_size(w, w._natural_height()).height()


def _screen_capped(w) -> bool:
    """Whether the SCREEN, rather than the content or the cap, is deciding."""
    from gui.window_size import SECONDARY_MAX_H

    return _sized_height(w) < min(w._natural_height(), SECONDARY_MAX_H)


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

    # ── the ticker does not dim what it has already scrolled past ────────
    #
    # Reported live from the 1.0.0-rc.1 binary. Every line in the ticker goes
    # past at the same reading distance, so the audience is reading the whole
    # column rather than one live line over a settled history — dimming the
    # older ones just makes most of the screen harder to read.

    def test_the_ticker_does_not_dim_older_translations(self, overlay):
        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        assert w._block_translation_qcolor(newest=False) == w._translation_qcolor()

    def test_realtime_still_dims_its_history(self, overlay):
        """There the history sits still under a live line, and the mute is
        what says "already said". Only the ticker changes."""
        w = overlay(SUBTITLE_MODE_REALTIME)
        assert w._block_translation_qcolor(newest=False) == w._history_qcolor()

    def test_static_still_dims_its_history(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC)
        assert w._block_translation_qcolor(newest=False) == w._history_qcolor()

    def test_the_ticker_leaves_the_stacked_original_muted(self, overlay):
        """"All the subtitles white, BESIDES the original text" — the stacked
        original stays the muted tone, which is what marks it as the source
        rather than as already-said."""
        from PySide6.QtGui import QColor

        w = overlay(SUBTITLE_MODE_CONTINUOUS, bilingual_mode=True, side_by_side=False)
        assert w._source_qcolor() == QColor(w._colors["muted"])

    def test_the_ticker_keeps_a_side_by_side_original_with_its_translation(
        self, overlay
    ):
        """Side by side the original is the other half of the row and drops to
        history exactly as its translation does. The ticker stops dimming the
        translation, so dimming the original would split the pair down the
        middle. Only that layout is affected — stacked is the test above."""
        w = overlay(SUBTITLE_MODE_CONTINUOUS, bilingual_mode=True, side_by_side=True)
        assert w._column_source_qcolor(newest=False) == w._column_source_qcolor(
            newest=True
        )

    def test_every_ticker_line_renders_in_the_same_colour(self, overlay, qt_app):
        """The rule above, checked on PIXELS rather than on the model — the two
        are different claims, and it is the painted ink the audience reads."""
        from PySide6.QtGui import QColor

        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        for i in range(4):
            w.add_subtitle(f"Zeile Nummer {i} mit genug Text zum Messen.")
        w.render(w.grab())

        wanted = QColor(w._translation_qcolor()).rgb()
        muted = QColor(w._history_qcolor()).rgb()
        assert wanted != muted, "theme gives both the same value — test proves nothing"

        image = w.grab().toImage()
        found = {image.pixel(x, y) & 0xFFFFFF
                 for y in range(0, image.height(), 2)
                 for x in range(0, image.width(), 2)}
        assert (wanted & 0xFFFFFF) in found, "no text drawn in the live colour"
        assert (muted & 0xFFFFFF) not in found, (
            "the ticker still painted a block in the muted history colour"
        )


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


class TestStayingInFrontOfTheTaskbar:
    """Always-on-top is a BAND, not a rank.

    Windows keeps its taskbar in that same band and orders it by whoever was
    raised last, so a click on the taskbar puts the shell in front of the
    subtitles for good: the flag is still set, and the overlay is still behind
    it. The overlay re-raises itself on a timer (_keep_on_top).
    """

    @staticmethod
    def _windows(monkeypatch, value: bool) -> None:
        # The module constant, never sys.platform: faking that for the whole
        # process crashed a previous run and spawned real windows.
        monkeypatch.setattr("gui.subtitle_window._WINDOWS", value)

    def test_the_timer_runs_while_the_setting_is_on(self, overlay, monkeypatch):
        self._windows(monkeypatch, True)
        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        assert w._restack_timer.isActive()

    def test_turning_the_setting_off_disarms_it(self, overlay, monkeypatch):
        self._windows(monkeypatch, True)
        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        w.set_always_on_top(False)
        assert not w._restack_timer.isActive()

    def test_nothing_restacks_on_the_other_platforms(self, overlay, monkeypatch):
        """macOS floats below the Dock whatever it asks for, and on X11 the
        stacking is the window manager's — re-raising every second there is a
        client fighting its own desktop."""
        self._windows(monkeypatch, False)
        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        assert not w._restack_timer.isActive()

    def test_a_tick_raises_the_overlay(self, overlay, monkeypatch):
        self._windows(monkeypatch, True)
        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        raised: list[bool] = []
        monkeypatch.setattr(w, "raise_", lambda: raised.append(True))
        monkeypatch.setattr(w, "isVisible", lambda: True)
        w._keep_on_top()
        assert raised == [True]

    def test_a_hidden_overlay_is_left_alone(self, overlay, monkeypatch):
        """Raising a window nobody is looking at is a pointless SetWindowPos
        once a second for as long as the app is open."""
        self._windows(monkeypatch, True)
        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        raised: list[bool] = []
        monkeypatch.setattr(w, "raise_", lambda: raised.append(True))
        monkeypatch.setattr(w, "isVisible", lambda: False)
        w._keep_on_top()
        assert raised == []


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
        from gui.subtitle_window import PILL_FONT_PX

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
        from gui.widgets import is_window_on_top, needs_remap

        w = overlay(SUBTITLE_MODE_CONTINUOUS)
        w.show()
        qt_app.processEvents()
        native = int(w.winId())
        for enabled in (True, False, True, True, False):
            w.set_always_on_top(enabled)
            qt_app.processEvents()
            assert w.isVisible(), f"hidden after set_always_on_top({enabled})"
            assert is_window_on_top(w) is enabled
            if needs_remap():
                # X11 has no cheap path: the flag IS the _NET_WM_STATE_ABOVE
                # property and Qt's xcb plugin only writes it while the window
                # is unmapped, so set_window_on_top re-creates and re-shows
                # there by design. What must hold on every platform is that the
                # window survives and stays visible — asserted above.
                native = int(w.winId())
            else:
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
        from gui.icons import ICON_SIZES, app_icon

        icon = app_icon()
        assert icon is not None and not icon.isNull()
        assert set(ICON_SIZES) <= {size.width() for size in icon.availableSizes()}

    def test_the_taskbar_size_is_drawn_and_square(self, qt_app):
        from gui.icons import app_icon

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
        # gui/control_panel.py and gui/icons.py both call logo_mark, and
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
            "gui.app",
            "gui.already_running",
            "gui.api_keys",
            "gui.announce_window",
            "gui.batch_window",
            "gui.control_panel",
            "gui.history_window",
            "gui.onboarding",
            "gui.settings_window",
            "gui.subtitle_window",
            "gui.update_banner",
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

        root = pathlib.Path(__file__).resolve().parent.parent / "gui"
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
        from gui.widgets import SegmentedControl

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
        from gui.widgets import SegmentedControl

        seg = SegmentedControl(["a", "b", "c"])
        assert [b.property("seg") for b in seg._buttons] == ["first", "middle", "last"]
        # Keep a reference: an unreferenced widget is collected and its C++
        # object deleted before the assertion runs.
        single = SegmentedControl(["only"])
        assert single._buttons[0].property("seg") == "only"

    def test_programmatic_set_does_not_emit(self, qt_app):
        # set_current_index is used to sync state; emitting would risk a loop
        # with handlers that write back.
        from gui.widgets import SegmentedControl

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
        import gui.control_panel as cp

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
        import gui.control_panel as cp

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
        if not p.device_indices:
            pytest.skip("no input devices on this machine")
        p._starting = True
        current = p.device_indices[p.device_combo.currentIndex()]
        p._started_device = current
        p._pending_device = current
        p._start_error = None
        p._finish_start()
        assert controller.swaps == []  # it is already the running device

    def test_a_failed_start_drops_the_parked_device(self, panel, monkeypatch):
        import gui.control_panel as cp

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
        import gui.control_panel as cp

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

    def test_the_default_height_clears_the_themed_card_stack(
        self, qt_app, monkeypatch
    ):
        """A fresh install must not open already scrolled.

        The old default was 880x640 against a card stack needing a 659 px
        window — nineteen pixels short, so every first launch showed a scroll
        bar, and nothing failed.

        **The theme is the whole test.** Card padding, borders and fonts all come
        from the stylesheet, applied at polish time, so an unthemed panel
        measures ~50 px shorter than the one a user sees — 550 against 599. The
        first version of this test skipped `apply_theme`, measured the short
        panel, and passed with the height put back to 640. `gui/AGENTS.md`
        warns about exactly this; it costs a wrong pass here rather than a
        wrong pixel.

        And the requirement is compared against the CONSTANT, not against a
        figure read off the same window: a test that measures both sides of its
        own assertion cannot fail in the direction that matters.

        Both directions, because both have now shipped: 640 was 19 px short and
        opened scrolled, 780 overshot by 121 and opened visibly half empty. The
        failure message carries the measured figure, which is what makes the
        constant maintainable — a card that gains a row fails here and says by
        how much.
        """
        import gui.control_panel as cp
        from gui.theme import apply_theme
        from utils.settings import load_settings

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )
        # Cleared BEFORE construction: __init__ is where a stored geometry is
        # read, and it wins over the default. (The autouse pinned_window_settings
        # fixture already blanks it; this states the dependency rather than
        # relying on it.)
        settings = load_settings()
        settings.window_geometry, settings.window_maximized = "", False

        class FakeController:
            pass

        # Restored afterwards, and that is not tidiness. setStyleSheet is
        # GLOBAL, so a theme left applied here re-measures every card for every
        # test that runs after this one — which is how the first version of this
        # test broke test_a_column_is_never_narrower_than_its_cards_need on the
        # Linux runner (themed cards need 869 px there against _COL2_MIN_W's
        # 800; unthemed they fit). That is a real finding about _COL2_MIN_W and
        # is left alone here: it is not what this PR is about, and hiding it
        # again by leaking the theme would be worse than either.
        previous_sheet = qt_app.styleSheet()
        apply_theme(qt_app, "light")
        p = cp.ControlPanel(FakeController())
        try:
            if not p._log_collapsed:
                p._toggle_log_panel()
            # Opened tall on purpose, so the measurement is of the content and
            # not of whatever the default happens to be. The window chrome is
            # the difference between the two.
            p.resize(cp._DEFAULT_W, 900)
            p.show()
            _settle(qt_app, rounds=14)
            area = p.card_area
            content = area.widget().sizeHint().height()
            chrome = p.height() - area.viewport().height()
            needed = content + chrome
            assert cp._DEFAULT_H >= needed, (
                f"the cards need a {needed}px window at {cp._DEFAULT_W}px wide "
                f"and _DEFAULT_H is {cp._DEFAULT_H} — a fresh install opens "
                f"already scrolled"
            )
            # The slack allowance is one card's inner padding, not a round
            # number: enough that a stylesheet tweak of a pixel or two does not
            # fail this, far short of the 121 px of dead space 780 produced.
            assert cp._DEFAULT_H <= needed + 24, (
                f"the cards need a {needed}px window and _DEFAULT_H is "
                f"{cp._DEFAULT_H} — {cp._DEFAULT_H - needed}px of empty space "
                f"below the last card"
            )
            # …and the default width keeps the two-column arrangement the setup
            # videos show. Three columns pins the Advanced card open, which is a
            # denser panel than a first-time user should be handed.
            p.resize(cp._DEFAULT_W, cp._DEFAULT_H)
            _settle(qt_app, rounds=14)
            assert p.card_grid.count == 2
            assert not area.verticalScrollBar().isVisible()
        finally:
            p.close()
            qt_app.setStyleSheet(previous_sheet)

    def test_the_opening_size_never_exceeds_the_screen(self, panel):
        """The height is chosen from the CONTENT, and content does not shrink to
        suit a 768px laptop — so it is clamped. Without this a default picked on
        a tall monitor opens partly under the taskbar, where the window cannot be
        dragged up to reach its own title bar."""
        from gui.window_size import MAX_SCREEN_SHARE

        class FakeRect:
            def __init__(self, w, h):
                self._w, self._h = w, h

            def width(self):
                return self._w

            def height(self):
                return self._h

        class FakeScreen:
            def __init__(self, w, h):
                self._r = FakeRect(w, h)

            def availableGeometry(self):
                return self._r

        for width, height in ((2048, 1104), (1366, 728), (1280, 680), (1024, 600)):
            panel.screen = lambda w=width, h=height: FakeScreen(w, h)
            size = panel._default_size()
            assert size.width() <= int(width * MAX_SCREEN_SHARE)
            assert size.height() <= int(height * MAX_SCREEN_SHARE)

    def test_card_grid_reflows_with_the_window(self, panel, qt_app):
        # 1 / 2 / 3 columns, at the same thresholds the Tk panel uses.
        # A hidden widget never receives resizeEvent, so the reflow is driven
        # directly — that is exactly what the handler does, and showing a real
        # control-panel window during a test run is not worth the parity.
        for width, expected in ((1200, 3), (900, 2), (520, 1)):
            panel.resize(width, 800)
            panel._relayout_columns()
            assert panel.card_grid.count == expected, f"{width}px should give {expected}"

    def test_a_column_is_never_narrower_than_its_cards_need(self, panel):
        # The horizontal scrollbar is off, so a threshold that lets a column
        # below its minimum clips the card instead of scrolling it.
        needs = [c.minimumSizeHint().width() for c in (panel.card_grid.col_a, panel.card_grid.col_b)]
        margins = panel.card_grid.grid.contentsMargins()
        chrome = margins.left() + margins.right() + panel.card_grid.grid.horizontalSpacing()
        assert card_grid_module()._COL2_MIN_W >= sum(needs) + chrome

    def test_opening_the_log_gives_the_cards_one_column(self, panel):
        assert panel._log_collapsed
        panel.resize(1200, 800)
        panel._relayout_columns()
        assert panel.card_grid.count == 3
        panel._toggle_log_panel()
        assert panel.card_grid.count == 1

    def test_the_log_opens_inside_a_window_that_can_hold_it(self, panel, qt_app):
        # It shares the window rather than bolting 420px onto the side; only a
        # window too narrow for both is widened.
        panel.resize(1400, 800)
        qt_app.processEvents()
        panel._toggle_log_panel()
        assert panel.width() == 1400
        assert panel.sidebar.width() == cp_module()._SIDEBAR_W_WITH_LOG

    def test_a_narrow_window_grows_to_fit_the_log(self, panel, qt_app):
        from PySide6.QtGui import QGuiApplication

        panel.resize(600, 800)
        qt_app.processEvents()
        panel._toggle_log_panel()
        # It grows to hold the sidebar and the log side by side — but never past
        # the screen it is on (the production rule in _toggle_log_panel). On a
        # headless/offscreen display narrower than that sum the window is capped
        # at the screen width, so the assertion states the rule, not the number.
        wanted = cp_module()._SIDEBAR_W_WITH_LOG + cp_module()._LOG_PANEL_MIN_W
        screen = panel.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            wanted = min(wanted, screen.availableGeometry().width())
        assert panel.width() >= wanted

    # --- log panel gives the width back (these need a SHOWN window) ---------
    #
    # The `panel` fixture never shows its window, and _apply_minimum_size()
    # early-returns a minimum of 0 while a window is invisible. That matters
    # here more than anywhere: opening the log widens the window by RAISING ITS
    # MINIMUM, and setMinimumSize only grows a window that is really on screen.
    # Driven hidden, these four tests pass against code that does nothing at
    # all — measured, not assumed (702 -> 840 shown, 702 -> 702 hidden).

    @staticmethod
    def _shown(panel, qt_app, width: int):
        """Show the panel at `width` and let the layout settle."""
        panel.show()
        qt_app.processEvents()
        panel.resize(width, 800)
        qt_app.processEvents()
        return panel.width()

    @staticmethod
    def _log_width(panel) -> int:
        """The width opening the log grows a too-narrow window to.

        Computed the way production does rather than hardcoded: the rule caps
        at the screen, so the number differs on a narrow display.
        """
        from PySide6.QtGui import QGuiApplication

        wanted = cp_module()._SIDEBAR_W_WITH_LOG + cp_module()._LOG_PANEL_MIN_W
        screen = panel.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            wanted = min(wanted, screen.availableGeometry().width())
        return wanted

    def test_closing_the_log_gives_the_widened_width_back(self, panel, qt_app):
        """Opening the log on a narrow window widens it; closing has to undo
        exactly that, or every peek at the log leaves the window bigger than
        the user left it — permanently, since the geometry is stored on exit."""
        narrow = self._log_width(panel) - 200
        before = self._shown(panel, qt_app, narrow)
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() > before  # it did widen — otherwise nothing to undo
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == before

    def test_closing_the_log_leaves_a_window_it_never_widened(self, panel, qt_app):
        """A window already wide enough is not grown on open, so it must not be
        shrunk on close either."""
        wide = self._log_width(panel) + 100
        before = self._shown(panel, qt_app, wide)
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == before
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == before

    def test_a_second_cycle_gives_back_its_own_width(self, panel, qt_app):
        """The record belongs to the cycle that made it. A width left over from
        an earlier open must never be the one a later close hands back."""
        widened = self._log_width(panel)
        self._shown(panel, qt_app, widened - 200)
        panel._toggle_log_panel()
        panel._toggle_log_panel()
        qt_app.processEvents()

        wide = self._shown(panel, qt_app, widened + 100)
        panel._toggle_log_panel()  # wide enough already — widens nothing
        qt_app.processEvents()
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == wide

    def test_a_log_window_dragged_wider_is_not_snapped_back(self, panel, qt_app):
        """Dragging the window out to read the log is a deliberate choice.
        Closing must give back what OPENING took, not undo the user's resize."""
        narrow = self._log_width(panel) - 200
        self._shown(panel, qt_app, narrow)
        panel._toggle_log_panel()
        qt_app.processEvents()
        panel.resize(panel.width() + 200, 800)
        qt_app.processEvents()
        dragged = panel.width()
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == dragged

    def test_a_wide_log_window_dragged_wider_is_not_snapped_back(
        self, panel, qt_app
    ):
        """Same, for a window that was wide enough to begin with — opening took
        nothing from it, so closing owes it nothing."""
        wide = self._shown(panel, qt_app, self._log_width(panel) + 100)
        panel._toggle_log_panel()
        qt_app.processEvents()
        panel.resize(wide + 200, 800)
        qt_app.processEvents()
        dragged = panel.width()
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == dragged

    def test_a_log_window_dragged_smaller_is_not_snapped_back_up(
        self, panel, qt_app
    ):
        """Reported live. A wide window (2 or 3 columns) is not widened by the
        log at all, so it records its own width; dragging it SMALLER and then
        closing the log used to snap it back up to the old size. Shrinking is a
        deliberate choice exactly as much as widening is."""
        self._shown(panel, qt_app, self._log_width(panel) + 400)
        panel._toggle_log_panel()
        qt_app.processEvents()
        panel.resize(self._log_width(panel) + 60, 800)
        qt_app.processEvents()
        dragged = panel.width()
        panel._toggle_log_panel()
        qt_app.processEvents()
        assert panel.width() == dragged

    def test_the_log_widens_by_raising_the_minimum_not_only_by_resizing(
        self, panel, qt_app
    ):
        """Pins the mechanism the first fix got wrong: _apply_log_panel_widths()
        raises the window's minimum to fit the log, and setMinimumSize grows a
        window sitting under the new floor on the spot. So the width to give
        back has to be captured BEFORE that call — after it, it is gone."""
        narrow = self._log_width(panel) - 200
        self._shown(panel, qt_app, narrow)
        assert panel.minimumWidth() < narrow  # the cards' floor, well under it
        panel._log_collapsed = False  # what the toggle sets before it applies
        panel._apply_log_panel_widths()  # no resize() call anywhere inside
        qt_app.processEvents()
        assert panel.minimumWidth() > narrow
        assert panel.width() > narrow
        panel._log_collapsed = True  # leave the fixture as we found it

    def test_the_layout_width_does_not_move_when_the_scroll_bar_appears(
        self, panel, qt_app
    ):
        """The invariant behind the flap below, and the one that is screen
        independent. The column count decides the content height, which decides
        whether the vertical bar shows, which changes the viewport — so the
        width the columns are chosen from must not depend on the bar."""
        from PySide6.QtCore import Qt

        self._shown(panel, qt_app, 1000)
        area = panel.card_area
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        qt_app.processEvents()
        with_bar = panel._available_width()
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        qt_app.processEvents()
        without_bar = panel._available_width()
        area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        assert with_bar == without_bar

    def test_the_column_count_settles_at_the_three_column_threshold(
        self, panel, qt_app
    ):
        """Reported live: the window "bugs around", unable to decide whether to
        go a grid bigger or smaller.

        _COL3_MIN_W sits inside the scroll bar's own width, so every window
        width in that band closed the loop: 3 columns -> Advanced card pinned
        open -> content taller -> bar appears -> viewport under the threshold ->
        2 columns -> content shorter -> bar goes -> 3 columns. Measured before
        the fix: 3/2/3/2 forever at all ten widths, at 720 and 800 px tall.
        """
        from PySide6.QtGui import QGuiApplication

        threshold = card_grid_module()._COL3_MIN_W
        bar = panel.card_area.verticalScrollBar().sizeHint().width()
        screen = panel.screen() or QGuiApplication.primaryScreen()
        if screen and screen.availableGeometry().width() < threshold + bar:
            pytest.skip("display is narrower than the three-column threshold")

        panel.show()
        qt_app.processEvents()
        for width in range(threshold, threshold + bar + 1):
            panel.resize(width, 720)
            for _ in range(8):
                qt_app.processEvents()
            seen = set()
            for _ in range(14):
                qt_app.processEvents()
                seen.add(panel.card_grid.count)
            assert len(seen) == 1, (
                f"{width}px never settled — oscillated between "
                f"{sorted(seen)} columns"
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
            panel.card_grid.grid.itemAt(i).widget() for i in range(panel.card_grid.grid.count())
        }
        assert placed == {panel.card_grid.col_a, panel.card_grid.col_b, panel.card_grid.col_c}

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
        import gui.control_panel as cp

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
        import gui.control_panel as cp

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
        import gui.control_panel as cp

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

    @staticmethod
    def _other_than(combo, current):
        """Some engine in the dropdown that is not the one already selected."""
        for i in range(combo.count()):
            if (value := combo.itemData(i)) != current:
                return value
        raise AssertionError("the dropdown offers only the current engine")

    def test_unticking_restores_the_translation_engine_it_overrode(self, panel):
        # Ticking replaces a manual pick, which is the point. Unticking has to
        # give it back, or trying the box out once costs the choice for good.
        from utils.settings import DEFAULT_AI_PROVIDER

        panel.use_default_translation.setChecked(False)
        manual = self._other_than(panel.provider_combo, DEFAULT_AI_PROVIDER)
        panel.provider_combo.setCurrentIndex(panel.provider_combo.findData(manual))
        manual_model = panel.settings.translation_model

        panel.use_default_translation.setChecked(True)
        assert panel.settings.ai_provider == DEFAULT_AI_PROVIDER

        panel.use_default_translation.setChecked(False)
        assert panel.settings.ai_provider == manual
        assert panel.settings.translation_model == manual_model
        assert panel.provider_combo.currentData() == manual

    def test_unticking_restores_the_transcription_engine_it_overrode(self, panel):
        from utils.settings import (
            DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER,
            DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            PIPELINE_MODE_STREAMING,
        )

        # The engine the tick will impose — which is what the manual pick has
        # to differ from. Excluding merely the *current* one is not enough:
        # the panel reads the real settings file, so whichever engine is
        # selected there decides whether the two collide.
        imposed = (
            DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
            if panel.settings.pipeline_mode == PIPELINE_MODE_STREAMING
            else DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
        )
        panel.use_default_transcription.setChecked(False)
        manual = self._other_than(panel.transcription_provider_combo, imposed)
        panel.transcription_provider_combo.setCurrentIndex(
            panel.transcription_provider_combo.findData(manual)
        )
        manual_model = panel.settings.transcription_model

        panel.use_default_transcription.setChecked(True)
        assert panel.settings.transcription_provider == imposed

        panel.use_default_transcription.setChecked(False)
        assert panel.settings.transcription_provider == manual
        assert panel.settings.transcription_model == manual_model
        assert panel.transcription_provider_combo.currentData() == manual

    def test_the_card_collapses_while_it_shares_a_column(self, panel):
        panel.resize(900, 800)
        panel._relayout_columns()
        assert panel.card_grid.count == 2
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
        assert panel.card_grid.count == 3
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
        if QApplication.focusWidget() is None:
            # Nothing can hold the application focus without a window manager
            # to activate a window — xvfb runs without one. The behaviour under
            # test only exists where focus does.
            pytest.skip("no window manager: no widget can take focus here")
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
        if QApplication.focusWidget() is None:
            # See the test above: no window manager, no focus to move.
            pytest.skip("no window manager: no widget can take focus here")
        panel._sync_running_state()
        qt_app.processEvents()
        assert QApplication.focusWidget() is panel.stop_btn


class TestDisplaySlidersFollowTheMode:
    """The two Display sliders track what the current mode gives them.

    Height never greys out — it means something everywhere — but in transparent
    static it stops being a height and becomes a lift, so its range, readout and
    caption all swap. Neither does opacity: Transparent takes the WINDOW's
    backdrop away and the slider moves to the card behind each line.
    """

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        from gui.theme import apply_theme

        apply_theme(qt_app, "dark")
        p = _panel(monkeypatch)
        yield p
        p.close()

    @staticmethod
    def _set_mode(panel, mode: str) -> None:
        """Put the panel in ``mode`` and re-sync, whether or not that moved it.

        ``setCurrentIndex`` emits nothing when the index is unchanged, and the
        panel opens in whatever mode the machine's own settings.json says — so
        a test asking for the mode it is already in would otherwise assert
        against a sync that never ran.
        """
        index = panel.mode_combo.findData(mode)
        if index < 0:
            pytest.skip(f"the current strategy offers no {mode} mode")
        panel.mode_combo.setCurrentIndex(index)
        panel._sync_mode_controls()

    def _lift(self, panel) -> None:
        self._set_mode(panel, SUBTITLE_MODE_STATIC)
        panel.transparent_check.setChecked(True)
        panel._sync_display_sliders()  # same reason as _set_mode

    # ── height: two controls in one row ──────────────────────────────────
    def test_the_height_row_is_never_greyed_out(self, panel):
        for mode in (SUBTITLE_MODE_STATIC, SUBTITLE_MODE_REALTIME):
            self._set_mode(panel, mode)
            for transparent in (True, False):
                panel.transparent_check.setChecked(transparent)
                assert panel.height_row.isEnabled(), (mode, transparent)

    def test_transparent_static_turns_it_into_a_capped_lift(self, panel):
        self._lift(panel)
        assert panel._lift_mode()
        assert panel.height_slider.maximum() == STATIC_LIFT_PERCENT_MAX
        assert panel.height_slider.minimum() == STATIC_LIFT_PERCENT_MIN
        # A control whose meaning changed has to say so.
        assert panel.height_caption.text() != panel._t("height", "Height:")
        assert panel.height_row.toolTip()

    def test_everything_else_keeps_it_a_height(self, panel):
        self._lift(panel)
        panel.transparent_check.setChecked(False)
        assert not panel._lift_mode()
        assert panel.height_slider.maximum() == WINDOW_HEIGHT_PERCENT_MAX
        assert panel.height_slider.minimum() == WINDOW_HEIGHT_PERCENT_MIN
        assert panel.height_caption.text() == panel._t("height", "Height:")
        assert not panel.height_row.toolTip()

    def test_each_meaning_comes_back_to_its_own_value(self, panel):
        """The point of the two fields: toggling Transparent must hand the
        slider what THAT meaning was last left at, not the other one's number
        squeezed into its range."""
        panel.settings.window_height_percent = WINDOW_HEIGHT_PERCENT_MAX
        panel.settings.static_lift_percent = 0
        self._lift(panel)
        assert panel.height_slider.value() == 0, "the lift got the height's value"
        panel.transparent_check.setChecked(False)
        panel._sync_display_sliders()
        assert panel.height_slider.value() == WINDOW_HEIGHT_PERCENT_MAX
        # And neither write touched the other's field.
        assert panel.settings.window_height_percent == WINDOW_HEIGHT_PERCENT_MAX
        assert panel.settings.static_lift_percent == 0

    def test_switching_modes_never_rewrites_the_stored_value(self, panel):
        """setRange clamps and setValue moves, and both emit valueChanged — so
        without blocking the signal, arriving in a mode would write ITS value
        into the field of the mode just left."""
        panel.settings.window_height_percent = WINDOW_HEIGHT_PERCENT_MAX
        panel.settings.static_lift_percent = 30
        self._lift(panel)
        assert panel.settings.window_height_percent == WINDOW_HEIGHT_PERCENT_MAX
        panel.transparent_check.setChecked(False)
        panel._sync_display_sliders()
        assert panel.settings.static_lift_percent == 30

    def test_dragging_it_writes_the_meaning_in_force(self, panel):
        self._lift(panel)
        panel.height_slider.setValue(12)
        assert panel.settings.static_lift_percent == 12
        assert panel.settings.window_height_percent != 12, "wrote the wrong field"
        assert panel.height_value.text() == "12%"

        panel.transparent_check.setChecked(False)
        panel._sync_display_sliders()
        panel.height_slider.setValue(40)
        assert panel.settings.window_height_percent == 40
        assert panel.settings.static_lift_percent == 12, "the lift was overwritten"

    def test_the_readout_follows_the_swap(self, panel):
        panel.settings.window_height_percent = WINDOW_HEIGHT_PERCENT_MAX
        panel.settings.static_lift_percent = STATIC_LIFT_PERCENT_MAX
        self._lift(panel)
        assert panel.height_value.text() == f"{STATIC_LIFT_PERCENT_MAX}%"

    # ── opacity ──────────────────────────────────────────────────────────
    def test_transparent_keeps_the_opacity_row_live(self, panel):
        """It used to grey out, on the grounds that Transparent leaves nothing
        to apply an opacity to. The mode still paints a card behind each line,
        and that card is the only thing between the text and the video — so the
        slider keeps its job and the tooltip says which backdrop it reaches."""
        self._lift(panel)
        assert panel.opacity_row.isEnabled()
        assert panel.opacity_row.toolTip(), "nothing says what it now applies to"
        panel.transparent_check.setChecked(False)
        assert panel.opacity_row.isEnabled()
        assert not panel.opacity_row.toolTip()


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
        from gui.widgets import AudioLevelBar as Bar

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
        from gui.widgets import AudioLevelBar as Bar

        # The translucent zone map is drawn amber-then-red; one pixel of
        # overlap composited twice and showed as a dark seam.
        amber = Bar.band_span(0, 275, Bar.GREEN_END, Bar.RED_START)
        red = Bar.band_span(0, 275, Bar.RED_START, 1.0)
        assert amber[1] <= red[0]


class TestNoStrayTopLevelWindows:
    """Little boxes flashed across the screen before the panel opened."""

    def test_a_card_never_shows_a_parentless_widget(self, qt_app):
        from gui.theme import apply_theme
        from gui.widgets import Card

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

        from gui.theme import apply_theme
        from gui.widgets import Card

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


class TestMinimumWindowWidth:
    """The panel must not be draggable narrower than its cards.

    The card area scrolls vertically only, so below the cards' own minimum the
    content is simply cut off — there is no horizontal bar to reach it, and the
    vertical one then draws on top of the clipped edge. Reported against the
    old fixed 420 px floor, where the cards needed 449.
    """

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        from gui.theme import apply_theme

        # Themed, because every figure here is style-derived: a card's padding,
        # border and font all come from the application stylesheet, and an
        # unthemed panel measures a different (and order-dependent) minimum.
        apply_theme(qt_app, "dark")
        p = _panel(monkeypatch)
        if not p._log_collapsed:
            p._toggle_log_panel()
        p.resize(1400, 900)
        p.show()
        _settle(qt_app)
        yield p
        p.close()

    def test_the_window_cannot_be_squeezed_below_the_cards(self, panel, qt_app):
        panel.resize(200, 900)
        _settle(qt_app)
        assert panel.card_area.viewport().width() >= panel.cards_host.width(), (
            "the cards are wider than the viewport showing them"
        )

    def test_the_floor_is_measured_not_assumed(self, panel):
        # It has to follow the cards, because the widest row is a translated
        # label beside a segmented control and that differs per GUI language.
        expected = (
            panel.card_grid.minimum_width()
            + panel.card_area.verticalScrollBar().sizeHint().width()
            + 2 * panel.card_area.frameWidth()
        )
        assert panel.minimumWidth() == expected

    def test_a_wide_panel_can_still_be_dragged_back_to_one_column(
        self, panel, qt_app
    ):
        # The regression a host-based measurement causes: at three columns the
        # host's minimum is all three columns added up, which pins the window
        # open at the width it happens to have.
        #
        # Driven to three columns rather than assumed to open there: the panel
        # opens at whatever its default size gives, which on CI's 1024x768
        # runner is two. Asserting the default was asserting the screen.
        panel.resize(1400, 900)
        _settle(qt_app)
        assert panel.card_grid.count == 3
        panel.resize(600, 900)
        _settle(qt_app)
        assert panel.width() == 600
        assert panel.card_grid.count == 1

    def test_an_open_log_keeps_room_for_both(self, panel, qt_app):
        # The sidebar is pinned wide while the log shares the window, so the
        # floor is that plus the log panel's own minimum — not the cards'.
        panel._toggle_log_panel()
        _settle(qt_app)
        cp = cp_module()
        assert panel.minimumWidth() == cp._SIDEBAR_W_WITH_LOG + cp._LOG_PANEL_MIN_W
        panel.resize(200, 900)
        _settle(qt_app)
        assert panel.log_panel.width() >= cp._LOG_PANEL_MIN_W


class TestTitleBarTheme:
    """The native title bar follows the app theme, not the OS preference.

    Windows paints a caption bar from the system light/dark setting and from
    nothing the application asks for, so a light-themed panel under a dark
    Windows kept a black bar joined to a white header.
    """

    def test_a_window_without_a_native_handle_is_left_alone(self, qt_app):
        from PySide6.QtWidgets import QWidget

        from gui.widgets import set_titlebar_dark

        # Asking for an HWND early would force Qt to create the platform
        # window ahead of time; there is nothing to theme yet either way.
        widget = QWidget()
        set_titlebar_dark(widget, True)  # must not raise, must not create one
        assert widget.windowHandle() is None
        widget.deleteLater()

    def test_the_theme_sweep_skips_frameless_windows(self, qt_app, monkeypatch):
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        import gui.theme as theme
        import gui.widgets as widgets

        seen: list[tuple[QWidget, bool]] = []
        monkeypatch.setattr(
            widgets, "set_titlebar_dark", lambda w, dark: seen.append((w, dark))
        )
        framed = QWidget()
        framed.show()
        frameless = QWidget()
        frameless.setWindowFlag(Qt.FramelessWindowHint, True)
        frameless.show()
        qt_app.processEvents()

        theme.apply_titlebar_theme(qt_app, "light")

        themed = [w for w, _dark in seen]
        assert framed in themed
        # The subtitle overlay is frameless: it has no caption to theme, and
        # poking its HWND buys nothing.
        assert frameless not in themed
        assert all(dark is False for _w, dark in seen), "light asked for dark"
        for w in (framed, frameless):
            w.close()
            w.deleteLater()

    def test_dark_is_anything_that_is_not_light(self, qt_app, monkeypatch):
        from PySide6.QtWidgets import QWidget

        import gui.theme as theme
        import gui.widgets as widgets

        seen: list[bool] = []
        monkeypatch.setattr(
            widgets, "set_titlebar_dark", lambda w, dark: seen.append(dark)
        )
        window = QWidget()
        window.show()
        qt_app.processEvents()
        theme.apply_titlebar_theme(qt_app, "dark")
        assert seen and all(seen)
        window.close()
        window.deleteLater()


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
        assert panel.card_grid.count == 3
        bottoms = {
            card.geometry().y() + card.geometry().height()
            for _box, card in panel.card_grid.tails
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
            for _box, card in panel.card_grid.tails
        )
        assert bottom < host.height() - 100, (
            f"cards reach {bottom} of {host.height()} — they filled the window"
        )

    def test_one_column_keeps_natural_heights(self, panel, qt_app):
        panel.resize(600, 980)
        qt_app.processEvents()
        assert panel.card_grid.count == 1
        for _box, card in panel.card_grid.tails:
            assert card.height() == card.sizeHint().height()

    def _top(self, card):
        host = card.window().cards_host
        return card.mapTo(host, card.rect().topLeft()).y()

    def test_two_columns_stack_the_right_column_tightly(self, panel, qt_app):
        # Levelling the bottoms here means padding Advanced away from the card
        # above it — a gap that grows every time column A does.
        panel.resize(900, 900)
        qt_app.processEvents()
        if panel.height() < 900:
            # The levelling this measures depends on how much height the
            # columns actually got. A screen shorter than the window asked for
            # (CI's runner is 1024x768) makes the gap a measurement of the
            # screen instead.
            pytest.skip("screen too short for the 900x900 layout under test")
        assert panel.card_grid.count == 2
        language = panel.card_grid.tails[1][1]
        host = panel.cards_host
        gap = self._top(panel.advanced_card) - (
            language.mapTo(host, language.rect().bottomLeft()).y()
        )
        spacing = panel.card_grid.grid.verticalSpacing()
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
            assert panel.card_grid.count == 2, size
            display = panel.card_grid.tails[0][1]
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
        assert self._bottom(panel.card_grid.tails[0][1]) == self._bottom(advanced)

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
        assert panel.card_grid.count == 2
        advanced = panel.advanced_card
        advanced.set_expanded(False)
        panel._level_two_column_bottoms()
        _settle(qt_app)
        assert not advanced.is_expanded()
        assert advanced.height() == advanced.sizeHint().height(), "inflated"
        assert self._bottom(panel.card_grid.tails[0][1]) == self._bottom(advanced)

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
        assert panel.card_grid.count == 2
        before = self._top(panel.advanced_card)
        panel.typography.set_expanded(True)
        _settle(qt_app)
        drift = abs(self._top(panel.advanced_card) - before)
        assert drift <= card_grid_module()._LEVEL_FILL_MAX_PX, f"slid {drift}px"
        panel.typography.set_expanded(False)
        _settle(qt_app)
        assert self._top(panel.advanced_card) == before

    def test_always_on_top_covers_the_control_panel(self, panel, qt_app):
        from gui.widgets import is_window_on_top, needs_remap
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
            if needs_remap():
                # X11 must re-map to write _NET_WM_STATE_ABOVE; see
                # gui/widgets.set_window_on_top. Staying visible is the
                # invariant there, and it is asserted above.
                native = int(panel.winId())
            else:
                # Recreating the native window is what made the panel flash
                # white.
                assert int(panel.winId()) == native, f"recreated for mode {mode}"


class TestControlRowHeights:
    """Everything sharing a row with a dropdown is as tall as the dropdown."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        from gui.theme import apply_theme

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

        from gui.widgets import CONTROL_H

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

        from gui.theme import apply_theme

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

        from gui.theme import apply_theme

        apply_theme(qt_app, "light")
        box = QCheckBox("Aus")
        box.resize(240, 30)
        assert self._accent_pixels(box.grab().toImage()) < 20

    def test_the_dropdown_paints_a_chevron_not_a_block(self, qt_app):
        from gui.theme import apply_theme
        from gui.widgets import Dropdown

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
        from gui.theme import apply_theme, current_colors
        from gui.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch", "English"])
        combo.resize(240, 44)
        assert self._fill(combo) == current_colors()["entry"]
        self._set_hover(combo, True)
        assert self._fill(combo) == current_colors()["panel_soft"]

    def test_a_disabled_dropdown_stays_inert(self, qt_app):
        from gui.theme import apply_theme, current_colors
        from gui.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch"])
        combo.setEnabled(False)
        combo.resize(240, 44)
        self._set_hover(combo, True)
        assert self._fill(combo) == current_colors()["entry"]

    def test_the_level_meter_shows_its_zones_while_silent(self, qt_app):
        # The operator needs to see how much headroom is left before amber and
        # red, not discover the boundaries by clipping.
        from gui.theme import apply_theme, current_colors
        from gui.widgets import AudioLevelBar

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

        from gui.theme import apply_theme
        from gui.widgets import Dropdown

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

        from gui.theme import apply_theme
        from gui.widgets import Dropdown

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
        from gui.theme import apply_theme
        from gui.widgets import Dropdown

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

        from gui.theme import apply_theme
        from gui.widgets import Dropdown

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
        from gui.theme import stylesheet

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
        from gui.theme import stylesheet

        sheet = stylesheet("light")
        assert sheet.index("QComboBox:enabled:focus") > sheet.index(
            "QComboBox:enabled:hover"
        )


def row_text(window, index: int) -> str:
    """Title + detail line of one history list row, as the delegate has them."""
    from gui.history_window import RowDelegate

    item = window.entry_list.item(index)
    return f"{item.text()}\n{item.data(RowDelegate.SUB_ROLE)}"


def row_tag(window, index: int) -> str:
    from gui.history_window import RowDelegate

    return window.entry_list.item(index).data(RowDelegate.TAG_ROLE)


class TestHistoryWindow:
    """Rendering only — parsing is utils/history.py and already covered."""

    @pytest.fixture
    def history(self, qt_app, monkeypatch):
        import gui.history_window as hw
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
        import gui.history_window as hw

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
        import gui.history_window as hw

        make, _ = history
        w = make()
        assert w.detail.parentWidget().layout().contentsMargins().left() >= hw.PANE_GAP

    def test_a_saved_summary_is_marked_on_the_row(self, history):
        import gui.history_window as hw

        make, _ = history
        w = make()
        assert row_text(w, 0).startswith(hw.SUMMARY_MARK)
        assert not row_text(w, 1).startswith(hw.SUMMARY_MARK)

    def test_the_summary_marker_says_what_it_means(self, history):
        """📝 is a bare glyph. The Tk viewer and the first Qt one both explained
        it in a tooltip; the four-tab rewrite dropped the only setToolTip call
        in the window and left the marker unexplained."""
        make, _ = history
        w = make()
        assert w.entry_list.item(0).toolTip()
        assert not w.entry_list.item(1).toolTip()

    def test_delete_is_the_danger_button(self, history):
        # Deleting a record is irreversible; it must not read like Copy.
        make, _ = history
        w = make()
        assert w.delete_btn.objectName() == "danger"
        assert w.summarise_btn.objectName() == "accent"

    def test_summarise_is_hidden_where_there_is_no_transcript(
        self, history, monkeypatch
    ):
        import gui.history_window as hw

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
        import gui.history_window as hw

        monkeypatch.setattr(hw, "list_history_sessions", lambda: [])

        w = hw.HistoryWindow(lambda key, fallback="": fallback)
        try:
            assert w.entry_list.count() == 0
            assert w.detail.toPlainText() == ""
        finally:
            w.close()

    def test_unreadable_session_does_not_raise(self, history, monkeypatch):
        import gui.history_window as hw

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
        import gui.history_window as hw
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
        import gui.history_window as hw

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

        from gui.history_window import RowDelegate

        make, runs = batch
        w = make()
        item = w.entry_list.item(0)
        assert item.text().endswith(runs[0].source_name)  # stored whole
        assert item.data(RowDelegate.ELIDE_ROLE) == Qt.ElideMiddle

    def test_it_can_open_straight_onto_this_tab(self, qt_app, batch):
        # "Show in history" after a batch run must land on the run, not on the
        # session list — as it does in the Tk viewer.
        import gui.history_window as hw

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
        import gui.history_window as hw

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
    """main.py's single-instance guard.

    It used to show the CustomTkinter dialog whatever tree was asked for,
    which put Tk in a Qt-only process and set the process DPI awareness to
    per-monitor v1 before Qt could ask for the v2 context it wants.
    """

    @pytest.fixture
    def dialog(self, qt_app):
        from gui.already_running import AlreadyRunningDialog

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

    def test_the_entry_point_shows_this_dialog(self, monkeypatch):
        # It used to branch on main._QT_MODE, with a CustomTkinter dialog on
        # the other side. There is one dialog now, and the guard is that
        # main.py reaches THIS one — a second instance is otherwise announced
        # by nothing at all.
        import types

        import main

        calls = []
        monkeypatch.setitem(
            sys.modules,
            "gui.already_running",
            types.SimpleNamespace(
                show_already_running_dialog=lambda: calls.append(1) or True
            ),
        )
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
        import gui.update_banner as ub
        from utils.update_check import UpdateInfo

        ub.reset_cache()
        monkeypatch.setattr(
            ub,
            "check_for_update",
            lambda include_prereleases=False: UpdateInfo(
                version="9.9.9", url="https://example.invalid/r"
            ),
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
        monkeypatch.setattr(
            ub, "check_for_update", lambda *_: calls.append(1)
        )
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
        ub.check_for_update = lambda *_: calls.append(1)
        second = ub.UpdateBanner(lambda key, fallback="": fallback)
        try:
            second.start_check(True)
            assert calls == []
            assert "9.9.9" in second.label.text()
        finally:
            second.close()

    def test_the_channel_is_passed_through(self, banner, qt_app):
        made, ub = banner
        seen = []
        ub.check_for_update = lambda include_prereleases=False: seen.append(
            include_prereleases
        )
        made.start_check(True, True)
        _wait_for(qt_app, lambda: seen == [True])
        assert seen == [True]

    def test_toggling_the_channel_re_asks(self, banner, qt_app):
        # The cached answer belongs to one channel. Opting into pre-releases
        # must not replay the stable answer.
        made, ub = banner
        made.start_check(True)
        _wait_for(qt_app, lambda: not made.isHidden())
        seen = []
        ub.check_for_update = lambda include_prereleases=False: seen.append(
            include_prereleases
        )
        second = ub.UpdateBanner(lambda key, fallback="": fallback)
        try:
            second.start_check(True, True)
            _wait_for(qt_app, lambda: seen == [True])
            assert seen == [True]
        finally:
            second.close()


class TestSkippingAnUpdate:
    """"Skip this version" — the permanent half of dismissing the notice.

    The ✕ beside it hides the banner until the next launch ("not now"); this
    records the release and stays quiet for it for good ("not this one"). A user
    deliberately staying on their build had only one way to stop being asked
    before this: turn the whole check off, which then also hides the release
    they would have wanted.
    """

    @pytest.fixture
    def banner(self, qt_app, monkeypatch):
        import gui.update_banner as ub
        from utils.update_check import UpdateInfo

        ub.reset_cache()
        monkeypatch.setattr(
            ub,
            "check_for_update",
            lambda include_prereleases=False: UpdateInfo(
                version="2.0.0", url="https://example.invalid/r"
            ),
        )
        skipped: list[str] = []
        made = ub.UpdateBanner(
            lambda key, fallback="": fallback, on_skip=skipped.append
        )
        yield made, skipped, ub
        made.close()
        ub.reset_cache()

    def test_skipping_hides_it_and_reports_the_version(self, banner, qt_app):
        made, skipped, _ub = banner
        made.start_check(True)
        _wait_for(qt_app, lambda: not made.isHidden())
        made.action_btn.click()
        assert made.isHidden()
        # Reported out rather than written here: the banner knows nothing about
        # where preferences live.
        assert skipped == ["2.0.0"]

    def test_a_skipped_version_never_appears_again(self, banner, qt_app):
        made, _skipped, ub = banner
        # A fresh launch: same answer from GitHub, with the stored skip.
        ub.reset_cache()
        made.start_check(True, False, "2.0.0")
        _settle(qt_app)
        assert made.isHidden()

    def test_a_newer_release_still_gets_through(self, banner, qt_app):
        # Skipping 1.5.0 must not silence the update notice for good — that is
        # what turning the check off is for.
        made, _skipped, ub = banner
        ub.reset_cache()
        made.start_check(True, False, "1.5.0")
        _wait_for(qt_app, lambda: not made.isHidden())
        assert "2.0.0" in made.label.text()

    def test_an_older_release_than_the_skipped_one_stays_quiet(self, banner, qt_app):
        # Not an equality test: a patch published on an older branch, or the
        # same tag re-pointed, is the same news again.
        made, _skipped, ub = banner
        ub.reset_cache()
        made.start_check(True, False, "2.1.0")
        _settle(qt_app)
        assert made.isHidden()

    def test_nothing_is_skipped_by_default(self, banner, qt_app):
        # The guard that matters: is_newer_version(x, "") is False, so treating
        # an empty setting as a version would hide the banner from everyone.
        made, _skipped, _ub = banner
        made.start_check(True, False, "")
        _wait_for(qt_app, lambda: not made.isHidden())
        assert "2.0.0" in made.label.text()

    def test_the_panel_writes_the_skip_through_at_once(self, qt_app, monkeypatch):
        # Not deferred to the next _persist: the point of skipping is that the
        # notice is gone for good, and a panel that never gets round to
        # persisting would ask again on the next launch.
        import gui.control_panel as cp

        saved: list[str] = []
        p = _panel(monkeypatch)
        try:
            monkeypatch.setattr(
                cp, "save_settings", lambda s: saved.append(s.skipped_update_version)
            )
            p._on_update_skipped("3.1.4")
            assert p.settings.skipped_update_version == "3.1.4"
            assert saved == ["3.1.4"]
        finally:
            p.settings.skipped_update_version = ""
            p.close()


class TestReviewPrompt:
    """"How are you finding MinbarLive?", asked after three completed sessions.

    Three ways out, each meaning something different: clicking through to the
    form settles it for good, "Never show again" settles it for good without the
    form, and the ✕ is "not this time" — the counter resets and the question
    comes back after another three. So the only way to be asked twice is to keep
    saying "not now".
    """

    @pytest.fixture
    def review(self, qt_app):
        import gui.review_banner as rb

        decisions: list[tuple[int, bool]] = []
        made = rb.ReviewBanner(
            lambda key, fallback="": fallback,
            on_decision=lambda sessions, disabled: decisions.append(
                (sessions, disabled)
            ),
        )
        yield made, decisions, rb
        made.close()

    def test_it_stays_down_below_the_threshold(self, review):
        made, _decisions, rb = review
        for sessions in range(rb.PROMPT_AFTER_SESSIONS):
            made.maybe_show(sessions, False)
            assert made.isHidden(), f"showed after {sessions} sessions"

    def test_it_appears_on_the_third_completed_session(self, review):
        made, _decisions, rb = review
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS, False)
        assert not made.isHidden()
        assert made.label.text()

    def test_disabled_means_never(self, review):
        made, _decisions, rb = review
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS * 10, True)
        assert made.isHidden()

    def test_never_show_again_settles_it(self, review):
        made, decisions, rb = review
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS, False)
        made.action_btn.click()
        assert made.isHidden()
        assert decisions == [(0, True)]

    def test_the_close_button_only_resets_the_counter(self, review):
        # The whole difference from "Never show again": ✕ is "not this time".
        made, decisions, rb = review
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS, False)
        made.close_btn.click()
        assert made.isHidden()
        assert decisions == [(0, False)]
        # …and it really comes back, after another run of sessions.
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS, False)
        assert not made.isHidden()

    def test_the_counter_is_reset_not_left_at_the_threshold(self, review):
        # Left where it was, the prompt would be due again at the very next stop
        # — and a notice that reappears immediately is the one people learn to
        # turn off.
        made, decisions, rb = review
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS, False)
        made.close_btn.click()
        sessions, _disabled = decisions[0]
        assert sessions == 0

    def test_clicking_through_to_the_form_settles_it_too(self, review, monkeypatch):
        # Asking again after somebody has answered is the rude case.
        made, decisions, rb = review
        opened: list[str] = []
        monkeypatch.setattr(
            "gui.notice_banner.webbrowser.open", lambda url: opened.append(url)
        )
        made.maybe_show(rb.PROMPT_AFTER_SESSIONS, False)
        _click(made)
        assert opened == [rb.FEEDBACK_FORM_URL]
        assert decisions == [(0, True)]
        assert made.isHidden()

    def test_the_form_is_the_one_the_docs_link(self):
        # One form, one place to read the answers. If this ever diverges, half
        # the responses land somewhere nobody checks.
        from pathlib import Path

        import gui.review_banner as rb

        root = Path(__file__).parent.parent
        for name in ("README.md", "CONTRIBUTING.md", "docs/index.html"):
            text = (root / name).read_text(encoding="utf-8")
            assert rb.FEEDBACK_FORM_URL in text, f"{name} links a different form"


class TestThePanelCountsSessionsForTheReviewPrompt:
    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui.control_panel as cp

        p = _panel(monkeypatch)
        saved: list[tuple[int, bool]] = []
        monkeypatch.setattr(
            cp,
            "save_settings",
            lambda s: saved.append(
                (s.sessions_since_review_prompt, s.review_prompt_disabled)
            ),
        )
        p.settings.sessions_since_review_prompt = 0
        p.settings.review_prompt_disabled = False
        yield p, saved
        p.settings.sessions_since_review_prompt = 0
        p.settings.review_prompt_disabled = False
        p.close()

    def test_each_completed_session_counts_and_is_persisted(self, panel):
        p, saved = panel
        p._maybe_ask_for_a_review()
        assert p.settings.sessions_since_review_prompt == 1
        assert saved == [(1, False)]

    def test_the_prompt_appears_once_the_count_is_reached(self, panel, qt_app):
        from gui.review_banner import PROMPT_AFTER_SESSIONS

        p, _saved = panel
        for _ in range(PROMPT_AFTER_SESSIONS):
            p._maybe_ask_for_a_review()
        assert not p.review_banner.isHidden()

    def test_a_disabled_prompt_stops_even_the_counting(self, panel):
        # Nothing to count towards, so no pointless write on every stop.
        p, saved = panel
        p.settings.review_prompt_disabled = True
        p._maybe_ask_for_a_review()
        assert p.settings.sessions_since_review_prompt == 0
        assert saved == []

    def test_it_never_stacks_under_the_update_notice(self, panel, qt_app):
        # Two accent-soft strips above the cards read as a wall of nagging, and
        # the update offer is the one the user may act on today.
        from gui.review_banner import PROMPT_AFTER_SESSIONS

        p, _saved = panel
        p.show()
        p.update_banner.show_notice("Version 9.9.9", "https://example.invalid", "skip")
        _settle(qt_app)
        assert p.update_banner.isVisible()
        for _ in range(PROMPT_AFTER_SESSIONS):
            p._maybe_ask_for_a_review()
        assert p.review_banner.isHidden()
        # The count is kept rather than reset, so the question lands after the
        # next session instead of being lost. That is why the due test is >=.
        assert p.settings.sessions_since_review_prompt == PROMPT_AFTER_SESSIONS
        p.update_banner.hide_notice()
        p._maybe_ask_for_a_review()
        assert not p.review_banner.isHidden()

    def test_the_decision_is_written_through(self, panel):
        p, saved = panel
        p._on_review_decision(0, True)
        assert p.settings.review_prompt_disabled is True
        assert p.settings.sessions_since_review_prompt == 0
        assert saved == [(0, True)]


class TestBatchWindow:
    """The pipeline is batch/processor.py; these cover the window around it."""

    @pytest.fixture
    def batch(self, qt_app, monkeypatch):
        import sys
        import types

        import gui.batch_window as bw
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
        # _on_start asks for any missing provider key first, and ensure_keys
        # opens a REAL modal ApiKeyDialog when none is stored. On a developer
        # machine with keys in the keychain it returns immediately and the
        # tests below pass; on a runner with an empty keychain it blocks the
        # whole suite (it took CI's Windows job down with an access violation
        # in exactly this path). Patch it on gui.batch_window, not on
        # gui.api_keys — the import binds per module.
        monkeypatch.setattr(bw, "ensure_keys", lambda providers, texts, parent: True)
        # closeEvent asks what to do with a run in progress, which is another
        # real modal dialog. Recorded rather than shown, defaulting to the
        # non-destructive answer. A test sets calls["close_prompt"]["answer"]
        # to True (cancel the run), False (keep it) or None (dismissed — the
        # prompt's own close button, which must abandon the close entirely).
        close_prompt: dict = {"asked": 0, "answer": False, "kwargs": {}}

        def fake_ask(parent, title, message, **kwargs):
            close_prompt["asked"] += 1
            close_prompt["kwargs"] = kwargs
            return close_prompt["answer"]

        monkeypatch.setattr(bw, "ask_yes_no_or_dismiss", fake_ask)
        # The ffmpeg offer is a modal too; nothing in this fixture may reach a
        # real one. Declined, since no test here wants the download path.
        monkeypatch.setattr(bw, "ask_yes_no", lambda *a, **k: False)
        w = bw.BatchWindow(lambda k, f="": f, load_settings())
        calls["close_prompt"] = close_prompt
        yield w, calls
        w.close()

    def test_start_is_disabled_until_a_file_is_chosen(self, batch):
        w, _ = batch
        assert not w.start_btn.isEnabled()

    def test_it_opens_at_the_windowed_size(self, batch):
        # The port asked for a fixed 640x700 and towered over the Tk window.
        # Width is the Tk card's; the height follows the content and must not
        # be the sizeHint, which reserves a line a wrapped label never uses.
        import gui.batch_window as bw

        w, _ = batch
        assert w.width() == bw.BATCH_WINDOW_W
        assert w.height() == _sized_height(w)

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
        if _screen_capped(w):
            # Already as tall as this screen allows, so there is no growth to
            # measure. Nothing about the expander is broken.
            pytest.skip("screen too short: the window is capped either way")
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

    def test_the_error_carries_the_install_command_where_there_is_one(
        self, batch, monkeypatch
    ):
        # macOS and Linux are offered no download, so this message is the
        # only place the operator can find out what to do next (#38). Patched
        # rather than read off the host, so the branch is pinned on every OS.
        w, _ = batch
        monkeypatch.setattr(
            "utils.ffmpeg_download.ffmpeg_install_command",
            lambda: "brew install ffmpeg",
        )
        monkeypatch.setattr(w, "_offer_ffmpeg_download", lambda: False)
        w._on_ffmpeg_missing()
        assert w.status.objectName() == "status_error"
        assert "brew install ffmpeg" in w.status.text()

    def test_no_known_command_falls_back_to_the_plain_message(
        self, batch, monkeypatch
    ):
        w, _ = batch
        monkeypatch.setattr(
            "utils.ffmpeg_download.ffmpeg_install_command", lambda: None
        )
        monkeypatch.setattr(w, "_offer_ffmpeg_download", lambda: False)
        w._on_ffmpeg_missing()
        text = w.status.text()
        assert "ffmpeg" in text
        # Not the hint's shape — nothing to interpolate, so no stray colon
        # or an empty "install it and try again:" trailing off.
        assert "{command}" not in text and not text.rstrip().endswith(":")

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

    @staticmethod
    def _start_a_blocking_run(w, monkeypatch):
        """Start a run that is still going when close() lands."""
        import sys
        import types

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

    def test_closing_a_running_job_asks_first(self, batch, monkeypatch):
        # Closing a window is not a statement about the job: cancelling
        # silently threw away a run that could be twenty minutes in.
        w, calls = batch
        self._start_a_blocking_run(w, monkeypatch)
        calls["close_prompt"]["answer"] = True  # "Cancel run"
        w.close()
        assert calls["close_prompt"]["asked"] == 1, "closed without asking"
        assert w.worker.cancel_event.is_set()

    def test_closing_can_leave_the_run_in_the_background(self, batch, monkeypatch):
        # The .srt and the history entry are written on the worker thread, so
        # a run outliving this window still lands in the history's Batch tab.
        w, calls = batch
        self._start_a_blocking_run(w, monkeypatch)
        calls["close_prompt"]["answer"] = False  # "Keep running"
        w.close()
        assert calls["close_prompt"]["asked"] == 1
        assert not w.worker.cancel_event.is_set(), "the run was cancelled anyway"
        w.worker.cancel()  # don't leave the thread parked on the test's clock

    def test_the_close_prompt_names_both_actions_and_defaults_to_keeping(
        self, batch, monkeypatch
    ):
        # "Yes"/"No" cannot say which button throws the work away, and Return
        # must not be the one that does.
        w, calls = batch
        self._start_a_blocking_run(w, monkeypatch)
        w.close()
        kwargs = calls["close_prompt"]["kwargs"]
        assert kwargs["yes_text"] and kwargs["no_text"]
        assert kwargs["default_yes"] is False
        w.worker.cancel()

    def test_dismissing_the_prompt_leaves_the_window_open_and_running(
        self, batch, monkeypatch
    ):
        # The prompt's own close button is "never mind", not a third answer.
        # Closing the batch window on it would make an unlabelled ✕ decide the
        # fate of a run that may be twenty minutes in.
        w, calls = batch
        self._start_a_blocking_run(w, monkeypatch)
        calls["close_prompt"]["answer"] = None  # dismissed
        # close() reports whether the close was accepted; closeEvent.ignore()
        # makes it False, which is the window staying put.
        assert w.close() is False, "the batch window closed on a dismissed prompt"
        assert calls["close_prompt"]["asked"] == 1
        assert not w.worker.cancel_event.is_set(), "the run was cancelled anyway"
        w.worker.cancel()

    def test_closing_without_a_run_asks_nothing(self, batch):
        w, calls = batch
        w.close()
        assert calls["close_prompt"]["asked"] == 0


class TestAnnounceWindow:
    @pytest.fixture
    def announce(self, qt_app, monkeypatch):
        import gui.announce_window as aw
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
        import gui.announce_window as aw
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
        w, settings, _ = announce
        w.show()
        _settle(qt_app)
        heights = []
        capped = False
        for count in range(6):
            settings.announcement_history = [f"Nachricht {i}" for i in range(count)]
            w._refresh_lists()
            _settle(qt_app)
            heights.append(w.height())
            capped = capped or _screen_capped(w)
            assert w.height() == _sized_height(w), f"lagging at {count} entries"
        # Growing, then all the way back to where it started. A screen too
        # short to show the growth still proves the tracking above.
        if capped:
            pytest.skip("screen too short: the window is capped before it grows")
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
        from gui.window_size import SECONDARY_MAX_H

        w, settings, _ = announce
        settings.announcement_favorites = [f"Favorit {i}" for i in range(5)]
        settings.announcement_history = [f"Nachricht {i}" for i in range(20)]
        w.show()
        w._refresh_lists()
        _settle(qt_app)
        assert w._natural_height() > SECONDARY_MAX_H  # it would have grown past it
        # The cap that actually applies: SECONDARY_MAX_H, or a share of the
        # screen where the screen is shorter than that.
        assert w.height() == _sized_height(w)
        assert w.height() <= SECONDARY_MAX_H
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
        from gui.i18n import load_gui_translations

        de = load_gui_translations("de")
        return lambda key, fallback="": de.get(key, fallback)

    def test_the_buttons_speak_the_gui_language(self, qt_app, texts):
        from gui.dialogs import MessageDialog

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

        from gui.dialogs import MessageDialog

        dialog = MessageDialog(None, "T", "M", confirm=True, translate=texts)
        dialog.no_btn.click()
        assert dialog.result() != QDialog.Accepted
        dialog = MessageDialog(None, "T", "M", confirm=True, translate=texts)
        dialog.yes_btn.click()
        assert dialog.result() == QDialog.Accepted

    def test_a_destructive_confirm_defaults_to_no(self, qt_app, texts):
        from gui.dialogs import MessageDialog

        dialog = MessageDialog(
            None, "T", "M", confirm=True, default_yes=False, translate=texts
        )
        assert dialog.no_btn.isDefault()
        assert not dialog.yes_btn.isDefault()
        dialog.close()

    def test_a_destructive_confirm_is_not_painted_in_the_go_colour(
        self, qt_app, texts
    ):
        # #accent is the app's green "go": on a button that deletes something it
        # reads as the safe, recommended half of the choice. Opt-in, so the
        # ordinary confirms around it are unchanged.
        from gui.dialogs import MessageDialog

        ordinary = MessageDialog(None, "T", "M", confirm=True, translate=texts)
        assert ordinary.yes_btn.objectName() == "accent"
        deleting = MessageDialog(
            None, "T", "M", confirm=True, destructive=True, translate=texts
        )
        assert deleting.yes_btn.objectName() == "danger"
        for w in (ordinary, deleting):
            w.close()

    def test_the_severity_decides_the_glyph_colour(self, qt_app):
        from PySide6.QtWidgets import QLabel

        from gui.dialogs import MessageDialog

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
        from gui.dialogs import MessageDialog

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

        root = pathlib.Path(__file__).resolve().parents[1] / "gui"
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
        import gui.onboarding as ob
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
        import gui.onboarding as ob

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
        import gui.onboarding as ob

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
        import gui.onboarding as ob

        w, controller = wizard
        w.stack.setCurrentIndex(ob._DEVICE_STEP)
        assert controller.started, "no preview started on the device step"

    def test_leaving_the_step_releases_the_device(self, wizard):
        import gui.onboarding as ob

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
        import gui.onboarding as ob

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
        import gui.onboarding as ob

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
        import gui.onboarding as ob

        monkeypatch.setattr(ob, "save_settings", lambda s: None)
        w = ob.OnboardingWizard(qt_app)
        w.gui_lang_combo.setCurrentIndex(w.gui_lang_combo.findData("en"))
        yield w
        w.close()

    def test_later_steps_are_relabelled(self, wizard):
        from gui.i18n import load_gui_translations

        de = load_gui_translations("de")
        wizard.gui_lang_combo.setCurrentIndex(wizard.gui_lang_combo.findData("de"))
        # A label from a step the user has not reached yet.
        assert wizard.disclaimer_check.text() == de["wizard_disclaimer_accept"]
        assert wizard.title_label.text() == de["wizard_title"]
        assert wizard.next_btn.text() == de["wizard_next"]

    def test_the_theme_segment_is_relabelled(self, wizard):
        from gui.i18n import load_gui_translations
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

        from gui.pipeline_bridge import PipelineBridge

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

        from gui.pipeline_bridge import PipelineBridge

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
        import gui.api_keys as api_keys
        import providers.openai.client as client

        activated: list[str] = []
        monkeypatch.setattr(api_keys, "get_stored_api_key", lambda p: "sk-test-key")
        monkeypatch.setattr(client, "set_api_key", lambda k: activated.append(k))

        api_keys.activate_stored_keys()
        assert activated == ["sk-test-key"]

    def test_nothing_activated_when_no_key_is_stored(self, monkeypatch):
        import gui.api_keys as api_keys
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
    import gui.control_panel as cp

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
        import gui.announce_window as aw
        import gui.settings_window as sw

        # The batch window is absent: it persists nothing of its own.
        for module in (aw, sw):
            monkeypatch.setattr(module, "save_settings", lambda s: None)
        p = _panel(monkeypatch)
        p.resize(1200, 900)
        yield p
        p.close_secondary_windows()
        p.close()

    def test_the_three_windows_share_one_width(self, panel, qt_app):
        from gui.window_size import SECONDARY_WINDOW_W

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
        from gui.window_size import SECONDARY_MAX_H

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
        from gui.window_size import SECONDARY_MAX_H

        panel.open_batch()
        _settle(qt_app)
        w = panel._batch_window
        assert w._natural_height() <= SECONDARY_MAX_H
        assert w.height() == _sized_height(w)
        before = w.height()
        w.more.set_expanded(True)
        _settle(qt_app)
        if _screen_capped(w):
            pytest.skip("screen too short: the window is capped either way")
        assert w.height() > before

    def test_a_host_with_less_room_shrinks_the_window(self, qt_app):
        # The rule an in-app panel goes through: the host declares the room it
        # has and the window re-measures itself against it.
        from PySide6.QtCore import QSize
        from PySide6.QtWidgets import QWidget

        from gui.window_size import SECONDARY_WINDOW_W, content_size

        w = QWidget()
        assert content_size(w, 400) == QSize(SECONDARY_WINDOW_W, 400)
        w.host_max_size = QSize(360, 300)
        assert content_size(w, 400) == QSize(360, 300)
        w.deleteLater()


class TestSettingsWindowKeys:
    """The API-key card. Remove used to act on providers that had no key."""

    @pytest.fixture
    def settings_win(self, qt_app, monkeypatch):
        import gui.settings_window as sw

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


# A path on no real drive: every reset test reports it back, and nothing here
# may be tempted to touch a real one.
_RESET_DIR = Path("X:/nowhere/MinbarLive")


def _reset_ok(keys=("openai",)):
    from utils.factory_reset import ResetResult

    return ResetResult(
        data_dir=_RESET_DIR, data_dir_removed=True, keys_removed=list(keys)
    )


def _reset_failed(errors=("openai: the key is still in the keychain",)):
    from utils.factory_reset import ResetResult

    return ResetResult(
        data_dir=_RESET_DIR, data_dir_removed=False, errors=list(errors)
    )


class TestSettingsWindowFactoryReset:
    """The one irreversible control in the app.

    Nothing here calls the real ``factory_reset`` — it deletes the developer's
    own app-data folder and keychain entries. ``utils.factory_reset`` is tested
    on its own in tests/test_factory_reset.py; this covers the window around it.
    """

    @pytest.fixture
    def reset_win(self, qt_app, monkeypatch):
        """The window, plus a recorder. Two queues drive the handler's loop:

        ``answers`` is what each dialog returns and ``results`` what each
        ``factory_reset()`` call hands back. Both keep repeating their last
        entry once exhausted, so a test states only the turns it cares about
        and an unexpected extra round cannot hang the run.
        """
        import gui.settings_window as sw

        monkeypatch.setattr(sw, "save_settings", lambda s: None)
        p = _panel(monkeypatch)
        p.open_settings()
        win = p._settings_window

        calls = {
            "reset": 0,
            "quit": 0,
            "messages": [],
            "asked": [],
            "answers": [False],
            "results": [_reset_ok()],
        }

        def _next(queue):
            return queue.pop(0) if len(queue) > 1 else queue[0]

        def fake_reset():
            calls["reset"] += 1
            return _next(calls["results"])

        # Patched on utils.factory_reset, because the handler imports it inside
        # the function — the name is looked up at call time, not at import.
        import utils.factory_reset as fr

        monkeypatch.setattr(fr, "factory_reset", fake_reset)
        monkeypatch.setattr(
            sw,
            "show_message",
            lambda parent, title, message, **kw: calls["messages"].append(
                (title, message)
            ),
        )

        def fake_ask(parent, title, message, **kw):
            calls["asked"].append((title, message, kw))
            return _next(calls["answers"])

        monkeypatch.setattr(sw, "ask_yes_no", fake_ask)

        class FakeApp:
            @staticmethod
            def quit():
                calls["quit"] += 1

            @staticmethod
            def sendPostedEvents(*_a):  # noqa: N802 - mirrors the Qt API
                pass

        monkeypatch.setattr(sw, "QApplication", FakeApp)

        yield win, calls
        p.close_secondary_windows()
        p.close()

    def test_the_button_is_painted_as_destructive(self, reset_win):
        win, _ = reset_win
        # #danger, so it does not read as the next ordinary button in a column
        # of them. The sheet already carries the rule.
        assert win.reset_btn.objectName() == "danger"

    def test_a_running_session_blocks_it_before_anything_is_asked(
        self, reset_win
    ):
        # A live pipeline is writing history and holds the recordings directory
        # open, so rmtree would fail halfway through.
        win, calls = reset_win
        win._panel._running = True
        win._on_factory_reset()
        assert calls["reset"] == 0
        assert calls["asked"] == []
        assert calls["quit"] == 0
        assert len(calls["messages"]) == 1

    def test_connecting_blocks_it_too(self, reset_win):
        # _starting, not only _running: the session is coming up and the
        # provider handshake is already writing.
        win, calls = reset_win
        win._panel._running = False
        win._panel._starting = True
        win._on_factory_reset()
        assert calls["reset"] == 0

    def test_declining_the_confirmation_deletes_nothing(self, reset_win):
        win, calls = reset_win
        calls["answers"] = [False]
        win._on_factory_reset()
        assert calls["reset"] == 0
        assert calls["quit"] == 0

    def test_the_confirmation_cannot_be_deleted_away_with_return(self, reset_win):
        # default_yes=False and named buttons: on an irreversible delete,
        # Return must not press the destructive one and "Yes"/"No" makes the
        # user guess which button loses their data (gui/dialogs.py).
        win, calls = reset_win
        calls["answers"] = [False]
        win._on_factory_reset()
        _title, _msg, kw = calls["asked"][0]
        assert kw["default_yes"] is False
        assert kw["yes_text"] and kw["no_text"]
        assert kw["yes_text"] != kw["no_text"]
        # …and it is not painted in the accent green, which would mark deleting
        # everything as the recommended half of the choice.
        assert kw["destructive"] is True

    def test_accepting_resets_reports_the_path_and_quits(self, reset_win):
        win, calls = reset_win
        calls["answers"] = [True]
        win._on_factory_reset()
        assert calls["reset"] == 1
        assert calls["quit"] == 1
        _title, message = calls["messages"][0]
        # The report names what went — that is the whole point of showing one.
        # str(Path), not the literal: on Windows it comes back backslashed.
        assert str(_RESET_DIR) in message
        assert "openai" in message

    def test_a_reset_with_no_stored_keys_says_so(self, reset_win):
        win, calls = reset_win
        calls["answers"] = [True]
        calls["results"] = [_reset_ok(keys=())]
        win._on_factory_reset()
        _title, message = calls["messages"][0]
        # Not an empty gap where the provider list belongs — a blank there
        # reads as a truncated message. Asserted through the window's own
        # translation: the suite runs in whatever GUI language the machine is
        # set to, so an English literal here only passes on an English box.
        assert win._t("reset_done_no_keys", "none were stored") in message
        assert str(_RESET_DIR) in message

    def test_a_failed_reset_names_the_error_and_offers_another_go(self, reset_win):
        win, calls = reset_win
        # Confirm, then "Close MinbarLive" at the failure dialog.
        calls["answers"] = [True, False]
        calls["results"] = [_reset_failed()]
        win._on_factory_reset()
        _title, message, kw = calls["asked"][-1]
        assert "still in the keychain" in message
        assert str(_RESET_DIR) in message
        # The retry is the offer, and it is the default — the destructive
        # decision was already made at the confirmation, this is the recovery.
        assert kw["default_yes"] is True
        assert not kw.get("destructive")
        assert kw["yes_text"] != kw["no_text"]
        # Quits once the user gives up: every provider key is already gone, so
        # what is left running is an install whose storage was pulled out from
        # under it.
        assert calls["quit"] == 1

    def test_try_again_really_runs_the_reset_again(self, reset_win):
        win, calls = reset_win
        # Confirm, then Try again; the second attempt succeeds.
        calls["answers"] = [True, True]
        calls["results"] = [_reset_failed(), _reset_ok()]
        win._on_factory_reset()
        assert calls["reset"] == 2
        assert calls["quit"] == 1
        # The second run succeeded, so the last thing shown is the report and
        # not another failure dialog.
        assert len(calls["asked"]) == 2
        _title, message = calls["messages"][-1]
        assert str(_RESET_DIR) in message

    def test_the_retry_loop_ends_when_the_user_stops_retrying(self, reset_win):
        # Guards the shape of the loop itself: it is a `while True`, so a
        # failure path that never consults the user again is an app that cannot
        # be closed.
        win, calls = reset_win
        calls["answers"] = [True, True, True, False]
        calls["results"] = [_reset_failed()]
        win._on_factory_reset()
        # The confirmation, then two "Try again"s, then "Close MinbarLive".
        assert calls["reset"] == 3
        assert calls["quit"] == 1

    def test_a_translation_with_a_broken_placeholder_still_reports(
        self, reset_win, monkeypatch
    ):
        win, calls = reset_win
        calls["answers"] = [True]
        # A translator who wrote {pfad} instead of {path}: the message must
        # still name the folder rather than raising inside the handler. Set on
        # the panel's table, which is what _t reads.
        win._panel.texts["reset_done_text"] = "Deleted: {pfad}"
        win._on_factory_reset()
        _title, message = calls["messages"][0]
        assert str(_RESET_DIR) in message


class TestIntegratedWindows:
    """Secondary windows presented inside the control panel (window_style)."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui.announce_window as aw
        import gui.settings_window as sw

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

        from gui.modal_host import MIN_PANEL_H, MIN_PANEL_W, clamped_panel_size

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

        from gui.theme import apply_theme

        # The panel is only styled if there IS a sheet: nothing applies one on
        # the way to a ControlPanel, only gui/app.py does.
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
        import gui.settings_window as sw
        from gui.settings_window import _STYLE_SEGMENTS

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
            "import gui.control_panel as cp\n"
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
        import gui.control_panel as cp

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
        import gui.control_panel as cp

        p, controller, _calls = panel
        p._running = True
        p.settings.auto_stop_inactivity = True
        controller.idle = cp.AUTO_STOP_INACTIVITY_SECONDS + 1
        stopped: list[bool] = []
        monkeypatch.setattr(type(p), "on_stop", lambda self: stopped.append(True))
        p._check_inactivity()
        assert stopped == [True]

    def test_busy_session_is_left_alone(self, panel, monkeypatch):
        import gui.control_panel as cp

        p, controller, _calls = panel
        p._running = True
        p.settings.auto_stop_inactivity = True
        controller.idle = cp.AUTO_STOP_INACTIVITY_SECONDS - 1
        stopped: list[bool] = []
        monkeypatch.setattr(type(p), "on_stop", lambda self: stopped.append(True))
        p._check_inactivity()
        assert stopped == []

    def test_the_checkbox_actually_disables_it(self, panel, monkeypatch):
        import gui.control_panel as cp

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
        import gui.control_panel as cp

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
        import gui.control_panel as cp

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

    def test_swapping_the_languages_reconnects_too(self, panel, qt_app):
        """Reported live: the ⇄ button changed the SOURCE on a running stream
        without reopening it, so the engine kept transcribing the previous
        language — German speech came back written in Arabic script — while the
        target, read per translation call, changed at once.

        _on_source_changed cannot cover this: _refresh_source_combo blocks the
        combo's signals across its repopulate, so the handler never runs.
        """
        p, controller = panel
        p._running = True
        p.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        p.settings.source_language = "German"
        p.settings.target_language = "English"
        p._on_swap_languages()
        self._drive(qt_app, controller, p)
        assert p.settings.source_language == "English"
        assert controller.restarts, "the swap left the stream on the old language"

    def test_swapping_while_stopped_does_not_reconnect(self, panel):
        p, controller = panel
        p._running = False
        p.settings.pipeline_mode = PIPELINE_MODE_STREAMING
        p.settings.source_language = "German"
        p.settings.target_language = "English"
        p._on_swap_languages()
        assert p.settings.source_language == "English"  # the swap still happens
        assert controller.restarts == []

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

    Side by side it needs ~895 px. As a separate window the WM held it there,
    but as an in-app panel it is resized as a child widget, which bypasses that
    minimum — inside a control panel under ~620 px wide, Copy and Save… were
    laid out past its right edge and could not be clicked at all. The Tk viewer
    reflows here instead (gui/history_view.py _layout_history_responsive).
    """

    @pytest.fixture
    def history(self, qt_app, monkeypatch):
        import gui.history_window as hw

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

    def test_the_breakpoints_are_measured_not_assumed(self, history):
        # Both are the sum of the list, the margins and four TRANSLATED labels,
        # so hard-coded numbers would be wrong in some GUI language. They also
        # have to stay in order, or an arrangement becomes unreachable.
        assert history._one_row_min_w > 0
        assert history.minimumWidth() < history._side_by_side_min_w
        assert history._side_by_side_min_w < history._one_row_min_w

    def test_it_stays_side_by_side_while_there_is_room(self, history, qt_app):
        from PySide6.QtCore import Qt

        history.resize(history._one_row_min_w + 120, 560)
        _settle(qt_app)
        assert history.splitter.orientation() == Qt.Horizontal
        # One row: Summarise, a stretch, and the three secondary actions.
        assert history._action_bottom.count() == 0

    def test_the_actions_wrap_before_the_panes_stack(self, history, qt_app):
        from PySide6.QtCore import Qt

        # The middle arrangement, and the reason there are two breakpoints:
        # wrapping the buttons is the cheap concession, so it is spent first
        # and the panes keep sitting side by side well past the width one row
        # of actions needs.
        history.resize(history._one_row_min_w - 60, 560)
        _settle(qt_app)
        assert history.splitter.orientation() == Qt.Horizontal
        assert history._action_top.count() == 1
        assert history._action_bottom.count() == 3

    def test_narrow_stacks_the_panes_and_wraps_the_actions(self, history, qt_app):
        from PySide6.QtCore import Qt

        history.resize(history._side_by_side_min_w - 60, 560)
        _settle(qt_app)
        assert history.splitter.orientation() == Qt.Vertical
        # Summarise alone above; Delete / Copy / Save… sharing the row below.
        assert history._action_top.count() == 1
        assert history._action_bottom.count() == 3

    def test_it_is_still_side_by_side_where_the_host_is_two_column(
        self, history, qt_app
    ):
        from PySide6.QtCore import Qt

        from gui.card_grid import _COL2_MIN_W
        from gui.modal_host import PANEL_FRACTION

        # The viewer must not stack before the control panel behind it drops to
        # one column. The narrowest an in-app panel can be while the control
        # panel still lays its cards out in two is _COL2_MIN_W * PANEL_FRACTION;
        # anything above that has to stay side by side.
        history.resize(int(_COL2_MIN_W * PANEL_FRACTION), 560)
        _settle(qt_app)
        assert history.splitter.orientation() == Qt.Horizontal

    def test_it_can_be_made_narrower_than_the_wide_layout_needs(self, history, qt_app):
        # The regression this guards: with the window's floor left at the wide
        # arrangement's minimum, it could never be dragged narrow enough to
        # REACH the mode that lowers it.
        history.resize(400, 420)
        _settle(qt_app)
        assert history.width() == 400

    def test_every_action_stays_inside_the_window(self, history, qt_app):
        # Also the guard on _side_by_side_min_w being derived rather than
        # measured: if that arithmetic ever put the stacking breakpoint too
        # low, the middle arrangement would be held past the width it fits in
        # and the buttons would go over the edge here.
        for width in (900, 880, 760, 620, 611, 609, 500, 420):
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
        import gui.history_window as hw

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


class TestHistoryLayoutIsTabIndependent:
    """Switching tab must not move the layout.

    Summarise is hidden on the cost and log tabs, and a hidden widget counts as
    empty to a layout — so the right pane asked for 168px less there. That
    difference reached the operator twice: the session list took whatever the
    right pane's minimum left it (225px on Verlauf, 280px on Kosten at an 820px
    window), and the breakpoint between side-by-side and stacked moved with it.
    """

    @pytest.fixture
    def viewer(self, qt_app, monkeypatch):
        import gui.history_window as hw
        from utils.history import BatchRun, HistorySession

        monkeypatch.setattr(hw, "list_history_sessions", lambda: [
            HistorySession(
                date="2026-08-05", path="a.txt", start_time="12:35",
                end_time="12:36", duration_minutes=1, active_seconds=53,
                language_pair="AR → GE", entry_count=18, has_summary=False,
            )
        ])
        monkeypatch.setattr(hw, "list_batch_runs", lambda: [
            BatchRun(
                date="2026-07-19", time="01:45", source_name="talk.mp3",
                path="b.txt", duration_minutes=10, active_seconds=600,
                language_pair="AU → GE", entry_count=83, has_summary=False,
                formats=["srt", "txt"],
            )
        ])
        monkeypatch.setattr(hw, "list_cost_sessions", lambda: [{
            "id": "s1",
            "started_at": "2026-08-05T10:35:00+00:00",
            "ended_at": "2026-08-05T10:36:00+00:00",
            "total_cost_usd": "0.0337",
            "fully_priced": True,
            "providers": {"openai": {
                "requests": 18, "cost_usd": "0.0337",
                "fully_priced": True, "models": {},
            }},
        }])
        monkeypatch.setattr(hw, "list_log_files", lambda: [
            type("LogFile", (), {"date": "2026-08-05", "path": "l.log",
                                 "size_kb": 243})()
        ])
        monkeypatch.setattr(hw, "parse_history_file", lambda _p: [])
        monkeypatch.setattr(hw, "read_summary", lambda _p: None)
        monkeypatch.setattr(hw, "read_batch_languages", lambda _p: ("Arabic", "German"))

        made = []

        def _open(initial_tab="history", width=820):
            window = hw.HistoryWindow(
                lambda key, fallback="": fallback, initial_tab=initial_tab
            )
            window.resize(width, 600)
            window.show()
            _settle(qt_app)
            made.append(window)
            return window

        yield _open
        for window in made:
            window.close()

    def test_the_list_keeps_its_width_across_the_tabs(self, viewer, qt_app):
        import gui.history_window as hw

        # 820px: wide enough to stay side by side, narrow enough that the right
        # pane's minimum used to eat into the list on the two Summarise tabs.
        window = viewer()
        widths = []
        for index in range(4):
            window._tab_group.button(index).click()
            _settle(qt_app)
            widths.append(window.entry_list.width())
        assert widths == [hw.LIST_W] * 4

    def test_a_viewer_opened_on_any_tab_agrees_on_the_width(self, viewer):
        import gui.history_window as hw

        # Clicking through one window and opening four are different paths: the
        # splitter is re-pinned on resize, so only a fresh window shows the
        # width a tab would have chosen for itself.
        assert [
            viewer(initial_tab=tab).entry_list.width()
            for tab in ("history", "batch", "cost", "logs")
        ] == [hw.LIST_W] * 4

    def test_neither_breakpoint_depends_on_the_tab(self, viewer):
        breakpoints = {
            tab: (window._one_row_min_w, window._side_by_side_min_w)
            for tab in ("history", "batch", "cost", "logs")
            if (window := viewer(initial_tab=tab)) is not None
        }
        assert len(set(breakpoints.values())) == 1, breakpoints

    def test_reserving_summarise_leaves_the_other_actions_where_they_were(
        self, viewer, qt_app
    ):
        # The space Summarise keeps while hidden sits where the stretch after it
        # would have been, so nothing else may move. If it did, the fix would be
        # trading one visible jump for another.
        window = viewer()
        positions = []
        for index in range(4):
            window._tab_group.button(index).click()
            _settle(qt_app)
            positions.append(
                window.delete_btn.mapTo(window, window.delete_btn.rect().topLeft()).x()
            )
        assert len(set(positions)) == 1, positions

    def test_stacked_it_gives_the_reserved_space_back(self, viewer, qt_app):
        # Narrow, Summarise is alone on its own row — reserving it on a tab that
        # hides it would leave an empty row above the other three.
        window = viewer(initial_tab="cost")
        window.resize(window._side_by_side_min_w - 60, 560)
        _settle(qt_app)
        assert window._narrow
        assert not window.summarise_btn.sizePolicy().retainSizeWhenHidden()

    def test_the_list_floor_is_raised_only_side_by_side(self, viewer, qt_app):
        import gui.history_window as hw

        # Side by side the floor is what stops a tab's action bar taking from
        # the list. Stacked it has to come back down: the window's own minimum
        # is measured from THAT arrangement, so LIST_W there would raise the
        # floor by 130px and undo the mode that exists to lower it.
        window = viewer()
        assert not window._narrow
        assert window.splitter.widget(0).minimumWidth() == hw.LIST_W
        window.resize(window._side_by_side_min_w - 60, 560)
        _settle(qt_app)
        assert window._narrow
        assert window.splitter.widget(0).minimumWidth() == hw.LIST_W_MIN


class TestBatchRunLock:
    """Everything that decides what a run produces is frozen while one runs.

    The worker was handed its arguments at Start, so a change made mid-run
    cannot reach it — it would only leave the window describing a job it is not
    running. The Tk window locks the same set (gui/batch_view.py
    _batch_option_combos).
    """

    @pytest.fixture
    def batch(self, qt_app):
        import gui.batch_window as bw
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
        from gui.batch_window import _MEDIA_EXTENSIONS

        tk_offered = {
            "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "mp4", "mkv",
            "mov", "webm", "avi", "m4v", "wmv", "flv", "ts", "mpg", "mpeg",
        }
        assert tk_offered <= set(_MEDIA_EXTENSIONS)

    def test_the_patterns_are_wildcards_qt_understands(self):
        from gui.batch_window import _MEDIA_EXTENSIONS

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

        from gui.theme import app_families

        missing = [f for f in app_families() if not QFontDatabase.hasFamily(f)]
        assert not missing, f"requesting fonts this machine lacks: {missing}"

    def test_the_application_font_is_the_platform_stack(self, qt_app):
        from gui.theme import app_families, apply_theme

        apply_theme(qt_app, "light")
        assert qt_app.font().families() == app_families()

    def test_arabic_is_measured_with_a_family_that_has_arabic(self, qt_app):
        from gui.fonts import arabic_families, source_font, subtitle_font

        arabic = "بل لا يشعرون"
        assert subtitle_font(40, text=arabic).families() == arabic_families()
        assert source_font(40, arabic).families() == arabic_families()

    def test_latin_keeps_the_interface_stack(self, qt_app):
        from gui.fonts import source_font, subtitle_font, ui_families

        german = "Vielmehr merken sie es nicht."
        assert subtitle_font(40, text=german).families() == ui_families()
        assert source_font(40, german).families() == ui_families()

    def test_an_honorific_does_not_make_a_german_line_arabic(self, qt_app):
        # ﷺ/ﷻ are Arabic-block code points the translator inserts into
        # otherwise-Latin lines; classing those as Arabic would restyle them.
        from gui.fonts import subtitle_font, ui_families

        assert subtitle_font(40, text="dass Allah ﷻ es sagt.").families() == (
            ui_families()
        )

    def test_the_stylesheet_asks_for_no_family_this_machine_lacks(self, qt_app):
        """The same rule, for the families the STYLESHEET names.

        Two were left hardcoded there: a ``sans-serif`` tail on the base rule
        and ``"Consolas", "Menlo", monospace`` on the log panel. Qt's
        stylesheet parser does not implement the CSS generics — those are
        family names to it — so macOS looked for a family called "Sans-serif",
        populated its whole alias table hunting for it, and printed the cost.
        The warning only ever names the FIRST miss, which is why this walks
        every family in the sheet rather than the one that was reported.
        """
        import re

        from PySide6.QtGui import QFontDatabase

        from gui.theme import stylesheet

        for family in re.findall(r"font-family:\s*(.+?);", stylesheet("dark")):
            for name in [n.strip().strip('"') for n in family.split(",")]:
                assert QFontDatabase.hasFamily(name), (
                    f"the stylesheet asks for {name!r}, which this machine lacks"
                )


class TestInkOverhang:
    """Qt reports the metrics of the family it was ASKED for and paints missing
    glyphs from whichever family has them. Arabic drawn in a Latin-only family
    therefore measures against a descent its glyphs go straight through, and
    the original overlapped its translation (seen on Linux, where the UI family
    has no Arabic at all)."""

    def test_a_blocks_height_covers_the_ink_it_draws(self, overlay):
        from PySide6.QtGui import QFontMetrics

        from gui.fonts import subtitle_font

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
        from gui.fonts import subtitle_font
        from gui.subtitle_window import SubtitleWindow

        font = subtitle_font(40, text="Vielmehr merken sie es nicht.")
        monkeypatch.setattr(SubtitleWindow, "_ink", staticmethod(lambda t, f: (0, 0)))
        flat = w._layout_text("Vielmehr merken sie es nicht.", font)[1]
        monkeypatch.setattr(SubtitleWindow, "_ink", staticmethod(lambda t, f: (0, 12)))
        assert w._layout_text("Vielmehr merken sie es nicht.", font)[1] == flat + 12


class TestPairInkGap:
    """A source line and its translation are one utterance and have to read as
    one. The join was closed at the top only — the translation's blank band —
    and the source's UNUSED DESCENT was left in: Noto Sans Arabic reserves 35 px
    of descent at 48 px text and an Arabic line draws 19 into it, so on Linux
    the original floated 20 px above its translation where Segoe UI, whose
    Arabic ink reaches its descent line exactly, put it at 4."""

    @staticmethod
    def _ink_gap(w, block) -> int:
        """Distance from the source's lowest ink to the translation's highest."""
        from PySide6.QtGui import QFontMetrics

        trans_font, src_font = w._block_fonts(block)
        fm_s, fm_t = QFontMetrics(src_font), QFontMetrics(trans_font)
        source_h = w._measure(block.source, src_font)
        source_ink_bottom = fm_s.ascent() + fm_s.tightBoundingRect(block.source).bottom()
        trans_ink_top = (
            source_h
            + w._pair_gap(block)
            + fm_t.ascent()
            + fm_t.tightBoundingRect(block.translation).top()
        )
        return trans_ink_top - source_ink_bottom

    def _block(self, w):
        from gui.subtitle_window import Block

        return Block(translation=PAIRS[0][0], source=PAIRS[0][1])

    def test_a_reserved_descent_the_source_never_draws_into_is_closed(
        self, overlay, monkeypatch
    ):
        # Forced, because no Windows font reserves one: Segoe UI's Arabic ink
        # reaches its descent line, so the slack there is genuinely zero.
        from gui.subtitle_window import SubtitleWindow

        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        block = self._block(w)
        _trans, src_font = w._block_fonts(block)
        here = SubtitleWindow._descent_slack(block.source, src_font)
        before = w._pair_gap(block)
        monkeypatch.setattr(
            SubtitleWindow, "_descent_slack", staticmethod(lambda text, font: 16)
        )
        # Whatever this machine's own slack was, the gap moves by the difference.
        assert w._pair_gap(block) == before + here - 16

    def test_the_slack_is_what_the_font_reserves_and_does_not_use(self, overlay):
        from PySide6.QtGui import QFontMetrics

        from gui.fonts import source_font
        from gui.subtitle_window import SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)
        text = PAIRS[0][1]
        font = source_font(48, text)
        fm = QFontMetrics(font)
        expected = max(0, fm.descent() - fm.tightBoundingRect(text).bottom())
        assert SubtitleWindow._descent_slack(text, font) == expected

    def test_a_font_that_uses_its_whole_descent_is_left_alone(self, overlay):
        # The Windows case: nothing about the layout changes where the ink
        # already reaches the descent line.
        from PySide6.QtGui import QFontMetrics

        from gui.fonts import source_font
        from gui.subtitle_window import SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)
        text = PAIRS[0][1]
        font = source_font(48, text)
        fm = QFontMetrics(font)
        if fm.tightBoundingRect(text).bottom() >= fm.descent():
            assert SubtitleWindow._descent_slack(text, font) == 0

    def test_the_pair_ends_up_ink_to_ink(self, overlay):
        """What the whole mechanism is for: the gap the constant names.

        PAIR_GAP plus the clearance _reclaim holds back, and nothing else —
        no font's reserved-but-unused space on either side of the join.
        """
        from gui.subtitle_window import _STACK_INK_GAP_EM, PAIR_GAP

        w = overlay(SUBTITLE_MODE_REALTIME, bilingual_mode=True)
        block = self._block(w)
        _trans, src_font = w._block_fonts(block)
        clearance = round(_STACK_INK_GAP_EM * src_font.pixelSize())
        assert abs(self._ink_gap(w, block) - (PAIR_GAP + clearance)) <= 2


class TestTransparentStaticGeometry:
    """The height slider has two meanings, and transparent static is the one
    where it stops being a height.

    Static draws ONE block, sized to whatever was just said. A band shorter
    than that block had nowhere to put the overflow — the first lines were cut
    off at the top edge and the last ran under the disclaimer pill and off the
    bottom of the screen — and only the feed modes shift as they fill, so
    nothing rescued it. With no backdrop of its own to lose, transparent static
    takes the whole monitor instead, and the slider moves the content up it.
    """

    @staticmethod
    def _transparent(overlay, **kwargs):
        kwargs.setdefault("bilingual_mode", True)
        return overlay(SUBTITLE_MODE_STATIC, transparent_static=True, **kwargs)

    def test_transparent_static_ignores_the_slider_as_a_height(self, overlay):
        w = self._transparent(overlay, window_height_percent=23)
        assert w._effective_height_percent() == 100

    def test_opaque_static_is_still_a_band(self, overlay):
        """Making THIS full height would wash the whole screen at the backdrop
        opacity instead of the bottom strip the operator asked for."""
        w = overlay(
            SUBTITLE_MODE_STATIC, transparent_static=False, window_height_percent=23
        )
        assert w._effective_height_percent() == 23
        assert w._static_lift() == 0, "a band has nothing to lift"

    @pytest.mark.parametrize(
        "mode", [SUBTITLE_MODE_REALTIME, SUBTITLE_MODE_CONTINUOUS]
    )
    def test_a_feed_mode_still_obeys_it(self, overlay, mode):
        w = overlay(mode, window_height_percent=23, transparent_static=True)
        assert w._effective_height_percent() == 23
        assert w._static_lift() == 0

    def test_the_lift_does_not_reach_the_band(self, overlay):
        """The reported bug: dragging Abstand von unten to 0 and unticking
        Transparent left a 0%-tall overlay — one pixel, subtitles and footer
        gone, and only a drag of the height slider brought them back. The two
        meanings had one stored field and do not even share a floor."""
        w = self._transparent(
            overlay, static_lift_percent=0, window_height_percent=40
        )
        assert w._static_lift() == 0, "this value is not the one that broke it"
        w.set_transparent_static(False)
        assert w._effective_height_percent() == 40

    def test_a_band_is_never_thinner_than_it_can_hold(self, overlay):
        """Still clamped where the number becomes pixels, although the lift can
        no longer arrive here — a hand-edited settings.json is enough."""
        w = overlay(SUBTITLE_MODE_STATIC, window_height_percent=0)
        assert w._effective_height_percent() == WINDOW_HEIGHT_PERCENT_MIN

    def test_the_setting_is_kept_for_when_the_mode_changes_back(self, overlay):
        # Ignored, not overwritten: leaving transparent static has to restore
        # the band the operator chose, not reset it to full screen.
        w = self._transparent(overlay, window_height_percent=23)
        w.set_subtitle_mode(SUBTITLE_MODE_REALTIME)
        assert w._effective_height_percent() == 23

    def test_the_window_is_re_placed_when_its_height_changes_meaning(self, overlay):
        # The window height changes although the setting did not, so nothing
        # else would have triggered the placement — by either route into it.
        w = overlay(SUBTITLE_MODE_REALTIME, window_height_percent=23)
        placed: list[str] = []
        w._apply_geometry = lambda: placed.append(f"{w._mode}/{w._transparent_static}")
        w.set_transparent_static(True)  # a feed mode: transparent means nothing
        assert placed == []
        w.set_subtitle_mode(SUBTITLE_MODE_STATIC)
        assert placed == ["static/True"]
        w.set_transparent_static(False)  # now it decides band vs whole monitor
        assert placed == ["static/True", "static/False"]

    def test_a_block_taller_than_the_band_no_longer_spills(self, overlay):
        # The reported symptom, as geometry: at 23% a long bilingual block was
        # taller than the whole content area.
        w = self._transparent(overlay)
        screen = w._screen().geometry()
        w.resize(screen.width(), int(screen.height() * 23 / 100))
        block = _long_block()
        assert w._measure_block(block) > w._content_height(), "not a spilling case"
        w.resize(screen.width(), screen.height())
        assert w._measure_block(block) <= w._content_height()

    # ── fitting the text to a band ───────────────────────────────────────
    def test_room_enough_keeps_the_configured_size(self, overlay):
        # The fit is a rescue, not a policy: whenever the block already fits,
        # the operator's chosen font size is used untouched.
        w = overlay(
            SUBTITLE_MODE_STATIC,
            bilingual_mode=True,
            transparent_static=False,
            window_height_percent=100,
        )
        block = _long_block()
        assert w._measure_at(block, 1.0) <= w._content_height(), "not a fitting case"
        assert w._static_fit_scale(block) == 1.0
        assert w._fit_scale == 1.0, "the scale leaked out of the measurement"

    def test_a_short_band_shrinks_the_text_into_it(self, overlay):
        w = overlay(
            SUBTITLE_MODE_STATIC, bilingual_mode=True, transparent_static=False
        )
        screen = w._screen().geometry()
        w.resize(screen.width(), int(screen.height() * 0.2))
        block = _long_block()
        assert w._measure_at(block, 1.0) > w._content_height(), "not a fitting case"
        scale = w._static_fit_scale(block)
        assert scale < 1.0
        assert w._measure_at(block, scale) <= w._content_height()

    def test_it_finds_the_LARGEST_size_that_fits(self, overlay):
        """A search that only ever shrinks stops at the first size that happens
        to fit and leaves the band half empty — the linear estimate undershoots,
        because wrapping moves in whole words. Text smaller than it needs to be
        is a legibility bug, not a cosmetic one."""
        w = overlay(
            SUBTITLE_MODE_STATIC, bilingual_mode=True, transparent_static=False
        )
        from gui.subtitle_window import _FIT_MIN_SCALE

        # Absolute heights rather than a share of this machine's screen, so the
        # band under test is the same on every box (see the sibling test).
        checked = 0
        for height in (92, 138, 192, 400):
            w.resize(1024, height)
            block = _long_block()
            scale = w._static_fit_scale(block)
            if scale >= 1.0:
                continue  # it already fits at the configured size
            available = w._content_height()
            if w._measure_at(block, _FIT_MIN_SCALE) > available:
                # Documented at _static_fit_scale: below the floor the shrink
                # stops being a fix, so the floor is returned even though it
                # does not fit. Nothing to assert about a largest size here.
                assert scale == _FIT_MIN_SCALE, height
                continue
            checked += 1
            assert w._measure_at(block, scale) <= available, height
            # Anything meaningfully larger must NOT fit, or a bigger size was
            # available and went unused.
            bigger = min(1.0, scale * 1.15)
            assert w._measure_at(block, bigger) > available, (
                f"{height}px: {scale:.2f} fits and so does {bigger:.2f} — "
                f"the band was left {available - w._measure_at(block, scale)}px empty"
            )
        assert checked, "every band skipped; this proves nothing about the search"

    def test_transparent_never_shrinks_because_it_never_has_to(self, overlay):
        # It has the whole monitor, so there is no band to fit anything into.
        w = self._transparent(overlay, window_height_percent=5)
        assert w._static_fit_scale(_long_block()) == 1.0

    def test_the_pills_never_claim_more_than_half_a_short_band(self, overlay):
        """The pills are a fixed size and do not scale with the subtitle font,
        so on a thin band they asked for more room than the window had: the
        content area collapsed to one pixel and there was nothing left to fit
        the text into."""
        w = overlay(
            SUBTITLE_MODE_STATIC, bilingual_mode=True, transparent_static=False
        )
        screen = w._screen().geometry()
        for percent in (5, 8, 12, 40):
            w.resize(screen.width(), max(1, int(screen.height() * percent / 100)))
            assert w.reserved_bottom() <= max(1, w.height() // 2), percent
            assert w._content_height() >= w.height() // 2, percent

    def test_the_text_never_leaves_the_window_however_thin_the_band(
        self, overlay
    ):
        # The reported symptom: at the slider's floor the block ran off the
        # bottom of the screen.
        from PySide6.QtGui import QPainter, QPixmap

        # ABSOLUTE heights, never a share of this machine's screen: the band a
        # given percentage produces depends on the monitor, so a screen-derived
        # size asserts something different on every box. 38 px is 5% of the
        # 768 px CI runner and is shorter than a bilingual block can ever be —
        # the case that has to hold, not the one that happens to.
        for percent, height in ((5, 38), (8, 61), (12, 92), (23, 176), (100, 768)):
            w = overlay(
                SUBTITLE_MODE_STATIC,
                bilingual_mode=True,
                transparent_static=False,
                window_height_percent=percent,
            )
            w.resize(1024, height)
            w.add_subtitle(_LONG_DE, _LONG_AR)
            drawn: list[tuple[int, int]] = []

            def record(p, block, x, y, newest=True, _w=w, _o=w._draw_block, _d=drawn):
                _d.append((y, _w._measure_block(block)))
                return _o(p, block, x, y, newest)

            w._draw_block = record
            pixmap = QPixmap(max(1, w.width()), max(1, w.height()))
            painter = QPainter(pixmap)
            try:
                w._paint_static(painter)
            finally:
                painter.end()
            top, height = drawn[0]
            # The bottom is the edge that matters: it is the one the disclaimer
            # sits on and the one the monitor ends at. A block too tall for the
            # band loses its opening lines off the TOP instead — top may go
            # negative, and that is the deliberate direction (see _paint_static).
            assert top + height <= w.height(), (
                f"{percent}%: text runs {top + height - w.height()}px past the "
                f"bottom of a {w.height()}px overlay"
            )
            if height <= w._content_height():
                assert top >= 0, f"{percent}%: a block that FITS was cut off"

    def test_the_block_sits_above_the_footer_not_mid_screen(self, overlay):
        """Centring was invisible in a short band and wrong on a whole monitor.

        A subtitle belongs at the bottom of the picture, so the block is
        anchored to the foot of the content area and grows upward.
        """
        from PySide6.QtGui import QPainter, QPixmap

        w = self._transparent(overlay, window_height_percent=0)
        w.add_subtitle(*reversed(PAIRS[0]))
        tops: list[int] = []
        w._draw_block = lambda p, block, x, y, newest=True: tops.append(y) or 0
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._paint_static(painter)
        finally:
            painter.end()
        block_height = w._measure_block(w._blocks[-1])
        assert tops == [w._content_height() - block_height]
        # ...and clear of the disclaimer pill, which reserved_bottom holds back.
        assert tops[0] + block_height <= w.height() - w.reserved_bottom()

    # ── the lift ─────────────────────────────────────────────────────────
    def test_the_slider_lifts_the_content_off_the_bottom(self, overlay):
        w = self._transparent(overlay, static_lift_percent=0)
        w.add_subtitle(*reversed(PAIRS[0]))
        assert w._static_lift() == 0
        w.set_static_lift_percent(20)
        assert w._static_lift() == int(w.height() * 20 / 100)

    def test_the_lift_is_capped_at_half_the_screen(self, overlay):
        # Past halfway a subtitle is no longer at the bottom of the picture.
        w = self._transparent(overlay, static_lift_percent=100)
        w.add_subtitle(*reversed(PAIRS[0]))
        assert w._static_lift() == int(w.height() * STATIC_LIFT_PERCENT_MAX / 100)

    def test_the_lift_never_pushes_the_block_off_the_top(self, overlay):
        # The same bug at the other end: it stops where the block runs out of
        # room rather than walking the text off the top edge.
        w = self._transparent(overlay, static_lift_percent=STATIC_LIFT_PERCENT_MAX)
        w.add_subtitle(_LONG_DE, _LONG_AR)
        w.resize(w.width(), 420)
        assert w._static_lift() <= max(
            0, w._content_height() - w._measure_block(w._blocks[-1])
        )

    def test_the_block_and_the_pills_move_by_the_same_amount(self, overlay):
        """The whole point of the control: the disclaimer travels WITH the text
        it belongs to instead of staying pinned to the bottom of the screen."""
        from PySide6.QtGui import QPainter, QPixmap

        def measure(percent):
            w = self._transparent(overlay, static_lift_percent=percent)
            w.add_subtitle(*reversed(PAIRS[0]))
            tops: list[int] = []
            pills: list[int] = []
            w._draw_block = lambda p, b, x, y, newest=True: tops.append(y) or 0
            w._pill = lambda p, text, bottom, fill, fg, **kw: (
                pills.append(bottom) or bottom
            )
            pixmap = QPixmap(w.width(), w.height())
            painter = QPainter(pixmap)
            try:
                w._paint_static(painter)
                w._paint_pills(painter)
            finally:
                painter.end()
            return tops[0], pills[0], w._static_lift()

        low_block, low_pill, low_lift = measure(0)
        high_block, high_pill, high_lift = measure(STATIC_LIFT_PERCENT_MAX)
        assert high_lift > low_lift, "the lift did not take effect"
        assert low_block - high_block == low_pill - high_pill == high_lift - low_lift

    def test_a_feed_mode_leaves_the_pills_where_they_were(self, overlay):
        from PySide6.QtGui import QPainter, QPixmap

        from gui.subtitle_window import FOOTER_MARGIN

        w = overlay(SUBTITLE_MODE_REALTIME, window_height_percent=40)
        pills: list[int] = []
        w._pill = lambda p, text, bottom, fill, fg, **kw: pills.append(bottom) or bottom
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._paint_pills(painter)
        finally:
            painter.end()
        assert pills[0] == w.height() - FOOTER_MARGIN


class TestTransparentStaticRibbon:
    """The backdrop of transparent static mode: one box per RENDERED LINE.

    Reported against two per-paragraph boxes, which failed in both directions —
    a short sentence still got a box running the width of the screen, and the
    pair's boxes overlapped so the translation's hid the source's last line.
    """

    def _runs(self, w, block):
        """The block's paragraphs, laid out exactly as _draw_block lays them."""
        import gui.subtitle_window as sw

        trans_font, src_font = w._block_fonts(block)
        runs = []
        used = 0
        if src_font is not None and block.source:
            layout, height = w._layout_text(block.source, src_font)
            runs.append(sw._Run(layout, 0, height))
            used += height + w._pair_gap(block)
        layout, height = w._layout_text(block.translation, trans_font)
        runs.append(sw._Run(layout, used, height))
        return runs

    def _rects(self, w, block):
        return w._ribbon_rects(self._runs(w, block), 0, w._content_width())

    def test_a_short_line_gets_a_short_box(self, overlay):
        from gui.subtitle_window import Block

        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True)
        rects = self._rects(w, Block("Ja."))
        assert len(rects) == 1
        assert rects[0].width() < w._content_width() // 2, (
            "the box still runs to the window's edges"
        )

    def test_a_longer_line_gets_a_wider_box(self, overlay):
        from gui.subtitle_window import Block

        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True)
        short = self._rects(w, Block("Ja."))[0]
        longer = self._rects(w, Block("Ja, und zwar ganz genau so."))[0]
        assert longer.width() > short.width(), "the box did not grow with the text"

    def test_one_box_per_rendered_line_not_per_paragraph(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True, bilingual_mode=True)
        block = _long_block()
        runs = self._runs(w, block)
        lines = sum(run.layout.lineCount() for run in runs)
        assert lines > 2, "this block does not wrap; it proves nothing"
        assert len(w._ribbon_rects(runs, 0, w._content_width())) == lines

    def test_a_wrapped_paragraph_hugs_each_line_separately(self, overlay):
        # One box around a wrapped sentence is a rectangle at its LONGEST
        # line's width, with ragged text inside it.
        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True)
        rects = self._rects(w, _long_block(source=None))
        assert len({r.width() for r in rects}) > 1, "every line got the same box"

    def test_the_boxes_tile_so_none_can_hide_a_line(self, overlay):
        """The overlap bug, stated as geometry.

        A block's source and its translation are pulled together until their
        metric boxes overlap — _pair_gap is allowed to go negative and only the
        INK is held apart — so two independent backdrops drew one on top of the
        other. Every box has to reach the next one and no further.
        """
        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True, bilingual_mode=True)
        rects = self._rects(w, _long_block())
        for above, below in zip(rects, rects[1:], strict=False):
            assert above.top() < below.top(), "the ribbon ran backwards"
            assert above.bottom() >= below.top() - 1, "a gap opened in the ribbon"

    def test_every_line_of_the_source_keeps_a_backdrop(self, overlay):
        # The visible failure: the translation's box covered the source's last
        # line, so half the Arabic was drawn on a box and half on the video.
        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True, bilingual_mode=True)
        block = _long_block()
        runs = self._runs(w, block)
        rects = w._ribbon_rects(runs, 0, w._content_width())
        source = runs[0]
        for i in range(source.layout.lineCount()):
            line = source.layout.lineAt(i)
            middle = source.top + line.position().y() + line.height() / 2
            assert any(r.top() <= middle <= r.bottom() for r in rects), (
                f"source line {i} has no backdrop under it"
            )

    def test_the_ribbon_is_drawn_before_any_text(self, overlay):
        """Order is the other half of the fix: interleaved, a backdrop still
        lands on the line above it however well the rects tile."""
        from PySide6.QtGui import QPainter, QPixmap

        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True, bilingual_mode=True)
        events: list[str] = []
        w._draw_ribbon = lambda p, rects: events.append("ribbon")
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            import gui.subtitle_window as sw

            original = sw.QTextLayout.draw
            sw.QTextLayout.draw = lambda self, *a: events.append("text")
            try:
                w._draw_block(painter, _long_block(), 0, 0)
            finally:
                sw.QTextLayout.draw = original
        finally:
            painter.end()
        assert events.count("ribbon") == 1
        assert events.index("ribbon") < events.index("text")

    @pytest.mark.parametrize(
        ("mode", "transparent"),
        [
            (SUBTITLE_MODE_STATIC, False),
            # Transparent is a static-mode option; a feed mode keeps its
            # window backdrop whatever the toggle says.
            (SUBTITLE_MODE_REALTIME, True),
        ],
    )
    def test_no_ribbon_when_the_window_carries_the_backdrop(
        self, overlay, mode, transparent
    ):
        from PySide6.QtGui import QPainter, QPixmap

        w = overlay(mode, bilingual_mode=True, transparent_static=transparent)
        drawn: list = []
        w._draw_ribbon = lambda p, rects: drawn.append(rects)
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._draw_block(painter, _long_block(), 0, 0)
        finally:
            painter.end()
        assert drawn == [], "a ribbon was drawn over the window's own backdrop"

    @pytest.mark.parametrize("theme", ["dark", "light"])
    def test_the_card_contrasts_with_the_text_drawn_on_it(self, overlay, theme):
        """The card was a fixed black while the text colour follows the
        subtitle theme, so Untertitel-Modus "Hell" put near-black text on a
        near-black box and the subtitles could not be read at all."""
        w = overlay(SUBTITLE_MODE_STATIC, transparent_static=True, theme_mode=theme)
        card = w._card_fill()
        text = w._translation_qcolor()
        assert abs(card.lightness() - text.lightness()) > 128, (
            f"{theme}: card {card.lightness()} vs text {text.lightness()}"
        )

    def test_the_backdrop_opacity_reaches_the_card(self, overlay):
        """The control the mode used to grey out. Asserted on the pixels the
        ribbon is actually filled with, because the fill is the whole of what
        the operator is setting here."""
        from PySide6.QtGui import QImage, QPainter

        def alpha_at(percent: int) -> int:
            w = overlay(
                SUBTITLE_MODE_STATIC,
                transparent_static=True,
                backdrop_opacity=percent,
            )
            image = QImage(w.width(), w.height(), QImage.Format_ARGB32)
            image.fill(0)
            painter = QPainter(image)
            try:
                w._draw_ribbon(painter, w._ribbon_rects(self._runs(w, _long_block()), 0, 400))
            finally:
                painter.end()
            return max(
                QColor.fromRgba(image.pixel(x, y)).alpha()
                for y in range(0, image.height(), 4)
                for x in range(0, image.width(), 4)
            )

        faint, solid = alpha_at(20), alpha_at(90)
        assert faint < solid, "the slider does not reach the card"
        assert faint > 0, "20% painted nothing at all"

    @pytest.mark.parametrize("side_by_side", [False, True])
    def test_the_card_keeps_its_clearance_from_the_disclaimer(
        self, overlay, side_by_side
    ):
        """A card is drawn _CARD_PAD_Y BELOW the text it wraps, and holding
        back only for the TEXT spent the clearance on that pad: the card's
        bottom border came out flush against the pill, with the two touching.
        The panel of the side-by-side layout keeps air there and so must this.
        """
        from PySide6.QtGui import QPainter, QPixmap

        from gui.subtitle_window import PILL_CLEARANCE

        w = overlay(
            SUBTITLE_MODE_STATIC,
            transparent_static=True,
            bilingual_mode=True,
            side_by_side=side_by_side,
            show_footer=True,
            window_height_percent=0,
        )
        w.add_subtitle(*reversed(PAIRS[0]))
        cards: list = []
        pill_tops: list[int] = []
        w._draw_ribbon = lambda p, rects: cards.extend(rects)
        w._pill = lambda p, text, bottom, fill, fg, **kw: (
            pill_tops.append(bottom - w._pill_height()) or bottom - w._pill_height()
        )
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._paint_static(painter)
            w._paint_pills(painter)
        finally:
            painter.end()
        assert cards, "nothing drew a card; this proves nothing"
        assert pill_tops, "no pill was drawn; this proves nothing"
        gap = min(pill_tops) - max(r.bottom() for r in cards)
        assert gap >= PILL_CLEARANCE, f"only {gap} px between card and pill"


class TestAnnouncementBackdrop:
    """An announcement belongs to no layout, so it has to carry its own.

    It is not a subtitle block, and in the side-by-side layout it deliberately
    does not use the two column panels (`_column_panel_rects` returns None while
    one is up). That left it with nothing behind it there: the window backdrop
    is fully transparent in that layout *because* the panels are the background.
    White text straight onto the picture — reported from a real session.
    """

    @staticmethod
    def _ribbons(w) -> list:
        from PySide6.QtGui import QPainter, QPixmap

        drawn: list = []
        w._draw_ribbon = lambda p, rects: drawn.extend(rects)
        w.set_announcement("Das Gebet beginnt in 5 Minuten")
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._paint_announcement(painter)
        finally:
            painter.end()
        return drawn

    def test_side_by_side_gives_the_announcement_a_card(self, overlay):
        w = overlay(
            SUBTITLE_MODE_REALTIME, side_by_side=True, bilingual_mode=True
        )
        assert w._backdrop().alpha() == 0, "premise: the window paints nothing"
        assert self._ribbons(w), "the announcement had no backdrop at all"

    def test_transparent_static_still_does(self, overlay):
        # The look the side-by-side case was asked to match; unchanged.
        w = overlay(
            SUBTITLE_MODE_STATIC, transparent_static=True, bilingual_mode=True
        )
        assert self._ribbons(w)

    @pytest.mark.parametrize(
        ("mode", "kwargs"),
        [
            (SUBTITLE_MODE_REALTIME, {}),
            (SUBTITLE_MODE_CONTINUOUS, {}),
            (SUBTITLE_MODE_STATIC, {"transparent_static": False}),
        ],
    )
    def test_a_mode_that_paints_its_own_backdrop_gets_no_card(
        self, overlay, mode, kwargs
    ):
        # The other half: where the window already paints a backdrop, a card
        # would be a second, darker box inside it.
        w = overlay(mode, bilingual_mode=True, **kwargs)
        assert w._backdrop().alpha() > 0, "premise: the window paints a backdrop"
        assert self._ribbons(w) == []


class TestSideBySideLayout:
    """The two-column bilingual layout (issue #49).

    A row is a table row, not two independent feeds: both cells start at the
    row's top edge and the taller one decides its height. If each column
    flowed on its own, pair 3 would end up beside pair 5 within a few
    utterances.
    """

    _KEEP = object()  # "leave the default"; None means "no original at all"

    def _overlay(self, overlay, mode=SUBTITLE_MODE_STATIC, **kwargs):
        kwargs.setdefault("bilingual_mode", True)
        kwargs.setdefault("side_by_side", True)
        return overlay(mode, **kwargs)

    def _block(self, translation=None, source=_KEEP):
        from gui.subtitle_window import Block

        if source is self._KEEP:
            source = PAIRS[0][1]
        return Block(translation or PAIRS[0][0], source)

    def test_the_two_columns_share_a_top_edge(self, overlay):
        w = self._overlay(overlay)
        src, trans = w._column_rects(self._block(), 120)
        assert src.y() == trans.y() == 120

    def test_a_row_is_as_tall_as_its_taller_column_not_both(self, overlay):
        w = self._overlay(overlay)
        block = self._block()
        src, trans = w._column_rects(block, 0)
        assert w._measure_block(block) == max(src.height(), trans.height())
        # And that is genuinely less than stacking them, or the layout would
        # buy nothing.
        assert w._measure_block(block) < src.height() + trans.height()

    def test_the_columns_are_equal_and_separated_by_a_real_gutter(self, overlay):
        w = self._overlay(overlay)
        src, trans = w._column_rects(self._block(), 0)
        assert src.width() == trans.width() == w._column_width()
        left, right = sorted((src, trans), key=lambda r: r.x())
        gutter = right.x() - (left.x() + left.width())
        # Deliberately NOT compared against COLUMN_GAP_RATIO: that would pass
        # for any value including zero. A hairline is the failure — two scripts
        # running into each other — so the bound is a share of the column, and
        # it holds at any window size.
        assert gutter >= w._column_width() * 0.05

    def test_the_panels_reach_much_closer_to_the_edge_than_a_text_margin(
        self, overlay
    ):
        """The panels are the BACKDROP in this layout, not a line of text.
        Keeping them SIDE_MARGIN_RATIO off the edge made them read as two small
        boxes floating inside a big one."""
        from gui.subtitle_window import SIDE_MARGIN_RATIO

        w = self._overlay(overlay)
        left, right = w._column_panel_rects()
        text_margin = w.width() * SIDE_MARGIN_RATIO
        assert left.x() < text_margin / 2
        assert w.width() - right.right() < text_margin / 2
        # Symmetric, so neither side looks pushed in.
        assert abs(left.x() - (w.width() - 1 - right.right())) <= 1

    def test_arabic_takes_the_right_column(self, overlay):
        """The Arabic → German main path: Arabic right because it is RTL, so
        the German translation lands on the left."""
        w = self._overlay(overlay)
        src, trans = w._column_rects(self._block(), 0)
        assert src.x() > trans.x()

    def test_the_columns_swap_when_the_translation_is_the_rtl_side(self, overlay):
        """Turkish → Arabic: the Arabic is now the TRANSLATION and still has to
        sit right, so the sides follow the script rather than the role."""
        w = self._overlay(overlay)
        block = self._block(translation=PAIRS[0][1], source="Rahman ve Rahim olan")
        src, trans = w._column_rects(block, 0)
        assert trans.x() > src.x()

    def test_two_ltr_languages_keep_the_translation_on_the_left(self, overlay):
        """No directional reason either way, so the audience's own language
        stays where it was on the Arabic path — left."""
        w = self._overlay(overlay)
        block = self._block(translation="In the name of Allah", source="Im Namen")
        src, trans = w._column_rects(block, 0)
        assert trans.x() < src.x()

    def test_a_block_with_no_original_spans_the_full_width(self, overlay):
        """Same-language mode, error messages and the verified-verse bypass all
        emit source=None, and that is routine rather than an edge case."""
        w = self._overlay(overlay)
        block = self._block(source=None)
        assert w._column_rects(block, 0) is None
        trans_font, _src = w._block_fonts(block)
        assert w._measure_block(block) == w._measure(block.translation, trans_font)

    def test_the_layout_needs_both_switches(self, overlay):
        w = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=True, side_by_side=False)
        assert w._column_rects(self._block(), 0) is None
        w = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=False, side_by_side=True)
        assert w._column_rects(self._block(), 0) is None

    def test_every_mode_gets_it(self, overlay):
        for mode in (
            SUBTITLE_MODE_STATIC,
            SUBTITLE_MODE_REALTIME,
            SUBTITLE_MODE_CONTINUOUS,
        ):
            w = self._overlay(overlay, mode)
            assert w._column_rects(self._block(), 0) is not None, mode

    # ── the panels behind the columns ────────────────────────────────────
    def test_two_identical_panels_sit_behind_the_columns(self, overlay):
        """Two of them, the same size, in the same place every frame — that is
        what makes it read as two columns rather than two stacks of text."""
        w = self._overlay(overlay)
        left, right = w._column_panel_rects()
        assert left.size() == right.size()
        assert left.y() == right.y()
        assert left.x() < right.x()

    def test_each_column_sits_inside_its_panel_with_an_even_inset(self, overlay):
        """The panel is the container: a column is the panel less its inset on
        both sides, so text can never reach a panel edge."""
        from gui.subtitle_window import COLUMN_PANEL_PAD_X

        w = self._overlay(overlay)
        panels = w._column_panel_rects()
        for text_rect in w._column_rects(self._block(), 0):
            panel = next(
                (p for p in panels if p.left() <= text_rect.left() <= p.right()), None
            )
            assert panel is not None, "no panel contains this column"
            assert text_rect.left() - panel.left() == COLUMN_PANEL_PAD_X
            assert panel.right() - text_rect.right() >= COLUMN_PANEL_PAD_X - 1

    def test_the_window_backdrop_gives_way_to_the_panels(self, overlay):
        """The panels ARE the backdrop here. Painting the window one as well
        put a third, larger box behind the two the layout exists to show —
        which is what it looked like on a real screen."""
        w = self._overlay(overlay, backdrop_opacity=100)
        assert w._backdrop().alpha() == 0
        # And the panels carry exactly what the window backdrop would have, so
        # the opacity slider still controls them.
        assert w._backdrop_fill().alpha() == 255
        stacked = overlay(
            SUBTITLE_MODE_STATIC,
            bilingual_mode=True,
            side_by_side=False,
            backdrop_opacity=100,
        )
        assert stacked._backdrop().alpha() == 255

    def test_transparent_swaps_the_panels_for_a_card_per_sentence(self, overlay):
        """It means the same thing in both layouts: no large background, a card
        around each sentence instead. Here that takes the panels away and gives
        each column's sentence its own card — never a card inside a panel."""
        from PySide6.QtGui import QPainter, QPixmap

        w = self._overlay(overlay, transparent_static=True)
        assert w._transparent_static_active() is True
        assert w._backdrop().alpha() == 0
        assert w._column_panel_rects() is None, "the panels must give way"

        drawn: list = []
        # One ribbon per column, never one spanning both: the columns are side
        # by side and share no vertical run, so tiling them together would put
        # a backdrop across the gutter.
        w._draw_ribbon = lambda p, rects: drawn.append(rects)
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._draw_block(painter, self._block(), 0, 0)
        finally:
            painter.end()
        assert len(drawn) == 2, "each column's sentence needs its own card"
        assert drawn[0] and drawn[1], "a column was given an empty ribbon"
        assert drawn[0][0].x() != drawn[1][0].x(), "both cards landed in one column"

    def test_a_latin_original_drops_the_italic_beside_its_translation(
        self, overlay
    ):
        """Italic marks the original as subordinate, which it only is when it
        is stacked ABOVE its translation. In a row of two equals it read as a
        quotation beside a sentence rather than the same thing twice."""
        w = self._overlay(overlay)
        block = self._block(translation="This is the translated line.")
        block.source = "Das ist die Originalzeile."
        _trans, source = w._block_fonts(block)
        assert source.bold(), "side by side draws the original bold"
        assert not source.italic()

    def test_stacked_keeps_it_italic(self, overlay):
        w = self._overlay(overlay, side_by_side=False)
        block = self._block(translation="This is the translated line.")
        block.source = "Das ist die Originalzeile."
        _trans, source = w._block_fonts(block)
        assert source.italic() and not source.bold()

    def test_arabic_is_upright_in_both_layouts(self, overlay):
        # It has no italic face worth the name, and never had one here.
        for side_by_side in (True, False):
            w = self._overlay(overlay, side_by_side=side_by_side)
            _trans, source = w._block_fonts(self._block())
            assert not source.italic(), side_by_side

    def test_the_panels_stay_when_transparent_is_off(self, overlay):
        w = self._overlay(overlay, transparent_static=False)
        assert w._column_panel_rects() is not None

    def test_the_panels_do_not_reach_the_footer_pill(self, overlay):
        w = self._overlay(overlay)
        left, _right = w._column_panel_rects()
        assert left.bottom() <= w.height() - w.reserved_bottom()

    def test_the_panel_sits_in_an_even_frame_of_video(self, overlay):
        """The panels ARE the backdrop here, so where they start IS the top of
        the overlay. At the feed's own margin they began far enough down that
        at 100% height a band of video stood above them and a hairline below —
        the maintainer asked for one clearance at both ends."""
        from gui.subtitle_window import FOOTER_MARGIN

        w = self._overlay(overlay)
        left, right = w._column_panel_rects()
        assert left.top() == right.top()
        pill_top = w.height() - FOOTER_MARGIN - w._pill_height()
        above = left.top()
        below = pill_top - (left.bottom() + 1)
        assert above == below, f"{above} px of video above, {below} below"

    def test_the_feed_starts_at_the_panels_own_inset(self, overlay):
        """Measured from the PANEL, not from the window: a line held a share of
        the height down from the window's edge would leave a band of empty
        backdrop inside the top of the panel."""
        from gui.subtitle_window import COLUMN_PANEL_PAD_Y

        w = self._overlay(overlay, mode=SUBTITLE_MODE_REALTIME)
        panel, _right = w._column_panel_rects()
        assert self._first_block_top(w) == panel.top() + COLUMN_PANEL_PAD_Y

    def test_the_stacked_layout_keeps_its_share_of_the_height(self, overlay):
        """It has no panel to be inset from, so the line is held off the
        window's edge by the same figure as before."""
        from gui.subtitle_window import FEED_TOP_RATIO

        w = self._overlay(overlay, mode=SUBTITLE_MODE_REALTIME, side_by_side=False)
        expected = int(w.height() * FEED_TOP_RATIO)
        assert expected > 0, "this window is too short to tell the two apart"
        assert self._first_block_top(w) == expected

    @staticmethod
    def _first_block_top(w) -> int:
        """The y the feed's first block is actually drawn at."""
        from PySide6.QtGui import QPainter, QPixmap

        w.add_subtitle(*reversed(PAIRS[0]))
        tops: list[int] = []
        w._draw_block = lambda p, block, x, y, newest=True: tops.append(y) or 0
        pixmap = QPixmap(w.width(), w.height())
        painter = QPainter(pixmap)
        try:
            w._paint_realtime(painter)
        finally:
            painter.end()
        return tops[0]

    def test_no_panels_outside_the_layout(self, overlay):
        stacked = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=True, side_by_side=False)
        assert stacked._column_panel_rects() is None
        mono = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=False, side_by_side=True)
        assert mono._column_panel_rects() is None

    def test_an_announcement_takes_the_panels_away(self, overlay):
        """It renders large and centred across the whole window; framing it in
        two columns it does not use would read as a mistake."""
        w = self._overlay(overlay)
        assert w._column_panel_rects() is not None
        w.set_announcement("Das Gebet beginnt in fünf Minuten.")
        assert w._column_panel_rects() is None

    # ── the original's weight ────────────────────────────────────────────
    def test_the_newest_original_carries_the_same_weight_as_its_translation(
        self, overlay
    ):
        """Stacked, the original is a subordinate line and takes the muted
        tone. Side by side it is the other half of the row, and muted there
        reads as already-said on one side and current on the other."""
        from PySide6.QtGui import QColor

        w = self._overlay(overlay)
        assert w._column_source_qcolor(newest=True) == QColor(w._colors["text"])
        assert w._column_source_qcolor(newest=False) == w._history_qcolor()

    def test_the_stacked_layout_keeps_its_muted_original(self, overlay):
        from PySide6.QtGui import QColor

        w = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=True, side_by_side=False)
        assert w._source_qcolor() == QColor(w._colors["muted"])

    def test_a_configured_source_colour_still_wins(self, overlay):
        from PySide6.QtGui import QColor

        w = self._overlay(overlay)
        w.set_source_text_color("#FF0000")
        assert w._column_source_qcolor(newest=True) == QColor("#FF0000")

    def test_the_original_is_bold_only_in_this_layout(self, overlay):
        """AGENTS.md keeps Arabic source lines at regular weight in the STACKED
        layout, where the original is a subordinate line. Side by side it is
        the other half of the row and carries the same weight."""
        block = self._block()
        columns = self._overlay(overlay)
        stacked = overlay(SUBTITLE_MODE_STATIC, bilingual_mode=True, side_by_side=False)
        assert columns._block_fonts(block)[1].bold() is True
        assert stacked._block_fonts(block)[1].bold() is False
        # The translation was always bold; this is about matching it.
        assert columns._block_fonts(block)[0].bold() is True

    def test_the_live_line_keeps_its_own_weight(self, overlay):
        """The live transcript is not a column — it stays full width below the
        feed — so source_font's new bold flag must not reach it by default.
        Arabic there was already drawn in the translation font."""
        w = self._overlay(overlay)
        w.set_live_text("Im Namen Allahs")
        assert w._live_font().bold() is False
        w.set_live_text("بسم الله الرحمن الرحيم")
        assert w._live_font().bold() is True

    def test_toggling_it_restacks_a_continuous_feed(self, overlay):
        """Continuous blocks carry an absolute y computed from their height,
        and every height just changed."""
        w = self._overlay(overlay, SUBTITLE_MODE_CONTINUOUS, side_by_side=False)
        for translation, source in PAIRS:
            w.add_subtitle(translation, source_text=source)
        before = [b.y for b in w._blocks]
        w.set_side_by_side(True)
        assert [b.y for b in w._blocks] != before
        # Still bottom-anchored and still in order, with no overlap.
        for earlier, later in zip(w._blocks, w._blocks[1:], strict=False):
            assert later.y >= earlier.y + w._measure_block(earlier)


class TestLayoutAppearanceMemory:
    """Each layout remembers its own font sizes and colours.

    A column is half as wide and the two scripts sit at equal weight there, so
    one set of values cannot suit both. The live fields are SWAPPED with the
    other layout's on every toggle, so the subtitle window, the batch window
    and the steppers all keep reading the same fields.
    """

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        import gui.control_panel as cp

        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        s = p.settings
        s.font_size_base = 30
        s.source_font_size_base = 42.0
        s.translation_text_color = ""
        s.source_text_color = ""
        s.alt_font_size_base = None
        s.alt_source_font_size_base = None
        s.alt_translation_text_color = None
        s.alt_source_text_color = None
        yield p
        p.close()

    def _live(self, panel):
        s = panel.settings
        return (
            s.font_size_base,
            s.source_font_size_base,
            s.translation_text_color,
            s.source_text_color,
        )

    def test_the_selector_is_hidden_without_an_original_to_put_beside_it(self, panel):
        panel._on_bilingual_toggled(False)
        assert panel.layout_segment.isHidden()
        panel._on_bilingual_toggled(True)
        assert not panel.layout_segment.isHidden()

    def test_hiding_the_selector_keeps_the_stored_layout(self, panel):
        """Turning the original off and on again must not silently drop the
        layout that was chosen."""
        panel.layout_segment.set_current_index(1)
        panel._on_bilingual_toggled(False)
        panel._on_bilingual_toggled(True)
        assert panel.layout_segment.current_index() == 1

    def test_it_is_a_selector_that_names_both_layouts(self, panel):
        """Not a checkbox: the off state is a real alternative layout, and
        "Side by side" unticked leaves the other one unnamed."""
        from gui.widgets import SEGMENT_COMPACT_H, SegmentedControl

        assert isinstance(panel.layout_segment, SegmentedControl)
        labels = panel._subtitle_layout_labels()
        assert len(labels) == 2 and all(labels)
        # Inline beside a checkbox, so it must not carry a row control's height.
        assert panel.layout_segment._buttons[0].height() == SEGMENT_COMPACT_H

    def test_the_selector_shares_the_row_with_the_toggle(self, panel):
        """Next to "Show original text", not under it — which also means
        hiding it cannot change the card's height.

        Asserted on the layout rather than on coordinates: these windows are
        never shown, so widget geometry is whatever Qt last happened to set.
        """
        body = panel.bilingual_check.parentWidget().layout()
        row = None
        for i in range(body.count()):
            sub = body.itemAt(i).layout()
            if sub is None:
                continue
            if any(
                sub.itemAt(j).widget() is panel.bilingual_check
                for j in range(sub.count())
            ):
                row = sub
                break
        assert row is not None, '"Show original text" is not on a row'
        in_row = [row.itemAt(i).widget() for i in range(row.count())]
        assert in_row[:2] == [panel.bilingual_check, panel.layout_segment]

    def test_the_first_switch_moves_nothing(self, panel):
        """Both layouts start from what was already chosen, so turning the mode
        on for the first time does not resize the subtitles."""
        before = self._live(panel)
        panel._on_side_by_side_toggled(True)
        assert self._live(panel) == before
        assert panel.settings.alt_font_size_base == 30

    def test_each_layout_keeps_what_was_chosen_for_it(self, panel):
        """All four values, not just the original's: both sizes and both
        colours belong to the layout they were chosen in."""
        panel._on_side_by_side_toggled(True)  # seeds both sides
        s = panel.settings
        s.font_size_base = 45  # tuned for a half-width column
        s.source_font_size_base = 45.0
        s.translation_text_color = "#FFD700"
        s.source_text_color = "#FFFFFF"

        panel._on_side_by_side_toggled(False)
        assert self._live(panel) == (30, 42.0, "", "")

        panel._on_side_by_side_toggled(True)
        assert self._live(panel) == (45, 45.0, "#FFD700", "#FFFFFF")

    def test_the_controls_are_repainted_from_the_layout_that_was_restored(
        self, panel
    ):
        """The steppers and colour buttons show stored values, and a switch
        replaces all four at once — the translation stepper included, which
        only ``_step_font`` used to keep in step."""
        panel._on_side_by_side_toggled(True)
        panel.settings.font_size_base = 60  # a much smaller rendered size
        columns_text = panel._font_percent_text()
        panel._refresh_typography()
        assert panel.font_stepper.value.text() == columns_text

        panel._on_side_by_side_toggled(False)
        assert panel.settings.font_size_base == 30
        assert panel.font_stepper.value.text() == panel._font_percent_text()
        assert panel.font_stepper.value.text() != columns_text

    def test_the_stacked_side_survives_more_than_one_round_trip(self, panel):
        panel._on_side_by_side_toggled(True)
        panel.settings.font_size_base = 45
        for _ in range(3):
            panel._on_side_by_side_toggled(False)
            assert panel.settings.font_size_base == 30
            panel._on_side_by_side_toggled(True)
            assert panel.settings.font_size_base == 45

    def test_a_change_made_while_stacked_stays_on_the_stacked_side(self, panel):
        panel._on_side_by_side_toggled(True)
        panel._on_side_by_side_toggled(False)
        panel.settings.font_size_base = 25  # tuned for one full-width column
        panel._on_side_by_side_toggled(True)
        assert panel.settings.font_size_base == 30
        panel._on_side_by_side_toggled(False)
        assert panel.settings.font_size_base == 25


class TestParagraphDirection:
    """A trailing full stop belongs at the END of the sentence, which for
    Arabic is its LEFT edge. QTextOption defaults its text direction to
    LEFT-TO-RIGHT rather than to "work it out", so every line was laid out as
    an LTR paragraph: the words still ran right-to-left (bidi does that inside
    the paragraph regardless), but the terminator — a neutral character —
    attached to the paragraph and landed on the right."""

    @staticmethod
    def _direction(w, text: str) -> str:
        from gui.fonts import subtitle_font

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
        from gui.subtitle_window import FOOTER_MARGIN, PILL_CLEARANCE

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

        from gui.theme import apply_theme

        apply_theme(qt_app, "light")
        style = qt_app.style()
        assert style.styleHint(QStyle.SH_ComboBox_Popup) == 0
        assert style.styleHint(QStyle.SH_ComboBox_UseNativePopup) == 0

    def test_the_popup_is_a_plain_item_view(self, qt_app):
        from PySide6.QtWidgets import QListView

        from gui.theme import apply_theme
        from gui.widgets import Dropdown

        apply_theme(qt_app, "light")
        combo = Dropdown(["Deutsch", "English"])
        try:
            # The stylesheet dresses `QComboBox QAbstractItemView`; a view the
            # platform style substitutes is not necessarily one of those.
            assert isinstance(combo.view(), QListView)
        finally:
            combo.close()


class TestDropdownTooltips:
    """A dropdown elides what it cannot fit, in the closed box and in the
    popup alike. Device names are the case that hurts: two inputs from the
    same chip differ only in the tail Qt cuts off, so the picker becomes
    unreadable at the width people actually run the window at. The full text
    is carried as a tooltip — but only where it is genuinely cut off, so
    hovering never just repeats a label that is already fully readable.

    Tooltips are asserted against the original string, never against
    `itemText()`, which would pass for an elided value too."""

    LONG = "Mikrofonarray (Intel Smart Sound Technology for Digital Microphones)"
    SHORT = "Deutsch"
    NARROW = 220
    WIDE = 900

    @pytest.fixture
    def combo(self, qt_app):
        """A *shown, themed* dropdown, and it needs to be both.

        Shown, because Qt defers a hidden widget's resize event until it is
        mapped — a tooltip decided in resizeEvent is never taken on one.

        Themed, because the stylesheet is what pads a popup row
        (`QAbstractItemView::item`), and the row inset is measured from it.
        `setStyleSheet` is global on the session-scoped QApplication, so
        without this the class measures an unthemed popup when run alone and
        a themed one when another test happens to run first.
        """
        from gui.theme import apply_theme
        from gui.widgets import Dropdown

        apply_theme(qt_app, "light")
        made = Dropdown()
        made.show()
        qt_app.processEvents()
        yield made
        made.hidePopup()
        made.close()

    @staticmethod
    def _at(combo, width, qt_app):
        combo.resize(width, 44)
        qt_app.processEvents()

    def test_a_cut_off_entry_gets_a_tooltip(self, combo, qt_app):
        combo.addItem(self.LONG)
        self._at(combo, self.NARROW, qt_app)
        assert combo.toolTip() == self.LONG

    def test_an_entry_that_fits_gets_none(self, combo, qt_app):
        # The whole point of the rule: a tooltip repeating a readable label is
        # noise, and it appears under the cursor during a live session.
        combo.addItem(self.SHORT)
        self._at(combo, self.NARROW, qt_app)
        assert combo.toolTip() == ""

    def test_the_tooltip_follows_the_width(self, combo, qt_app):
        # Squeezing the window is what turns a readable entry into an elided
        # one, so the decision cannot be taken once at insert time.
        combo.addItem(self.LONG)
        self._at(combo, self.WIDE, qt_app)
        assert combo.toolTip() == ""
        self._at(combo, self.NARROW, qt_app)
        assert combo.toolTip() == self.LONG
        self._at(combo, self.WIDE, qt_app)
        assert combo.toolTip() == ""

    def test_the_tooltip_follows_the_selection(self, combo, qt_app):
        # A previous device's name on a box now showing another is worse
        # than no tooltip at all.
        combo.addItems([self.LONG, self.SHORT])
        self._at(combo, self.NARROW, qt_app)
        assert combo.toolTip() == self.LONG
        combo.setCurrentIndex(1)
        assert combo.toolTip() == ""

    def test_an_entry_added_later_is_covered(self, combo, qt_app):
        # Every device dropdown is built empty and filled after enumeration,
        # so the constructor path alone would cover none of them.
        self._at(combo, self.NARROW, qt_app)
        combo.addItem(self.LONG)
        assert combo.toolTip() == self.LONG

    def test_the_arrow_and_padding_are_not_counted_as_room(self, combo, qt_app):
        # Room for text is the edit-field subrect, not the whole box — the
        # arrow and the padding take ~30 px of it. A box made exactly as wide
        # as the string still cannot show it, so this fails for an
        # implementation that measures self.width() and reports "it fits".
        combo.addItem(self.LONG)
        self._at(combo, combo.fontMetrics().horizontalAdvance(self.LONG), qt_app)
        assert combo.toolTip() == self.LONG

    def test_renaming_the_shown_entry_resyncs(self, combo, qt_app):
        # gui/onboarding.py relabels the provider entry in place once its key
        # is known; the old label must not survive on the box.
        combo.addItem(self.LONG)
        self._at(combo, self.NARROW, qt_app)
        combo.setItemText(0, self.SHORT)
        assert combo.toolTip() == ""

    def test_popup_rows_carry_only_what_the_popup_cuts_off(self, combo, qt_app):
        from PySide6.QtCore import Qt

        combo.addItems([self.LONG, self.SHORT])
        self._at(combo, self.NARROW, qt_app)
        combo.showPopup()
        assert combo.itemData(0, Qt.ToolTipRole) == self.LONG
        assert combo.itemData(1, Qt.ToolTipRole) is None


class TestAlwaysOnTopAcrossPlatforms:
    """X11 carries always-on-top as _NET_WM_STATE_ABOVE, which Qt's xcb plugin
    only writes while the window is unmapped. Setting the flag on a visible
    window therefore did nothing at all there — the setting was simply dead on
    Linux."""

    def test_a_visible_window_is_remapped_where_the_flag_needs_it(
        self, qt_app, monkeypatch
    ):
        from PySide6.QtWidgets import QWidget

        import gui.widgets as widgets

        monkeypatch.setattr(widgets, "needs_remap", lambda: True)
        window = QWidget()
        window.show()
        _settle(qt_app)
        try:
            widgets.set_window_on_top(window, True)
            _settle(qt_app)
            assert widgets.is_window_on_top(window)
            # setWindowFlag re-parents, which hides the widget: a window that
            # was on screen has to be put back, or the overlay disappears the
            # moment the operator changes the setting.
            assert window.isVisible()
            widgets.set_window_on_top(window, False)
            _settle(qt_app)
            assert not widgets.is_window_on_top(window)
            assert window.isVisible()
        finally:
            window.close()

    def test_a_matching_flag_is_re_asserted_where_the_wm_owns_the_state(
        self, qt_app, monkeypatch
    ):
        """The overlay went missing while the panel obeyed the setting.

        On X11 the flag Qt holds is not what the window manager is doing — the
        overlay is re-mapped behind Qt's back by its own geometry repair — so
        an early return on a matching flag left it under the browser. Windows,
        where the flag IS the truth, still returns early and never flashes.
        """
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QWidget

        import gui.widgets as widgets

        window = QWidget()
        window.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        calls = []
        monkeypatch.setattr(
            QWidget, "setWindowFlag", lambda self, f, on=True: calls.append((f, on))
        )
        try:
            monkeypatch.setattr(widgets, "needs_remap", lambda: False)
            widgets.set_window_on_top(window, True)
            assert calls == [], "the cheap platform re-applied a flag it already had"

            monkeypatch.setattr(widgets, "needs_remap", lambda: True)
            widgets.set_window_on_top(window, True)
            assert calls == [(Qt.WindowStaysOnTopHint, True)]
        finally:
            window.close()

    def test_a_re_mapped_window_is_raised_again(self, qt_app, monkeypatch):
        # setWindowFlag builds a NEW native window, and the WM decides where to
        # map it — under everything else, for one that never takes focus.
        from PySide6.QtWidgets import QWidget

        import gui.widgets as widgets

        monkeypatch.setattr(widgets, "needs_remap", lambda: True)
        raised = []
        monkeypatch.setattr(QWidget, "raise_", lambda self: raised.append(self))
        window = QWidget()
        window.show()
        _settle(qt_app)
        try:
            widgets.set_window_on_top(window, True)
            _settle(qt_app)
            assert raised == [window]
        finally:
            window.close()

    def test_a_hidden_window_is_not_shown_by_the_toggle(self, qt_app, monkeypatch):
        from PySide6.QtWidgets import QWidget

        import gui.widgets as widgets

        monkeypatch.setattr(widgets, "needs_remap", lambda: True)
        window = QWidget()
        try:
            widgets.set_window_on_top(window, True)
            assert widgets.is_window_on_top(window)
            assert not window.isVisible()
        finally:
            window.close()


class TestInterimTranscriptToggle:
    """The switch used to be read once, at Start: the bridge folded it into
    ``_streaming`` and nothing looked at it again, so turning the live
    transcript off mid-session left the last interim frozen on the overlay for
    the rest of the run. The Tk panel re-reads it on every poll tick."""

    class _Controller:
        def __init__(self):
            self.translation_queue = queue.Queue()
            self.error_queue = queue.Queue()

        def get_live_transcript(self):
            return ("interim", False)

    def _bridge(self, *, show_interim: bool):
        from gui.pipeline_bridge import PipelineBridge

        bridge = PipelineBridge(self._Controller())
        bridge.start(streaming=True, show_interim=show_interim)
        return bridge

    def test_turning_it_on_mid_session_starts_sampling(self, qt_app):
        bridge = self._bridge(show_interim=False)
        try:
            assert not bridge._live_timer.isActive()
            bridge.set_show_interim(True)
            assert bridge._live_timer.isActive()
        finally:
            bridge.stop()

    def test_turning_it_off_stops_sampling_and_clears_the_line(self, qt_app):
        bridge = self._bridge(show_interim=True)
        cleared: list[tuple] = []
        bridge.live_text.connect(lambda t, s: cleared.append((t, s)))
        try:
            assert bridge._live_timer.isActive()
            bridge.set_show_interim(False)
            assert not bridge._live_timer.isActive()
            assert cleared == [("", False)], "the stale interim was left on screen"
        finally:
            bridge.stop()

    def test_it_never_samples_a_segmented_session(self, qt_app):
        # Only a streaming pipeline has an in-progress transcript to sample.
        from gui.pipeline_bridge import PipelineBridge

        bridge = PipelineBridge(self._Controller())
        bridge.start(streaming=False, show_interim=True)
        try:
            assert not bridge._live_timer.isActive()
            bridge.set_show_interim(True)
            assert not bridge._live_timer.isActive()
        finally:
            bridge.stop()

    def test_the_panel_hands_the_switch_to_the_bridge(self, monkeypatch, qt_app):
        panel = _panel(monkeypatch)
        applied: list[bool] = []
        monkeypatch.setattr(
            panel.bridge, "set_show_interim", lambda v: applied.append(v)
        )
        try:
            panel.interim_check.setChecked(not panel.interim_check.isChecked())
            assert applied, "the toggle stopped at settings.json"
        finally:
            panel.close()


class TestLiveLineRows:
    """The live line turns its row over the way Tk's does: wrapped greedily
    from the start, only the last REALTIME_LIVE_MAX_ROWS rows kept. It used to
    be truncated with QFontMetrics.elidedText, which slid the text along one
    character at a time instead — and re-ordered RTL text on the way, so the
    live line's full stop ended up at the wrong end while the settled lines
    right below it were correct."""

    LONG = (
        "وهذا الذي قرره ابن جرير شيخ المفسرين رحمه الله تعالى في هذه السورة "
        "وهو المقصود الاعظم من هذه الايات الكريمة والله اعلم بالصواب"
    )

    def test_a_filled_row_starts_a_fresh_one(self, overlay):
        from config import REALTIME_LIVE_MAX_ROWS

        w = overlay(SUBTITLE_MODE_REALTIME)
        words = self.LONG.split()
        shown = []
        for n in range(1, len(words) + 1):
            w.set_live_text(" ".join(words[:n]), False)
            shown.append(w._live_rows())
        # Somewhere the visible text gets SHORTER than it was a word ago: that
        # is the row turning over rather than the text sliding along.
        assert any(
            len(b) < len(a) for a, b in zip(shown, shown[1:], strict=False)
        ), "the row never turned over"
        # And what is shown always fits the rows it is allowed.
        for text in shown:
            if text:
                layout, _h = w._layout_live(text)
                assert layout.lineCount() <= REALTIME_LIVE_MAX_ROWS

    def test_nothing_is_dropped_while_it_still_fits(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        w.set_live_text("هو المقصود الاعظم.", False)
        assert w._live_rows() == "هو المقصود الاعظم."

    def test_no_ellipsis_is_ever_inserted(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        w.set_live_text(self.LONG, False)
        assert "…" not in w._live_rows()


class TestLiveLineDirection:
    """A streaming STT prefixes artefact markers like <noise>. Unicode decides
    a paragraph's direction from its first STRONG character, so five Latin
    letters made an entire Arabic transcript an LTR paragraph and put its full
    stop at the right — where the sentence starts."""

    @staticmethod
    def _direction(w, text: str) -> str:
        w.set_live_text(text, False)
        shown = w._live_rows()
        # The layout has to stay referenced: a QTextLine borrows from it, and
        # reading one whose layout was a temporary corrupts the heap.
        layout, _height = w._layout_live(shown)
        line = layout.lineAt(0)
        return "rtl" if line.cursorToX(len(shown))[0] < line.cursorToX(0)[0] else "ltr"

    def test_a_latin_noise_marker_does_not_flip_an_arabic_line(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        assert self._direction(w, "<noise> هو المقصود الاعظم.") == "rtl"

    def test_a_latin_transcript_stays_ltr(self, overlay):
        w = overlay(SUBTITLE_MODE_REALTIME)
        assert self._direction(w, "This is the running transcript.") == "ltr"

    def test_settled_text_still_follows_the_unicode_rule(self, overlay):
        # Deliberately NOT counted: a translation legitimately opens in one
        # script and quotes the other, and counting would flip a German
        # sentence carrying a long Arabic quotation.
        from gui.fonts import subtitle_font

        w = overlay(SUBTITLE_MODE_STATIC)
        german = 'Er sagte: "وكل بدعة ضلالة وكل ضلالة في النار".'
        layout, _h = w._layout_text(german, subtitle_font(40, text=german))
        line = layout.lineAt(0)
        assert line.cursorToX(len(german))[0] > line.cursorToX(0)[0]


class TestHonorificClearance:
    """ﷺ/ﷻ come from whichever family has the glyph, not the one we asked for,
    and that family can draw them far taller than the line around them. Every
    way of paying for that in the SPACING was tried and reported as a bug:
    measuring them pushed the whole paragraph away from the one above,
    ignoring them let the ligature climb into it, and clamping the reclaim
    spread the paragraph's own rows apart. The ligature is made to fit
    instead."""

    OVERSIZED = "dass Allah ﷻ mit euch wetteifert."

    class _TallMetrics:
        """Metrics whose ligature overflows the box, as a Linux fallback's do.

        The rule cannot be exercised with a real font here: Segoe UI's
        ligature already fits, which is the no-op case below.
        """

        def __init__(self, ascent: int = 20, descent: int = 5):
            self._ascent, self._descent = ascent, descent

        def ascent(self):
            return self._ascent

        def descent(self):
            return self._descent

        def tightBoundingRect(self, _text):  # noqa: N802 - Qt API shape
            from PySide6.QtCore import QRect

            return QRect(0, -60, 40, 70)  # 60 above the baseline, 10 below

    def test_an_oversized_ligature_is_scaled_into_the_line(self, overlay):
        from gui.fonts import subtitle_font
        from gui.subtitle_window import SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)  # an app + theme exist
        font = subtitle_font(51, text=self.OVERSIZED)
        metrics = self._TallMetrics()
        fitted = SubtitleWindow._fitted_format("ﷻ", font, metrics)
        assert fitted is not None, "an overflowing ligature was left at full size"
        scaled = fitted.font().pixelSize()
        assert scaled < font.pixelSize()
        # Small enough that its ink now sits inside the ascent it has to share.
        assert 60 * scaled / font.pixelSize() <= metrics.ascent() + 1

    def test_a_ligature_that_already_fits_is_left_alone(self, overlay):
        # The Windows case, and the point of the whole design: where the glyph
        # fits, nothing about the layout changes at all.
        from gui.fonts import subtitle_font
        from gui.subtitle_window import SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)
        font = subtitle_font(51, text=self.OVERSIZED)
        from PySide6.QtGui import QFontMetrics

        metrics = QFontMetrics(font)
        if -metrics.tightBoundingRect("ﷻ").top() <= metrics.ascent():
            assert SubtitleWindow._honorific_formats(self.OVERSIZED, font) == []

    def test_each_ligature_gets_its_own_span(self, overlay, monkeypatch):
        # With the fit forced, so the mapping is checked on a machine whose
        # own ligatures need no scaling.
        from PySide6.QtGui import QTextCharFormat

        from gui.fonts import subtitle_font
        from gui.subtitle_window import SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)
        monkeypatch.setattr(
            SubtitleWindow,
            "_fitted_format",
            staticmethod(lambda glyph, font, metrics: QTextCharFormat()),
        )
        text = "Allah ﷻ und der Gesandte ﷺ sagten."
        formats = SubtitleWindow._honorific_formats(text, subtitle_font(51))
        assert [(f.start, f.length) for f in formats] == [
            (text.index("ﷻ"), 1),
            (text.index("ﷺ"), 1),
        ]

    def test_a_honorific_does_not_change_the_paragraph_rhythm(self, overlay):
        """The regression that clamping caused: a translation carrying a
        ligature had its OWN rows spread apart to make room for it."""
        from gui.fonts import subtitle_font
        from gui.subtitle_window import SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)
        plain = "dass Allah mit euch wetteifert."
        font = subtitle_font(51, text=plain)
        assert SubtitleWindow._ink(self.OVERSIZED, font) == SubtitleWindow._ink(
            plain, font
        )

    def test_a_line_without_one_is_measured_exactly_as_before(self, overlay):
        from PySide6.QtGui import QFontMetrics

        from gui.fonts import subtitle_font
        from gui.subtitle_window import _STACK_INK_GAP_EM, SubtitleWindow

        overlay(SUBTITLE_MODE_STATIC)
        text = "Vielmehr merken sie es nicht."
        font = subtitle_font(51, text=text)
        fm = QFontMetrics(font)
        expected = max(
            0,
            round(
                fm.ascent()
                + fm.tightBoundingRect(text).top()
                - _STACK_INK_GAP_EM * font.pixelSize()
            ),
        )
        assert SubtitleWindow._ink(text, font)[0] == expected

    @staticmethod
    def _oversized_run(text: str, font):
        """Format spans that make one glyph's run TALLER than the line.

        What a fallback family does to a line that borrows a single glyph from
        it: its ascent becomes the line's, however small the ink is scaled to.
        Fitting the ink cannot prevent that, and no Windows font reproduces it
        — so the run is forced here.
        """
        from PySide6.QtGui import QFont, QTextCharFormat, QTextLayout

        span = QTextLayout.FormatRange()
        span.start = text.index("ﷻ")
        span.length = 1
        taller = QFont(font)
        taller.setPixelSize(font.pixelSize() * 3)
        char_format = QTextCharFormat()
        char_format.setFont(taller)
        span.format = char_format
        return [span]

    def test_a_tall_run_does_not_drop_the_line_it_sits_in(self, overlay, monkeypatch):
        """The symptom that survived the scaling: a blank band above the line.

        Lines are placed by the top of their box, and a line whose ascent is
        the fallback family's is taller than the box every figure around it was
        measured from — so its ink lands that much lower, and the paragraph
        before it appears pushed away.
        """
        from PySide6.QtGui import QFontMetrics

        from gui.fonts import subtitle_font
        from gui.subtitle_window import SubtitleWindow

        w = overlay(SUBTITLE_MODE_STATIC)
        font = subtitle_font(51, text=self.OVERSIZED)
        monkeypatch.setattr(
            SubtitleWindow, "_honorific_formats", staticmethod(self._oversized_run)
        )
        # Bound first: a QTextLine borrows from its layout (gui/AGENTS.md).
        layout, _height = w._layout_text(self.OVERSIZED, font)
        line = layout.lineAt(0)
        assert line.ascent() > QFontMetrics(font).ascent(), "the run is not tall"
        baseline = line.position().y() + line.ascent()
        assert round(baseline) == round(QFontMetrics(font).ascent()), (
            "the honorific's line sits below the rhythm its block was measured at"
        )

    def test_a_line_of_one_font_keeps_its_box_top(self, overlay):
        # The correction is for borrowed metrics only: an ordinary line — every
        # line on Windows — is placed exactly where it always was.
        from gui.fonts import subtitle_font

        w = overlay(SUBTITLE_MODE_STATIC)
        text = "Vielmehr merken sie es nicht."
        layout, _height = w._layout_text(text, subtitle_font(51, text=text))
        assert layout.lineAt(0).position().y() == 0


class TestOverlayFitsTheScreen:
    """The overlay asks for a rectangle; a window manager may grant another.

    An X11 WM can honour the requested SIZE and refuse the requested POSITION —
    GNOME keeps a frameless window clear of its top bar — and the strip that
    then hangs off the bottom of the screen is the one carrying the disclaimer
    pill. The Tk overlay reads back what it was given and shrinks into it
    (_fit_geometry_to_monitor); the Qt one trusted setGeometry.
    """

    HEIGHT_PERCENT = 30  # enough of the screen to be real, little enough to see past

    def _placed(self, qt_app, overlay):
        """A shown overlay whose geometry the WM actually granted.

        A FEED mode, because the height percent has to actually apply: static
        ignores it and takes the whole monitor (_effective_height_percent), and
        a full-height overlay is already at the position these tests move it
        away from — every assertion would then hold trivially.
        """
        w = overlay(SUBTITLE_MODE_REALTIME, window_height_percent=self.HEIGHT_PERCENT)
        w.show()
        _settle(qt_app)
        g = w._screen().geometry()
        if w.geometry().bottom() != g.bottom() or w.geometry().x() != g.x():
            pytest.skip("this window manager places the overlay itself")
        return w, g

    @staticmethod
    def _past_the_remap(w):
        """Put ``w`` where SHRINKING is the repair _fit_to_screen will reach for.

        It has two, tried in order: re-ask for the rectangle unmapped, and only
        then shrink into what is on screen. On a remapping platform (X11) the
        first applies once and returns, so a test about shrinking would measure
        the re-ask instead — which is why these passed on Windows and failed
        under xvfb. Marks the re-ask as already spent; on Windows the flag is
        never read.
        """
        w._remapped = True

    def test_a_granted_rectangle_is_left_alone(self, qt_app, overlay):
        w, _g = self._placed(qt_app, overlay)
        before = w.geometry()
        w._fit_to_screen()
        assert w.geometry() == before

    def test_a_window_pushed_down_is_shrunk_to_the_screen(self, qt_app, overlay):
        from PySide6.QtCore import QPoint

        from gui.subtitle_window import MIN_FITTED_HEIGHT

        w, g = self._placed(qt_app, overlay)
        self._past_the_remap(w)
        height = w.height()
        # How far down to push it. Not a flat 150: the repair refuses to leave
        # less than MIN_FITTED_HEIGHT, so on a short screen (CI's runner is
        # 1024x768, where 30% is 230 px) a 150 px push asks for an 80 px
        # overlay and is correctly ignored — which read as the repair failing.
        push = min(150, height - MIN_FITTED_HEIGHT - 10)
        if push < 10:
            pytest.skip("screen too short to push the overlay off it meaningfully")
        top = QPoint(g.x(), g.y() + g.height() - height + push)
        w.move(top)
        _settle(qt_app)
        w._fit_to_screen()
        assert w.height() == height - push, "the overlay still hangs off the screen"
        assert w.geometry().bottom() <= g.bottom()
        # The top edge stays put — it is the bottom that was in the wrong place.
        assert w.pos() == top

    def test_an_implausible_measurement_is_ignored(self, qt_app, overlay):
        from PySide6.QtCore import QPoint

        from gui.subtitle_window import MIN_FITTED_HEIGHT

        w, g = self._placed(qt_app, overlay)
        # Otherwise X11 takes the re-ask branch and this passes without ever
        # reaching the floor it is about.
        self._past_the_remap(w)
        height = w.height()
        w.move(QPoint(g.x(), g.bottom() - MIN_FITTED_HEIGHT // 2))
        _settle(qt_app)
        w._fit_to_screen()
        # A sliver of an overlay helps nobody: leave it as it is.
        assert w.height() == height

    def test_a_hidden_overlay_is_not_resized(self, overlay):
        from PySide6.QtCore import QRect

        w = overlay(SUBTITLE_MODE_STATIC)
        g = w._screen().geometry()
        w.setGeometry(QRect(g.x(), g.bottom() - 200, 600, 400))  # half off-screen
        w._fit_to_screen()
        assert w.height() == 400

    def test_a_refused_move_is_asked_for_again_unmapped(
        self, qt_app, overlay, monkeypatch
    ):
        """The height slider's bug: the WM applied the size and not the move.

        An X11 WM honours a position on the next map — the rule that also
        governs _NET_WM_STATE_ABOVE — so the request is repeated with the
        window unmapped rather than left as a window that shrank from its top.
        """
        from PySide6.QtCore import QPoint

        import gui.subtitle_window as sw

        w, g = self._placed(qt_app, overlay)
        monkeypatch.setattr(sw, "needs_remap", lambda: True)
        # The window manager's answer: the size, at the old position.
        moved = QPoint(g.x(), g.y())
        w.move(moved)
        _settle(qt_app)
        assert w.pos() == moved
        w._fit_to_screen()
        _settle(qt_app)
        assert w.geometry() == w._requested, "the refused move was not re-made"
        assert w.isVisible(), "the overlay was left unmapped"

    def test_a_window_manager_that_will_not_comply_is_not_fought_forever(
        self, qt_app, overlay, monkeypatch
    ):
        # One remap per placement: a WM that refuses again would otherwise
        # flash the overlay on every check.
        from PySide6.QtCore import QPoint

        import gui.subtitle_window as sw

        w, g = self._placed(qt_app, overlay)
        monkeypatch.setattr(sw, "needs_remap", lambda: True)
        remaps = []
        monkeypatch.setattr(
            sw.QWidget, "hide", lambda self: remaps.append(self.geometry())
        )
        monkeypatch.setattr(sw.QWidget, "show", lambda self: None)
        w.move(QPoint(g.x(), g.y()))
        w._fit_to_screen()
        w._fit_to_screen()
        assert len(remaps) == 1

    def test_every_placement_arms_the_check(self, overlay):
        # The answer only arrives once the WM has replied, so the read-back is
        # queued rather than done inside _apply_geometry.
        w = overlay(SUBTITLE_MODE_STATIC)
        w._fit_timer.stop()
        w.set_window_height_percent(60)
        assert w._fit_timer.isActive()


class TestMacOsOverlayStacking:
    """Nothing a Qt client can ask for puts a window above the macOS Dock or
    menu bar: a stays-on-top window floats above other applications and still
    below both. A full-height overlay therefore lost its bottom strip — the
    disclaimer pill — behind the Dock."""

    def test_macos_is_laid_out_inside_the_work_area(self, overlay, monkeypatch):
        import gui.subtitle_window as sw

        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        monkeypatch.setattr(sw, "_MACOS", True)
        w._apply_geometry()
        available = w._screen().availableGeometry()
        assert w.geometry().bottom() == available.bottom()
        assert w.width() == available.width()

    def test_everywhere_else_a_topmost_overlay_still_covers_the_taskbar(
        self, overlay, monkeypatch
    ):
        # Windows and X11 paint a topmost overlay over the taskbar, and OBS
        # captures the whole frame because of it. Don't level that down.
        import gui.subtitle_window as sw

        w = overlay(SUBTITLE_MODE_STATIC, always_on_top=True)
        monkeypatch.setattr(sw, "_MACOS", False)
        w._apply_geometry()
        screen = w._screen().geometry()
        assert w.geometry().bottom() == screen.bottom()


class TestDropdownPopupPlacement:
    """The list has to open BELOW its box. Refusing the native popup was only
    half of it: the list's rectangle still came from the platform style, and
    the macOS one places it OVER the box so the current item sits under the
    pointer — which drew the first item on top of the closed box's own text."""

    def test_the_list_rect_is_the_box_rect(self, qt_app):
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QStyle, QStyleOptionComboBox

        from gui.theme import apply_theme

        apply_theme(qt_app, "dark")
        option = QStyleOptionComboBox()
        option.rect = QRect(10, 20, 300, 40)
        rect = qt_app.style().subControlRect(
            QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxListBoxPopup, None
        )
        # Qt maps this rect's bottom-left to place the popup: the box's own
        # rectangle opens the list directly underneath it.
        assert rect == option.rect

    def test_the_platform_style_cannot_place_the_list_itself(self, qt_app):
        # The one above passes on Windows either way — the common style already
        # answers option.rect. This forces the macOS answer underneath, since
        # no style here produces it.
        from PySide6.QtCore import QRect
        from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleOptionComboBox

        from gui.theme import _ControlStyle

        over_the_box = QRect(10, -60, 300, 160)

        class _MacLike(QProxyStyle):
            def subControlRect(self, control, option, sub_control, widget=None):  # noqa: N802
                return over_the_box

        style = _ControlStyle()
        # Bound to a name: setBaseStyle hands ownership to C++, and a temporary
        # is collected on the way — the Python override goes with it and the
        # real platform style answers instead.
        base = _MacLike()
        style.setBaseStyle(base)
        option = QStyleOptionComboBox()
        option.rect = QRect(10, 20, 300, 40)
        assert (
            style.subControlRect(
                QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxListBoxPopup, None
            )
            == option.rect
        )
        # Every other sub-control still belongs to the platform style.
        assert (
            style.subControlRect(
                QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, None
            )
            == over_the_box
        )


class TestLinuxPlatformSetup:
    """What a Linux launch asks of Qt before the QApplication exists."""

    def test_x11_is_asked_for_before_wayland(self):
        # A Wayland client can neither place its own windows nor stay on top,
        # and the overlay is nothing but those two things.
        from gui.platform_setup import linux_environment

        assert linux_environment({})["QT_QPA_PLATFORM"] == "xcb;wayland"

    def test_the_font_database_is_silenced_by_category_not_by_severity(self):
        # "qt.text.font.db.warning=false" was the first attempt, and the lines
        # kept coming out on Ubuntu: a rule naming one severity silences
        # nothing if the messages carry another.
        from gui.platform_setup import linux_environment

        rules = linux_environment({})["QT_LOGGING_RULES"]
        assert rules == "qt.text.font.db=false"

    def test_an_operators_own_settings_are_left_alone(self):
        from gui.platform_setup import linux_environment

        env = {"QT_QPA_PLATFORM": "wayland", "QT_LOGGING_RULES": "*.debug=true"}
        assert linux_environment(env) == {}

    @pytest.mark.skipif(
        sys.platform.startswith("linux"), reason="the Linux branch is the one that sets"
    )
    def test_no_other_platform_is_touched(self):
        import os

        from gui.platform_setup import prepare_qt_platform

        before = dict(os.environ)
        prepare_qt_platform()
        assert os.environ == before


class TestAmpersandInACheckboxLabel:
    """A translated "&" was painted as nothing at all.

    ``QAbstractButton`` reads a single "&" as a mnemonic marker, so the German
    and English "Alten Verlauf & Batch-Dateien …" lost the character and bound
    a stray Alt+Space. The translations carry Qt's "&&" escape, which is why
    this guards the JSON rather than the widget.
    """

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(
            cp.ControlPanel, "_ensure_subtitle_window", lambda self: None
        )
        p = cp.ControlPanel(type("C", (), {})())
        yield p
        p.subtitle_window = None
        p.close()

    def test_no_check_claims_a_mnemonic(self, panel):
        for attribute, box in panel._other_checks.items():
            assert box.shortcut().isEmpty(), (
                f"{attribute} bound {box.shortcut().toString()} from its label"
            )

    def test_every_language_escapes_its_ampersands(self):
        """Every "&" in a checkbox label must come in a pair.

        Reads the files rather than building six panels: an unescaped "&" is a
        property of the translation, and the next language to gain one should
        fail here whether or not it is the language under test.
        """
        import glob
        import json
        import re

        keys = ("show_footer", "auto_stop_inactivity", "noise_filter",
                "auto_cleanup_logs", "auto_cleanup_content",
                "auto_start_on_launch")
        for path in glob.glob("data/translations/gui/*.json"):
            with open(path, encoding="utf-8") as fh:
                texts = json.load(fh)
            for key in keys:
                text = texts.get(key)
                if text is None:
                    continue
                assert not re.search(r"(?<!&)&(?!&)", text), (
                    f"{path}:{key} has an unescaped '&' — Qt paints it as nothing"
                )


class TestSettingChangesLeaveABreadcrumb:
    """Changing a setting mid-session used to log NOTHING — six handlers, zero
    log calls, while twelve unused ``log_*_changed`` translation keys described
    the lines nobody ever emitted (2026-08-07). The log is hidden by default and
    exists for exactly this: reconstructing what an operator touched before it
    went wrong. English by decision — see gui/AGENTS.md."""

    @pytest.fixture
    def panel(self, qt_app, monkeypatch):
        cp = cp_module()
        monkeypatch.setattr(cp, "save_settings", lambda s: None)
        monkeypatch.setattr(cp, "activate_stored_keys", lambda: None)
        monkeypatch.setattr(cp, "ensure_keys", lambda *a, **k: True)
        monkeypatch.setattr(
            cp.ControlPanel, "_restart_pipeline_for_live_change", lambda self: None
        )

        class FakeController:
            pass

        p = cp.ControlPanel(FakeController())
        yield p
        p.close()

    @staticmethod
    def _lines(action) -> list[str]:
        """Log lines emitted by ``action``, read off the real queue."""
        from utils.logging import log_queue

        while not log_queue.empty():
            log_queue.get_nowait()
        action()
        out = []
        while not log_queue.empty():
            out.append(log_queue.get_nowait())
        return out

    def test_target_language_change_is_logged(self, panel):
        lines = self._lines(lambda: panel._on_target_changed(0))
        assert any("Target language changed to" in line for line in lines), lines

    def test_source_language_change_is_logged(self, panel):
        lines = self._lines(lambda: panel._on_source_changed(0))
        assert any("Source language changed to" in line for line in lines), lines

    def test_subtitle_mode_change_is_logged(self, panel):
        lines = self._lines(lambda: panel._on_mode_changed(0))
        assert any("Subtitle mode changed to" in line for line in lines), lines

    def test_scroll_speed_change_is_logged(self, panel):
        lines = self._lines(lambda: panel._step_speed(0.25))
        assert any("Scroll speed changed to" in line for line in lines), lines

    def test_transparent_toggle_is_logged_both_ways(self, panel):
        on = self._lines(lambda: panel._on_transparent_changed(True))
        off = self._lines(lambda: panel._on_transparent_changed(False))
        assert any("Transparent mode: enabled" in line for line in on), on
        assert any("Transparent mode: disabled" in line for line in off), off

    def test_the_breadcrumbs_are_english_not_translated(self, panel):
        """The panel is built with whatever gui_language settings.json carries.
        A line that came back translated would still contain the English
        substrings above only by accident, so assert the rule directly."""
        panel.texts = {
            "log_target_language_changed": "ÜBERSETZT: {language}",
            "log_subtitle_mode_changed": "ÜBERSETZT: {mode}",
        }
        lines = self._lines(lambda: panel._on_target_changed(0))
        assert lines and not any("ÜBERSETZT" in line for line in lines), lines


class TestApiKeyDialogRefusesAnEmptyKey:
    """OK on an empty field used to be indistinguishable from Cancel: both
    callers treat "" as a cancel, and ensure_keys aborts the whole Start on it,
    so the operator pressed OK and the session silently did not begin
    (2026-08-07). The `dlg_key_empty` string existed for this and was never
    wired up; disabling the button says it without a box to dismiss."""

    @pytest.fixture
    def dialog(self, qt_app):
        from gui.api_keys import ApiKeyDialog

        d = ApiKeyDialog("openai", {})
        yield d
        d.close()

    def test_ok_is_disabled_while_the_field_is_empty(self, dialog):
        assert not dialog._ok_button.isEnabled()

    def test_ok_enables_once_a_key_is_typed(self, dialog):
        dialog.edit.setText("sk-test-key")
        assert dialog._ok_button.isEnabled()

    def test_whitespace_alone_does_not_count_as_a_key(self, dialog):
        # key() strips, so "   " would reach the caller as "" — the exact
        # silent-cancel this guards against.
        dialog.edit.setText("   ")
        assert not dialog._ok_button.isEnabled()

    def test_clearing_the_field_disables_ok_again(self, dialog):
        dialog.edit.setText("sk-test-key")
        dialog.edit.setText("")
        assert not dialog._ok_button.isEnabled()

    def test_cancel_is_always_available(self, dialog):
        from PySide6.QtWidgets import QDialogButtonBox

        box = dialog.findChild(QDialogButtonBox)
        assert box.button(QDialogButtonBox.Cancel).isEnabled()

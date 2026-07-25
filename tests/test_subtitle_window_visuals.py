"""Animation-lifecycle and shallow-surface invariants for the audience window.

These tests intentionally avoid constructing a real ``Tk`` window. Callback
ownership, the reduced-motion preference and the compact-surface geometry are
UI invariants that can be verified without depending on a display server or
changing the subtitle rendering pipeline.

(The V3 colour-palette restyle from the source PR is deliberately not adopted;
the subtitle window keeps its existing theme palette, so the palette-token
assertions from that PR are not part of this suite.)
"""

import tkinter as tk
from types import SimpleNamespace

import pytest

import gui.subtitle_window as subtitle_module
from gui.subtitle_window import (
    SUBTITLE_MODE_CONTINUOUS,
    SubtitleWindow,
    _prefers_reduced_motion,
)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_reduced_motion_can_be_forced_for_render_harnesses(monkeypatch, value):
    monkeypatch.setenv("MINBARLIVE_REDUCED_MOTION", value)

    assert _prefers_reduced_motion() is True


@pytest.mark.parametrize("value", ["0", "false", "NO", "off"])
def test_reduced_motion_can_be_explicitly_disabled(monkeypatch, value):
    monkeypatch.setenv("MINBARLIVE_REDUCED_MOTION", value)

    assert _prefers_reduced_motion() is False


def test_cancel_animation_jobs_is_idempotent():
    window = object.__new__(SubtitleWindow)
    window._delayed_font_job = "after#1"
    window._continuous_start_job = "after#2"
    window._scroll_animation_id = "after#3"
    window._feed_anim_job = "after#4"
    cancelled = []
    window.after_cancel = cancelled.append

    window._cancel_animation_jobs()
    window._cancel_animation_jobs()

    assert cancelled == ["after#1", "after#2", "after#3", "after#4"]
    assert window._delayed_font_job is None
    assert window._continuous_start_job is None
    assert window._scroll_animation_id is None
    assert window._feed_anim_job is None


def test_cancel_after_job_clears_id_even_if_tk_already_removed_it():
    window = object.__new__(SubtitleWindow)
    window._feed_anim_job = "after#gone"

    def already_removed(_job):
        raise tk.TclError("event does not exist")

    window.after_cancel = already_removed

    window._cancel_after_job("_feed_anim_job")

    assert window._feed_anim_job is None


def test_stopped_hint_freezes_and_resumes_continuous_motion():
    window = object.__new__(SubtitleWindow)
    window._subtitle_mode = SUBTITLE_MODE_CONTINUOUS
    window._continuous_start_job = "after#start"
    window._scroll_animation_id = "after#scroll"
    window._feed_anim_job = "after#feed"
    cancelled = []
    started = []
    window.after_cancel = cancelled.append
    window._refresh_stopped_hint = lambda: None
    window._start_continuous_scroll = lambda: started.append(True)

    window.set_stopped_hint(True)

    assert cancelled == ["after#start", "after#scroll", "after#feed"]
    assert started == []

    window.set_stopped_hint(False)

    assert started == [True]


def test_monitor_switch_cancels_old_surface_jobs_before_redraw():
    window = object.__new__(SubtitleWindow)
    calls = []
    window._cancel_animation_jobs = lambda: calls.append("cancel")
    window._set_screen_position = lambda: calls.append("position")
    window._applied_size = (1920, 1080)
    window._update_font = lambda: calls.append("font")
    window._update_footer_visibility = lambda: calls.append("footer")
    window._reposition_subtitles = lambda: calls.append("subtitles")
    window._render_live_line = lambda: calls.append("live")
    window._render_announcement = lambda: calls.append("announcement")
    window._stopped_hint = True
    window._subtitle_mode = SUBTITLE_MODE_CONTINUOUS

    window.set_monitor(2)

    assert window._monitor_index == 2
    assert calls[0] == "cancel"
    assert calls[1:] == [
        "position",
        "font",
        "footer",
        "subtitles",
        "live",
        "announcement",
    ]


@pytest.mark.parametrize(
    ("show_footer", "expected_height"), [(True, 96), (False, 54)]
)
def test_five_percent_surface_keeps_footer_legible_without_limiting_footer_free_mode(
    monkeypatch, show_footer, expected_height
):
    monitor = SimpleNamespace(x=0, y=0, width=1920, height=1080)
    monkeypatch.setattr(subtitle_module, "get_monitors", lambda: [monitor])
    window = object.__new__(SubtitleWindow)
    window._monitor_index = 0
    window._window_height_percent = 5
    window._always_on_top = True
    window._show_footer = show_footer
    window._hwnd = None
    geometries = []
    window.geometry = geometries.append
    window._apply_topmost = lambda: None
    window._schedule_geometry_fit_check = lambda: None

    window._set_screen_position()

    assert window._applied_size == (1920, expected_height)
    assert geometries == [
        f"1920x{expected_height}+0+{1080 - expected_height}"
    ]


class _RecordingCanvas:
    def __init__(self):
        self.created = None

    def create_polygon(self, points, **options):
        self.created = (points, options)
        return 42


def test_static_subtitle_card_uses_theme_outline():
    window = object.__new__(SubtitleWindow)
    window.canvas = _RecordingCanvas()
    window._box_padding_x = 22
    window._box_padding_y = 8
    window._box_radius = 12
    window._card_fill = "#071521"
    window._card_outline = "#29414D"

    item_id = window._create_line_background((100, 200, 500, 260))

    assert item_id == 42
    assert window.canvas.created[1]["fill"] == "#071521"
    assert window.canvas.created[1]["outline"] == "#29414D"
    assert window.canvas.created[1]["width"] == 1


class TestStackOverlapFollowsTheFont:
    """The tight line stacking overlaps the metric boxes by the blank leading
    the LOWER font keeps above its ink. That leading is family-specific, so
    the shipped Windows numbers must survive while other families get their
    own — a hardcoded Segoe UI figure collided with real ink on Linux."""

    @staticmethod
    def _window(em, ascent):
        window = object.__new__(SubtitleWindow)
        window.font = ("family", 64, "bold")
        # Pre-seeded cache = the metrics of the family under test, so the
        # classification path runs for real without a Tk font.
        window._font_metrics_cache = {("family", 64, "bold"): (em, ascent)}
        return window

    @pytest.mark.parametrize(
        "text, expected",
        [("Plain caps line", 24), ("Über die Zeit", 10), ("الحمد لله", 0)],
    )
    def test_segoe_ui_keeps_its_shipped_overlap(self, text, expected):
        # Segoe UI Semibold at 64pt, measured: em 85 px, ascent 91 px.
        assert self._window(85.0, 91.0)._stack_overlap(text) == expected

    def test_shallower_ascent_gets_a_smaller_overlap(self):
        # Arial/Helvetica class at 64pt: em 86.8 px, ascent 78 px. Only
        # ~17 px of leading exists, so overlapping by Segoe UI's 32 px ate
        # into the glyphs (the Linux bug).
        window = self._window(86.8, 78.0)

        overlap = window._stack_overlap("Plain caps line")

        assert 0 <= overlap <= 10

    def test_font_without_internal_leading_gets_no_overlap(self):
        # Helvetica clones (Nimbus Sans) set the ascent at the cap height.
        window = self._window(80.0, 58.0)

        assert window._stack_overlap("Plain caps line") == 0

    def test_allah_honorific_does_not_count_as_arabic_ink(self):
        window = self._window(85.0, 91.0)

        assert window._stack_overlap("Allah ﷻ sagt") == window._stack_overlap("Allah")


class TestGeometryFitCorrection:
    """A window manager may honour the requested size but not the position
    (GNOME keeps splash windows out of the top-bar strut), pushing the
    overlay's bottom — and the disclaimer pill — off-screen."""

    @staticmethod
    def _window(rootx, rooty, width, height):
        window = object.__new__(SubtitleWindow)
        window._monitor_index = 0
        window._active_monitor = lambda: SimpleNamespace(
            x=0, y=0, width=1920, height=1080
        )
        window.update_idletasks = lambda: None
        window.winfo_rootx = lambda: rootx
        window.winfo_rooty = lambda: rooty
        window.winfo_width = lambda: width
        window.winfo_height = lambda: height
        window.canvas_width, window.canvas_height = width, height
        window._applied_size = (width, height)
        window._subtitle_mode = SUBTITLE_MODE_CONTINUOUS
        window.subtitle_stack = []
        window.geometry = window.__dict__.setdefault("_geometries", []).append
        for noop in (
            "_update_font",
            "_update_footer_visibility",
            "_render_announcement",
        ):
            setattr(window, noop, lambda *a, **k: None)
        return window

    def test_window_pushed_below_the_screen_is_shrunk_to_fit(self):
        window = self._window(0, 37, 1920, 1080)  # 37 px of it hangs off

        window._fit_geometry_to_monitor()

        assert window._geometries == ["1920x1043+0+37"]
        assert window._applied_size == (1920, 1043)
        assert window.canvas_height == 1043  # the pill is drawn from this

    def test_window_the_wm_granted_is_left_alone(self):
        window = self._window(0, 0, 1920, 1080)

        window._fit_geometry_to_monitor()

        assert window._geometries == []
        assert window._applied_size == (1920, 1080)

    def test_implausible_measurement_is_ignored(self):
        window = self._window(0, 1040, 1920, 1080)  # would leave a 40 px sliver

        window._fit_geometry_to_monitor()

        assert window._geometries == []

    def test_realtime_feed_scroll_absorbs_the_height_change(self):
        window = self._window(0, 37, 1920, 1080)
        window._subtitle_mode = subtitle_module.SUBTITLE_MODE_REALTIME
        window._live_feed_scroll = 500.0
        window._live_feed_scroll_target = 500.0
        window._reposition_subtitles = lambda: None
        window._render_live_line = lambda: None

        window._fit_geometry_to_monitor()

        # Shrunk by 37 px, so the feed scrolls 37 px further to keep the text
        # where it was on screen (same rule as set_window_height_percent).
        assert window._live_feed_scroll == 537.0
        assert window._live_feed_scroll_target == 537.0

    def test_smaller_window_than_requested_only_resyncs_the_canvas(self):
        # The WM granted 1043 px, _applied_size still claims 1080 — the footer
        # would be drawn 37 px below the canvas. No geometry call needed.
        window = self._window(0, 0, 1920, 1043)
        window._applied_size = (1920, 1080)
        window.canvas_height = 1080

        window._fit_geometry_to_monitor()

        assert window._geometries == []
        assert window._applied_size == (1920, 1043)
        assert window.canvas_height == 1043

"""V3 visual tokens and animation cleanup for the audience surface.

These tests intentionally avoid constructing a real ``Tk`` window.  The
palette and callback ownership are UI invariants that can be verified without
depending on a display server or changing the subtitle rendering pipeline.
"""

import tkinter as tk
from types import SimpleNamespace

import pytest

import gui.subtitle_window as subtitle_module
from gui.subtitle_window import (
    _SUBTITLE_THEME_PALETTES,
    SUBTITLE_MODE_CONTINUOUS,
    SubtitleWindow,
    _prefers_reduced_motion,
)


def _relative_luminance(color: str) -> float:
    channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [
        value / 12.92
        if value <= 0.04045
        else ((value + 0.055) / 1.055) ** 2.4
        for value in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    light, dark = sorted(
        (_relative_luminance(foreground), _relative_luminance(background)),
        reverse=True,
    )
    return (light + 0.05) / (dark + 0.05)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_v3_text_roles_keep_readable_contrast(theme):
    palette = _SUBTITLE_THEME_PALETTES[theme]

    assert _contrast(palette["primary_text"], palette["bg_color"]) >= 4.5
    assert _contrast(palette["secondary_text"], palette["bg_color"]) >= 4.5
    assert _contrast(palette["footer_fg"], palette["footer_bg"]) >= 4.5
    assert _contrast(palette["stopped_fg"], palette["stopped_bg"]) >= 4.5


def test_dark_surface_uses_v3_navy_ivory_brass_and_emerald_tokens():
    palette = _SUBTITLE_THEME_PALETTES["dark"]

    assert palette["bg_color"] == "#020A13"
    assert palette["primary_text"] == "#F7F3EA"
    assert palette["footer_fg"] == "#D8B474"
    assert palette["stopped_fg"] == "#7DE2B5"
    assert "#F5820D" not in palette.values()


def test_unknown_theme_falls_back_to_dark_palette():
    window = object.__new__(SubtitleWindow)
    window._theme_palettes = _SUBTITLE_THEME_PALETTES

    window._apply_theme_palette("not-a-theme")

    assert window._theme_mode == "dark"
    assert window._bg_color == _SUBTITLE_THEME_PALETTES["dark"]["bg_color"]
    assert window._accent_outline == "#9A7441"
    assert window._stopped_bg == "#082820"


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


def test_static_subtitle_card_uses_real_theme_outline():
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

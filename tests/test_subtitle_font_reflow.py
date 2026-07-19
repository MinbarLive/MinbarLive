"""Regression tests for changing subtitle size while a session is live."""

from __future__ import annotations

import subprocess
import sys
import tkinter as tk
import tkinter.font as tkfont

import pytest

from gui.subtitle_window import (
    SUBTITLE_MODE_CONTINUOUS,
    SUBTITLE_MODE_REALTIME,
    SUBTITLE_MODE_STATIC,
    SubtitleWindow,
)


def _display_available() -> bool:
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


pytestmark = pytest.mark.skipif(
    not _display_available(), reason="no display available for subtitle reflow tests"
)


@pytest.fixture(scope="module")
def tk_root():
    # One interpreter for the module avoids Tcl's intermittent Windows
    # bootstrap failure when several roots are created/destroyed in one
    # process. Alpha zero keeps the compact render harness invisible.
    root = tk.Tk()
    root.geometry("720x480+0+0")
    try:
        root.attributes("-alpha", 0.0)
    except tk.TclError:
        pass
    root.update()
    yield root
    root.destroy()


@pytest.fixture
def make_surface(tk_root):
    canvases: list[tk.Canvas] = []

    def _make(mode: str, *, reduced_motion: bool = True) -> SubtitleWindow:
        canvas = tk.Canvas(tk_root, width=720, height=480, highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        tk_root.update()
        canvases.append(canvas)

        window = object.__new__(SubtitleWindow)
        window.canvas = canvas
        # The rendering methods only need a Tk scheduler/surface, not a real
        # fullscreen Toplevel. Bind those operations to this compact harness.
        window.update_idletasks = tk_root.update_idletasks
        window.after = tk_root.after
        window.after_cancel = tk_root.after_cancel
        window.canvas_width = canvas.winfo_width()
        window.canvas_height = canvas.winfo_height()
        window._subtitle_mode = mode
        window._font_size_base = 60
        window._font_family = "Segoe UI"
        window._slant_font_family = "Segoe UI"
        window._primary_text = "#ffffff"
        window._secondary_text = "#aab8c2"
        window._card_fill = "#071521"
        window._card_outline = "#29414d"
        window._box_padding_x = 22
        window._box_padding_y = 8
        window._box_radius = 12
        window._line_gap = 10
        window._pair_gap = 5
        window.line_spacing = 30
        window.margin_bottom = 40
        window._bilingual_mode = True
        window._reduced_motion = reduced_motion
        window._destroying = False
        window._is_hidden = False
        window._stopped_hint = False
        window._delayed_font_job = None
        window._continuous_start_job = None
        window._scroll_animation_id = None
        window._feed_anim_job = None
        window._live_feed_scroll = 0.0
        window._live_feed_scroll_target = 0.0
        window._live_text = ""
        window._live_settled = False
        window._live_items = []
        window._canvas_footer_items = []
        window._stopped_hint_items = []
        window._announcement_items = []
        window.subtitle_stack = []
        window._update_font()
        return window

    yield _make

    for canvas in canvases:
        canvas.destroy()
    tk_root.update_idletasks()


def _translation_rows(window: SubtitleWindow) -> list[str]:
    block = window.subtitle_stack[-1]
    if block.line_items:
        return [window.canvas.itemcget(item_id, "text") for item_id, _ in block.line_items]
    return window.canvas.itemcget(block.text_id, "text").split("\n")


def _all_text_ids(window: SubtitleWindow) -> list[int]:
    ids: list[int] = []
    for block in window.subtitle_stack:
        ids.extend(
            item_id for item_id, _ in (block.line_items or [(block.text_id, None)])
        )
        ids.extend(item_id for item_id, _ in (block.source_items or []))
    ids.extend(item_id for item_id, _ in window._live_items)
    return ids


def _block_top(window: SubtitleWindow, index: int) -> int:
    bbox = window._block_bbox(window.subtitle_stack[index])
    assert bbox is not None
    return bbox[1]


@pytest.mark.parametrize(
    "mode", [SUBTITLE_MODE_REALTIME, SUBTITLE_MODE_CONTINUOUS, SUBTITLE_MODE_STATIC]
)
def test_live_font_growth_rewraps_existing_text_inside_canvas(make_surface, mode):
    window = make_surface(mode)
    translation = (
        "A clear translation must remain completely readable while the operator "
        "changes its size during a live sermon, without crossing the screen edge."
    )
    source = (
        "This original transcript also needs a fresh wrap when the live font size "
        "changes instead of keeping stale canvas rows."
    )
    window.add_subtitle(translation, source)
    initial_rows = len(_translation_rows(window))

    for _ in range(6):
        window.increase_font()

    assert window.subtitle_stack[-1].logical_text == translation
    assert window.subtitle_stack[-1].logical_source == source
    assert len(_translation_rows(window)) > initial_rows
    for item_id in _all_text_ids(window):
        bbox = window.canvas.bbox(item_id)
        assert bbox is not None
        assert bbox[0] >= 65
        assert bbox[2] <= window.canvas_width - 65


def test_realtime_font_reflow_snaps_newest_content_above_footer(make_surface):
    window = make_surface(SUBTITLE_MODE_REALTIME, reduced_motion=False)
    for index in range(3):
        window.add_subtitle(
            f"Settled translation number {index} remains readable after resizing "
            "and does not drift outside the audience surface."
        )
    window.set_live_text(
        "The speaker is still talking and this interim line must stay visible."
    )

    for _ in range(6):
        window.increase_font()

    limit = window.canvas_height - window.margin_bottom
    live_bbox = window.canvas.bbox(window._live_items[0][0])
    assert live_bbox is not None
    assert live_bbox[1] < limit
    assert live_bbox[3] <= limit + 2
    assert window._live_feed_scroll == window._live_feed_scroll_target
    assert window._feed_anim_job is None


def test_arabic_font_reflow_uses_logical_text_without_double_shaping(make_surface):
    window = make_surface(SUBTITLE_MODE_REALTIME)
    logical_text = (
        "إن تغيير حجم الخط أثناء البث المباشر يجب أن يعيد ترتيب الكلمات "
        "بشكل صحيح وأن يبقي النص العربي داخل حدود الشاشة."
    )
    window.add_subtitle(logical_text)

    for _ in range(6):
        window.increase_font()

    expected = window._wrap_text_to_lines(
        logical_text, window.canvas_width - 140, window.font
    )
    assert window.subtitle_stack[-1].logical_text == logical_text
    assert _translation_rows(window) == expected


@pytest.mark.parametrize(
    "mode", [SUBTITLE_MODE_REALTIME, SUBTITLE_MODE_CONTINUOUS, SUBTITLE_MODE_STATIC]
)
def test_source_font_size_changes_independently_on_the_live_surface(
    make_surface, mode
):
    window = make_surface(mode)
    window._source_font_size_base = 40.0
    window._update_font()
    window.add_subtitle(
        "The translated sentence keeps its own configured size.",
        "The original sentence is rewrapped independently while the session runs.",
    )
    if mode == SUBTITLE_MODE_REALTIME:
        window.set_live_text("The live transcript follows the original-text size.")

    canvas_before = window.canvas
    translation_base = window.get_font_size_base()
    translation_size = window.get_current_font_size()
    source_size = window.get_current_source_font_size()

    window.increase_source_font()

    assert window.canvas is canvas_before
    assert window.get_font_size_base() == translation_base
    assert window.get_current_font_size() == translation_size
    assert window.get_source_font_size_base() == 35.0
    assert window.get_current_source_font_size() > source_size
    assert window.subtitle_stack[-1].logical_source == (
        "The original sentence is rewrapped independently while the session runs."
    )
    source_id = window.subtitle_stack[-1].source_items[0][0]
    assert tkfont.Font(font=window.canvas.itemcget(source_id, "font")).cget(
        "size"
    ) == window.get_current_source_font_size()
    if mode == SUBTITLE_MODE_REALTIME:
        live_id = window._live_items[0][0]
        assert tkfont.Font(font=window.canvas.itemcget(live_id, "font")).cget(
            "size"
        ) == window.get_current_source_font_size()


@pytest.mark.parametrize(
    "mode", [SUBTITLE_MODE_REALTIME, SUBTITLE_MODE_CONTINUOUS, SUBTITLE_MODE_STATIC]
)
def test_translation_and_source_colors_update_existing_canvas_items(
    make_surface, mode
):
    window = make_surface(mode)
    window.add_subtitle("Translated audience text", "Original spoken text")

    window.set_translation_text_color("#41aabb")
    window.set_source_text_color("#cc8844")

    block = window.subtitle_stack[-1]
    translation_ids = [
        item_id for item_id, _ in (block.line_items or [(block.text_id, None)])
    ]
    source_ids = [item_id for item_id, _ in (block.source_items or [])]
    assert translation_ids
    assert source_ids
    assert all(
        window.canvas.itemcget(item_id, "fill") == "#41aabb"
        for item_id in translation_ids
    )
    assert all(
        window.canvas.itemcget(item_id, "fill") == "#cc8844"
        for item_id in source_ids
    )

    window.set_translation_text_color("")
    window.set_source_text_color("")
    assert window.get_translation_text_color() == ""
    assert window.get_source_text_color() == ""
    assert all(
        window.canvas.itemcget(item_id, "fill") == window._primary_text
        for item_id in translation_ids
    )
    assert all(
        window.canvas.itemcget(item_id, "fill") == window._secondary_text
        for item_id in source_ids
    )


@pytest.mark.parametrize(
    ("live_text", "expected_slant"),
    [
        ("The speaker is still talking", "italic"),
        ("المتحدث لا يزال يتكلم", "roman"),
    ],
)
def test_live_transcript_uses_source_size_and_color_even_when_settled(
    make_surface, live_text, expected_slant
):
    window = make_surface(SUBTITLE_MODE_REALTIME)
    window._source_font_size_base = 40.0
    window._update_font()
    window.set_source_text_color("#bb66dd")

    window.set_live_text(live_text, settled=True)

    live_id = window._live_items[0][0]
    rendered_font = tkfont.Font(font=window.canvas.itemcget(live_id, "font"))
    assert window.canvas.itemcget(live_id, "fill") == "#bb66dd"
    assert rendered_font.cget("size") == window.get_current_source_font_size()
    assert rendered_font.cget("slant") == expected_slant


def test_continuous_reflow_preserves_unseen_backlog_and_read_position(make_surface):
    window = make_surface(SUBTITLE_MODE_CONTINUOUS)
    logical_entries = [
        f"Queued translation {index} remains in its original reading order."
        for index in range(10)
    ]
    for entry in logical_entries:
        window.add_subtitle(entry)

    anchor_top = _block_top(window, 0)
    assert _block_top(window, -1) > window.canvas_height

    window.increase_font()

    assert [block.logical_text for block in window.subtitle_stack] == logical_entries
    assert _block_top(window, 0) == pytest.approx(anchor_top, abs=2)
    tops = [_block_top(window, index) for index in range(len(logical_entries))]
    assert tops == sorted(tops)
    assert tops[-1] > window.canvas_height


def test_static_long_bilingual_pair_stays_inside_vertical_readable_area(make_surface):
    window = make_surface(SUBTITLE_MODE_STATIC)
    translation = (
        "Die deutsche Übersetzung soll auch bei einer großen Schrift vollständig "
        "im sichtbaren Untertitelfenster bleiben und darf nicht über den oberen "
        "Bildschirmrand hinausrutschen. "
    ) * 3
    source = (
        "يجب أن يبقى النص العربي الأصلي مقروءا بالكامل داخل نافذة الترجمة "
        "وألا يخرج عن الحافة العلوية للشاشة عند تكبير حجم الخط. "
    ) * 3
    window.add_subtitle(translation, source)

    for _ in range(8):
        window.increase_font()
    for _ in range(14):
        window.increase_source_font()

    bbox = window._block_bbox(window.subtitle_stack[-1])
    assert bbox is not None
    assert bbox[1] >= 14
    assert bbox[3] <= window.canvas_height - window.margin_bottom + 2


def test_static_bilingual_cards_fit_supported_minimum_window_height(make_surface):
    window = make_surface(SUBTITLE_MODE_STATIC)
    # Five percent of a 1080p monitor is 54 physical pixels. Keep the compact
    # harness canvas itself large enough for Tk to report every item, but use
    # the maintained surface height that the real borderless window applies.
    window.canvas_height = 54
    window.margin_bottom = 8
    window._font_size_base = 20
    window._source_font_size_base = 20
    window._update_font()

    window._add_subtitle_block(
        "Deutsche Übersetzung",
        "النص العربي الأصلي",
        refresh_geometry=False,
    )

    item_ids: list[int] = []
    block = window.subtitle_stack[-1]
    for text_id, box_id in (block.line_items or []) + (block.source_items or []):
        item_ids.append(text_id)
        if box_id:
            item_ids.append(box_id)
    assert item_ids
    bounds = [window.canvas.bbox(item_id) for item_id in item_ids]
    assert all(bbox is not None for bbox in bounds)
    assert all(bbox[1] >= 0 for bbox in bounds if bbox is not None), bounds
    assert all(
        bbox[3] <= window.canvas_height for bbox in bounds if bbox is not None
    ), bounds


def test_static_bilingual_cards_fit_minimum_surface_with_footer(make_surface):
    window = make_surface(SUBTITLE_MODE_STATIC)
    window.canvas_height = 96
    # A one-line footer pill uses 37 px plus its bottom inset and clearance.
    window.margin_bottom = 55
    window._font_size_base = 20
    window._source_font_size_base = 20
    window._update_font()

    window._add_subtitle_block(
        "Deutsche Übersetzung",
        "النص العربي الأصلي",
        refresh_geometry=False,
    )

    block = window.subtitle_stack[-1]
    item_ids = [
        item_id
        for text_id, box_id in (block.line_items or []) + (block.source_items or [])
        for item_id in (text_id, box_id)
        if item_id
    ]
    bounds = [window.canvas.bbox(item_id) for item_id in item_ids]
    assert all(bbox is not None for bbox in bounds)
    assert all(bbox[1] >= 0 for bbox in bounds if bbox is not None), bounds
    # Card padding may use the footer's reserved clearance, but cannot touch
    # the pill itself (whose top is 49 px on this compact surface).
    assert all(bbox[3] <= 49 for bbox in bounds if bbox is not None), bounds


@pytest.mark.parametrize("surface_change", ["monitor", "always_on_top"])
def test_surface_change_reflows_existing_items_with_new_workarea_font(
    make_surface, surface_change
):
    window = make_surface(SUBTITLE_MODE_STATIC)
    window.add_subtitle("A short translated subtitle", "A short original line")
    old_item = window.subtitle_stack[-1].line_items[-1][0]
    old_size = tkfont.Font(font=window.canvas.itemcget(old_item, "font")).cget("size")

    window._stopped_hint = True
    window._applied_size = (1000, 480)
    window._set_screen_position = lambda *args, **kwargs: None
    window._update_footer_visibility = lambda: None
    window._render_announcement = lambda: None

    if surface_change == "monitor":
        window.set_monitor(1)
    else:
        window.set_always_on_top(True)

    new_item = window.subtitle_stack[-1].line_items[-1][0]
    new_size = tkfont.Font(font=window.canvas.itemcget(new_item, "font")).cget("size")
    assert window.canvas_width == 1000
    assert new_size == window.get_current_font_size()
    assert new_size > old_size

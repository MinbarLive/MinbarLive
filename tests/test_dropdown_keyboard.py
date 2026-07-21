"""Keyboard navigation for the shared CustomDropdown.

Drives the key handlers on a minimal object harness (no Tk display needed):
the selection/open logic is what matters, and the Tk calls it makes
(_select/_open/_close/focus_set) are stubbed and recorded.
"""

from __future__ import annotations

from gui.dropdown import CustomDropdown


def _make(values, current=None, *, enabled=True, is_open=False):
    win = object.__new__(CustomDropdown)
    win._values = list(values)
    win._current = current if current is not None else (values[0] if values else "")
    win._enabled = enabled
    win._is_open = is_open
    win.selected: list[str] = []
    win.opened = 0
    win.closed = 0
    win.focused = 0

    def _select(value):
        win._current = value
        win.selected.append(value)

    def _open():
        win.opened += 1
        win._is_open = True

    def _close():
        win.closed += 1
        win._is_open = False

    win._select = _select
    win._open = _open
    win._close = _close
    win.focus_set = lambda: setattr(win, "focused", win.focused + 1)
    return win


def test_down_arrow_steps_to_next_value():
    win = _make(["a", "b", "c"], "a")
    assert win._on_key_step(None, 1) == "break"
    assert win._current == "b"
    assert win.selected == ["b"]
    assert win.focused == 1


def test_up_arrow_steps_to_previous_value():
    win = _make(["a", "b", "c"], "c")
    win._on_key_step(None, -1)
    assert win._current == "b"
    assert win.selected == ["b"]


def test_arrow_at_edge_does_not_refire_the_callback():
    # Down on the last item stays put and must not re-fire _select (which would
    # e.g. re-prompt a provider key or restart the pipeline).
    win = _make(["a", "b", "c"], "c")
    win._on_key_step(None, 1)
    assert win._current == "c"
    assert win.selected == []
    # Focus is still asserted so the widget keeps keyboard control.
    assert win.focused == 1


def test_home_and_end_jump_to_first_and_last():
    win = _make(["a", "b", "c", "d"], "b")
    win._on_key_edge(None, 0)
    assert win._current == "a"
    win._on_key_edge(None, -1)
    assert win._current == "d"
    assert win.selected == ["a", "d"]


def test_toggle_opens_then_closes():
    win = _make(["a", "b"], "a")
    win._on_key_toggle()
    assert win._is_open and win.opened == 1
    win._on_key_toggle()
    assert not win._is_open and win.closed == 1


def test_escape_closes_and_keeps_focus():
    win = _make(["a", "b"], "a", is_open=True)
    assert win._on_key_escape() == "break"
    assert win.closed == 1
    assert win.focused == 1


def test_disabled_dropdown_ignores_keys():
    win = _make(["a", "b", "c"], "a", enabled=False)
    win._on_key_step(None, 1)
    win._on_key_edge(None, -1)
    win._on_key_toggle()
    assert win._current == "a"
    assert win.selected == []
    assert win.opened == 0


def test_empty_dropdown_steps_are_safe():
    win = _make([], "")
    assert win._on_key_step(None, 1) == "break"
    assert win._on_key_edge(None, 0) == "break"
    assert win.selected == []

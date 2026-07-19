"""Keyboard-accessibility tests for :class:`gui.dropdown.CustomDropdown`.

The handlers are invoked directly instead of pumping Tk's event loop.  This
matches the existing GUI-test strategy and avoids intermittent native Tcl
failures while still exercising the callbacks installed for each key.
"""

import sys
from pathlib import Path
from unittest.mock import Mock

import customtkinter as ctk
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from gui.dropdown import CustomDropdown


@pytest.fixture(scope="module")
def tk_root():
    try:
        root = ctk.CTk()
    except Exception as exc:
        pytest.skip(f"no display available for GUI tests: {exc}")
    root.geometry("360x180+0+0")

    yield root

    root.destroy()
    # CustomTkinter keeps process-global scaling registries.  Remove this dead
    # root so later GUI test modules never walk stale Tk objects.
    from customtkinter.windows.widgets.scaling.scaling_tracker import ScalingTracker

    ScalingTracker.window_widgets_dict.pop(root, None)
    ScalingTracker.window_dpi_scaling_dict.pop(root, None)


@pytest.fixture
def dropdown(tk_root):
    root = tk_root
    selected: list[str] = []
    widget = CustomDropdown(
        root,
        values=["First", "Second", "Third"],
        command=selected.append,
    )
    widget.pack(fill="x", padx=20, pady=20)
    root.update_idletasks()

    yield widget, selected

    widget._close()
    if CustomDropdown._bound_root is root:
        CustomDropdown._bound_root = None
    CustomDropdown._active = None
    widget.destroy()


def test_arrow_home_and_end_select_values_and_notify(dropdown, monkeypatch):
    widget, selected = dropdown
    virtual_events: list[object] = []
    focus_set = Mock()
    monkeypatch.setattr(widget, "focus_set", focus_set)
    widget.bind("<<ComboboxSelected>>", virtual_events.append)

    assert widget._on_key_step(None, 1) == "break"
    assert widget.get() == "Second"
    assert widget.current() == 1

    assert widget._on_key_edge(None, -1) == "break"
    assert widget.get() == "Third"

    assert widget._on_key_edge(None, 0) == "break"
    assert widget.get() == "First"
    assert selected == ["Second", "Third", "First"]
    assert virtual_events == [None, None, None]
    assert focus_set.call_count == 3


def test_return_toggle_and_escape_open_and_close_popup(dropdown, monkeypatch):
    widget, _selected = dropdown
    focus_set = Mock()
    monkeypatch.setattr(widget, "focus_set", focus_set)

    assert widget._on_key_toggle() == "break"
    assert widget._is_open is True
    assert widget._popup is not None
    assert widget._popup.winfo_exists()
    assert CustomDropdown._active is widget

    assert widget._on_key_toggle() == "break"
    assert widget._is_open is False
    assert widget._popup is None
    assert CustomDropdown._active is None

    widget._on_key_toggle()
    assert widget._on_key_escape() == "break"
    assert widget._is_open is False
    assert widget._popup is None
    focus_set.assert_called_once_with()


def test_widget_is_in_tab_order_and_focus_style_is_reversible(dropdown):
    widget, _selected = dropdown

    takefocus = widget.tk.call(widget._w, "cget", "-takefocus")
    assert str(takefocus) == "1"

    widget._on_keyboard_focus(None)  # type: ignore[arg-type]
    assert widget.cget("border_color") == widget._drop_hover

    widget._on_keyboard_blur(None)  # type: ignore[arg-type]
    assert widget.cget("border_color") == widget._border_color


def test_click_moves_focus_to_dropdown_before_opening(dropdown, monkeypatch):
    widget, _selected = dropdown
    focus_set = Mock()
    open_popup = Mock()
    monkeypatch.setattr(widget, "focus_set", focus_set)
    monkeypatch.setattr(widget, "_open", open_popup)
    CustomDropdown._focus_in_time = -999.0

    assert widget._on_click(None) == "break"  # type: ignore[arg-type]
    focus_set.assert_called_once_with()
    open_popup.assert_called_once_with()


def test_disabled_dropdown_ignores_selection_and_open_keys(dropdown, monkeypatch):
    widget, selected = dropdown
    open_popup = Mock()
    monkeypatch.setattr(widget, "_open", open_popup)
    widget.configure(state="disabled")

    assert widget._on_key_step(None, 1) == "break"
    assert widget._on_key_edge(None, -1) == "break"
    assert widget._on_key_toggle() == "break"

    assert widget.get() == "First"
    assert selected == []
    open_popup.assert_not_called()

"""Application-wide mouse-wheel / touchpad scrolling on X11 (Linux).

X11 delivers wheel and touchpad scrolling as ``<Button-4>`` / ``<Button-5>``
button presses, not the ``<MouseWheel>`` event that Windows and macOS send.
Tk's built-in ``Text`` and ``Listbox`` class bindings already translate
Button-4/5 into scrolling, which is why the log and text areas scroll on Linux
out of the box.  ``Canvas``-based widgets do not: CustomTkinter's
``CTkScrollableFrame`` (settings, sidebar, history) and our dropdown popup only
listen for ``<MouseWheel>``, so on Linux they never scroll.

Rather than patch each scrollable widget (which leaves the next one broken
again), bind Button-4/5 once at the application root and scroll whichever
scrollable widget sits under the pointer.  Any canvas-based scroll region —
current or future — then works with both a mouse wheel and a touchpad, with no
per-widget wiring.
"""

from __future__ import annotations

import sys
import tkinter as tk
import weakref

# Widget classes whose Tk bindings already scroll on <Button-4>/<Button-5>.
# Scrolling them again from our handler would move them two notches per event.
_NATIVE_WHEEL_CLASSES = {"Text", "Listbox"}

# Roots already wired, so repeated calls (main window + wizard, or several
# scrollable frames) install the global binding only once per Tk interpreter.
_bound_roots: weakref.WeakSet[tk.Misc] = weakref.WeakSet()


def install_x11_mousewheel(root: tk.Misc) -> None:
    """Route X11 Button-4/5 wheel events to the scrollable widget under the
    pointer.  No-op off Linux and idempotent per Tk root."""
    if not sys.platform.startswith("linux"):
        return
    if root in _bound_roots:
        return
    _bound_roots.add(root)
    # bind_all registers on the shared "all" bindtag, so events over every
    # widget in this interpreter — including the dropdown's Toplevel popup —
    # reach the handler.  Button-4 is wheel-up (scroll toward earlier content).
    root.bind_all("<Button-4>", lambda e: _on_wheel(e, -1), add="+")
    root.bind_all("<Button-5>", lambda e: _on_wheel(e, 1), add="+")


def _on_wheel(event: tk.Event[tk.Misc], direction: int) -> str | None:
    widget = getattr(event, "widget", None)
    if not isinstance(widget, tk.Misc):
        return None
    # Text/Listbox scrolled themselves already via their class bindings, which
    # fire before this "all"-tag handler — leave them alone.
    try:
        if widget.winfo_class() in _NATIVE_WHEEL_CLASSES:
            return None
    except tk.TclError:
        return None
    target = _find_scrollable(widget)
    if target is None:
        return None
    try:
        target.yview_scroll(direction, "units")
    except tk.TclError:
        return None
    return "break"


def _find_scrollable(widget: tk.Misc) -> tk.Misc | None:
    """Return the nearest ancestor (or the widget itself) that can scroll
    vertically right now, so an inner scroll region wins over an outer one and
    a fully-visible region hands the event to whatever contains it."""
    node: tk.Misc | None = widget
    while node is not None:
        if callable(getattr(node, "yview", None)):
            try:
                first, last = node.yview()
            except (tk.TclError, TypeError, ValueError):
                first, last = 0.0, 1.0
            if (first, last) != (0.0, 1.0):
                return node
        parent_name = node.winfo_parent()
        node = node.nametowidget(parent_name) if parent_name else None
    return None

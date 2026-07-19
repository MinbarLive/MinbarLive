"""Shared custom dropdown widget.

Used by both the control panel (gui/app_gui.py) and the first-run
onboarding wizard (gui/onboarding.py) so the two look and behave
identically. Extracted from app_gui.py.
"""

import sys
import time
import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

_ICON_FONT = "Segoe Fluent Icons" if sys.platform == "win32" else "Segoe UI Symbol"
_CHEVRON_DOWN = "\ue70d" if sys.platform == "win32" else "▾"


class CustomDropdown(ctk.CTkFrame):
    """Custom dropdown replacing CTkComboBox.

    Fixes:
    - Entire widget is clickable (not just the arrow)
    - 1-px separator acts as a visible border for the arrow zone
    - Max 5 visible items; extra items are scrollable
    - Popup width exactly matches the button width
    - Click once to open, click again to close (toggle)
    - Correct value selection, callbacks, and display updates
    """

    _active: "CustomDropdown | None" = None  # currently open dropdown
    _bound_root: Any = None  # tk root the app-level handlers are bound to
    # Monotonic timestamp of the last OS-level window FocusIn event.
    # Used to detect "click that restored window focus" vs a normal click.
    _focus_in_time: float = -999.0
    # A click within this many seconds of the window regaining OS focus is
    # treated as the focus-restore click: it re-focuses the window without
    # opening the dropdown (the user opens it with a second click). Kept
    # generous — Tk can dispatch the FocusIn noticeably before the click on a
    # cold/ scaled window, so the old 50 ms window missed it and the dropdown
    # opened on the first click. The first click resets the timestamp, so a
    # wide window never swallows the deliberate second click.
    _RESTORE_MAX_DELAY: float = 0.3

    def __init__(
        self,
        parent: Any,
        values: list[str] | tuple[str, ...] = (),
        command: Callable[[str], Any] | None = None,
        height: int = 46,
        corner_radius: int = 16,
        border_width: int = 1,
        font: ctk.CTkFont | None = None,
        dropdown_font: ctk.CTkFont | None = None,
        fg_color: str = "#0f172a",
        border_color: str = "#334155",
        button_color: str = "#0f172a",
        button_hover_color: str = "#182235",
        text_color: str = "#f8fafc",
        dropdown_fg_color: str = "#111827",
        dropdown_hover_color: str = "#263654",
        dropdown_text_color: str = "#f8fafc",
        **kwargs: Any,
    ) -> None:
        kwargs.pop("state", None)
        super().__init__(
            parent,
            fg_color=fg_color,
            border_color=border_color,
            border_width=border_width,
            corner_radius=corner_radius,
            height=height,
            **kwargs,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)

        self._values: list[str] = list(values)
        self._user_command = command
        self._virtual_command: Callable[[Any], Any] | None = None
        self._font = font or ctk.CTkFont(family="Segoe UI", size=14)
        self._height = height

        # Stored colours (updated via configure())
        self._fg_color = fg_color
        self._border_color = border_color
        self._btn_color = button_color
        self._btn_hover = button_hover_color
        self._text_color = text_color
        self._drop_bg = dropdown_fg_color
        self._drop_hover = dropdown_hover_color
        self._drop_text = dropdown_text_color

        self._current: str = self._values[0] if self._values else ""
        self._is_open: bool = False
        self._popup: tk.Toplevel | None = None
        self._enabled: bool = True

        self._build()
        # Participate in the normal Tab order. CTkFrame does not expose
        # ``takefocus`` as a styled option, but its underlying Tk frame does.
        tk.Frame.configure(self, takefocus=True)

    # ── Internal layout ───────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=0)
        self.grid_columnconfigure(2, weight=0)
        self.grid_rowconfigure(0, weight=1)

        self._value_label = ctk.CTkLabel(
            self,
            text=self._current,
            font=self._font,
            text_color=self._text_color,
            anchor="w",
            fg_color="transparent",
        )
        self._value_label.grid(row=0, column=0, sticky="ew", padx=(12, 4), pady=2)

        # 1-px vertical separator — left "border" of the arrow zone
        self._sep = ctk.CTkFrame(
            self, width=1, fg_color=self._border_color, corner_radius=0
        )
        self._sep.grid(row=0, column=1, sticky="ns", pady=8)
        self._sep.grid_propagate(False)

        self._arrow = ctk.CTkLabel(
            self,
            text=_CHEVRON_DOWN,
            font=ctk.CTkFont(family=_ICON_FONT, size=16),
            text_color=self._text_color,
            fg_color="transparent",
            width=40,
        )
        self._arrow.grid(row=0, column=2, sticky="nsew", padx=(4, 8), pady=2)

        for w in (self, self._value_label, self._sep, self._arrow):
            w.bind("<Button-1>", self._on_click, add="+")
        for sequence in ("<Return>", "<space>", "<Alt-Down>"):
            self.bind(sequence, self._on_key_toggle, add="+")
        self.bind("<Down>", lambda event: self._on_key_step(event, 1), add="+")
        self.bind("<Up>", lambda event: self._on_key_step(event, -1), add="+")
        self.bind("<Home>", lambda event: self._on_key_edge(event, 0), add="+")
        self.bind("<End>", lambda event: self._on_key_edge(event, -1), add="+")
        self.bind("<Escape>", self._on_key_escape, add="+")
        self.bind("<FocusIn>", self._on_keyboard_focus, add="+")
        self.bind("<FocusOut>", self._on_keyboard_blur, add="+")

    # ── Global outside-click detection (installed once per app) ──────────

    @classmethod
    def _install_global_handler(cls, root: tk.Misc) -> None:
        # Re-bind when the active Tk root changes (e.g. the onboarding wizard's
        # root is destroyed before the main window's root is created); a single
        # class-level "already bound" flag would leave the new root unbound.
        if cls._bound_root is not root:
            root.bind_all("<Button-1>", cls._on_global_click, add="+")
            root.bind_all("<MouseWheel>", cls._on_global_scroll, add="+")
            root.bind_all("<FocusIn>", cls._on_any_focus_in, add="+")
            cls._bound_root = root

    @staticmethod
    def _on_any_focus_in(event: "tk.Event[Any]") -> None:
        """Record when a top-level window gains OS focus (not internal widget focus).

        <FocusIn> fires on the Toplevel widget itself only when the OS activates
        the window from outside (another app, taskbar, Alt+Tab).  When a child
        widget gets keyboard focus inside an already-focused window the event
        goes to the child, so str(event.widget) != str(winfo_toplevel()) and we
        correctly ignore it.
        """
        try:
            w = event.widget
            if str(w) == str(w.winfo_toplevel()):
                CustomDropdown._focus_in_time = time.monotonic()
        except Exception:
            pass

    @staticmethod
    def _on_global_click(event: "tk.Event[Any]") -> None:
        # Any click anywhere consumes the focus-restore state.
        CustomDropdown._focus_in_time = -999.0
        active = CustomDropdown._active
        if active is None or not active._is_open:
            return
        # Use screen-space coordinates rather than widget paths.  On Windows,
        # clicking an unfocused window can deliver a <Button-1> whose
        # event.widget is the root window rather than the dropdown; path checks
        # fail in that case and the popup is closed immediately.  Coordinates
        # always reflect where the user actually clicked.
        x, y = event.x_root, event.y_root
        # Still inside the open popup?
        if active._popup:
            try:
                if active._popup.winfo_exists():
                    px = active._popup.winfo_rootx()
                    py = active._popup.winfo_rooty()
                    if (
                        px <= x < px + active._popup.winfo_width()
                        and py <= y < py + active._popup.winfo_height()
                    ):
                        return
            except Exception:
                pass
        # Still on the dropdown button itself?
        try:
            dx = active.winfo_rootx()
            dy = active.winfo_rooty()
            if (
                dx <= x < dx + active.winfo_width()
                and dy <= y < dy + active.winfo_height()
            ):
                return
        except Exception:
            pass
        active._close()

    @staticmethod
    def _on_global_scroll(event: "tk.Event[Any]") -> None:
        """Close the open dropdown when the user scrolls outside of it."""
        active = CustomDropdown._active
        if active is None or not active._is_open:
            return
        # Popup blocks its own scroll events with "break", so anything that
        # reaches bind_all must be from outside the popup — close immediately.
        active._close()

    # ── Toggle ────────────────────────────────────────────────────────────

    def _on_click(self, _event: "tk.Event[Any]") -> str:
        if not self._enabled:
            return "break"
        self.focus_set()
        # If the window just gained OS focus, this click is the focus-restore
        # click.  Accept the focus but don't open/close the dropdown; the user
        # can click a second time to interact with it.
        just_focused = (
            time.monotonic() - CustomDropdown._focus_in_time
            < CustomDropdown._RESTORE_MAX_DELAY
        )
        CustomDropdown._focus_in_time = -999.0  # consume regardless
        if just_focused:
            return "break"
        if self._is_open:
            self._close()
        else:
            self._open()
        return "break"  # stop event propagation to parent widgets

    def _on_key_toggle(self, _event: "tk.Event[Any] | None" = None) -> str:
        """Open/close from Return, Space, or Alt+Down."""
        if not self._enabled:
            return "break"
        if self._is_open:
            self._close()
        else:
            self._open()
        return "break"

    def _on_key_step(self, _event: "tk.Event[Any] | None", delta: int) -> str:
        """Choose the adjacent value with the arrow keys."""
        if not self._enabled or not self._values:
            return "break"
        try:
            index = self._values.index(self._current)
        except ValueError:
            index = 0
        index = max(0, min(len(self._values) - 1, index + delta))
        self._select(self._values[index])
        self.focus_set()
        return "break"

    def _on_key_edge(self, _event: "tk.Event[Any] | None", index: int) -> str:
        """Choose the first/last value with Home/End."""
        if not self._enabled or not self._values:
            return "break"
        self._select(self._values[index])
        self.focus_set()
        return "break"

    def _on_key_escape(self, _event: "tk.Event[Any] | None" = None) -> str:
        self._close()
        self.focus_set()
        return "break"

    def _on_keyboard_focus(self, _event: "tk.Event[Any]") -> None:
        if self._enabled:
            super().configure(border_color=self._drop_hover)

    def _on_keyboard_blur(self, _event: "tk.Event[Any]") -> None:
        super().configure(border_color=self._border_color)

    # ── Open popup ────────────────────────────────────────────────────────

    def _open(self) -> None:
        if not self._values:
            return
        if CustomDropdown._active and CustomDropdown._active is not self:
            CustomDropdown._active._close()

        self.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        w = max(self.winfo_width(), 100)
        item_h = 36
        visible = min(len(self._values), 5)

        popup = tk.Toplevel()
        popup.overrideredirect(True)
        popup.wm_attributes("-topmost", True)

        # Outer 1-px border frame
        outer = tk.Frame(popup, bg=self._border_color)
        outer.pack(fill="both", expand=True)
        inner = tk.Frame(outer, bg=self._drop_bg)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        _scroll_canvas: tk.Canvas | None = None
        if len(self._values) > 5:
            canvas_h = visible * item_h
            canvas = tk.Canvas(
                inner, height=canvas_h, bg=self._drop_bg, highlightthickness=0, bd=0
            )
            vsb = tk.Scrollbar(inner, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=vsb.set)
            vsb.pack(side="right", fill="y")
            canvas.pack(side="left", fill="both", expand=True)
            items_frame = tk.Frame(canvas, bg=self._drop_bg)
            cwin = canvas.create_window((0, 0), window=items_frame, anchor="nw")

            def _sync(
                e: "tk.Event[Any]", c: tk.Canvas = canvas, cw: int = cwin
            ) -> None:
                c.configure(scrollregion=c.bbox("all"))
                c.itemconfig(cw, width=c.winfo_width())

            items_frame.bind("<Configure>", _sync)
            canvas.bind("<Configure>", _sync)
            canvas.bind(
                "<MouseWheel>",
                lambda e, c=canvas: (
                    c.yview_scroll(int(-1 * (e.delta / 120)), "units"),
                    "break",
                )[1],
            )
            vsb.bind("<MouseWheel>", lambda e: "break")
            _scroll_canvas = canvas
        else:
            items_frame = inner

        for val in self._values:
            is_sel = val == self._current
            bg = self._drop_hover if is_sel else self._drop_bg
            ifrm = tk.Frame(items_frame, bg=bg, height=item_h)
            ifrm.pack(fill="x")
            ifrm.pack_propagate(False)
            ilbl = tk.Label(
                ifrm,
                text=val,
                bg=bg,
                fg=self._drop_text,
                font=("Segoe UI", 12),
                anchor="w",
                padx=12,
                cursor="hand2",
            )
            ilbl.pack(fill="both", expand=True)

            def _enter(
                e: "tk.Event[Any]", f: tk.Frame = ifrm, label: tk.Label = ilbl
            ) -> None:
                f.configure(bg=self._drop_hover)
                label.configure(bg=self._drop_hover)

            def _leave(
                e: "tk.Event[Any]",
                f: tk.Frame = ifrm,
                label: tk.Label = ilbl,
                v: str = val,
            ) -> None:
                _bg = self._drop_hover if v == self._current else self._drop_bg
                f.configure(bg=_bg)
                label.configure(bg=_bg)

            def _click(e: "tk.Event[Any]", v: str = val) -> None:
                self._select(v)

            def _scroll(
                e: "tk.Event[Any]", c: "tk.Canvas | None" = _scroll_canvas
            ) -> str:
                if c is not None:
                    c.yview_scroll(int(-1 * (e.delta / 120)), "units")
                return "break"

            for widget in (ifrm, ilbl):
                widget.bind("<Enter>", _enter, add="+")
                widget.bind("<Leave>", _leave, add="+")
                widget.bind("<Button-1>", _click, add="+")
                widget.bind("<MouseWheel>", _scroll, add="+")

        # Block scroll events from reaching CTkScrollableFrame's bind_all handler
        for _w in (popup, outer, inner):
            _w.bind("<MouseWheel>", lambda e: "break", add="+")

        popup_h = (visible if len(self._values) > 5 else len(self._values)) * item_h + 2
        popup.geometry(f"{w}x{popup_h}+{x}+{y}")
        popup.lift()

        self._popup = popup
        self._is_open = True
        CustomDropdown._active = self
        self._install_global_handler(self.winfo_toplevel())

    # ── Close popup ───────────────────────────────────────────────────────

    def _close(self) -> None:
        if self._popup:
            try:
                if self._popup.winfo_exists():
                    self._popup.destroy()
            except Exception:
                pass
            self._popup = None
        self._is_open = False
        if CustomDropdown._active is self:
            CustomDropdown._active = None

    def _select(self, value: str) -> None:
        self._current = value
        self._value_label.configure(text=value)
        self._close()
        if self._user_command:
            self._user_command(value)
        if self._virtual_command:
            self._virtual_command(None)

    # ── Public API (drop-in for ModernComboBox) ───────────────────────────

    def get(self) -> str:
        return self._current

    def set(self, value: str) -> None:
        self._current = value
        try:
            self._value_label.configure(text=value)
        except Exception:
            pass

    def current(self, index: int | None = None) -> int | None:
        if index is None:
            try:
                return self._values.index(self._current)
            except ValueError:
                return -1
        if 0 <= index < len(self._values):
            self._current = self._values[index]
            self._value_label.configure(text=self._current)
        return None

    def bind(
        self,
        sequence: str | None = None,
        func: Callable[..., Any] | None = None,
        add: str | None = None,
    ) -> Any:
        if sequence == "<<ComboboxSelected>>":
            self._virtual_command = func
            return None
        return super().bind(sequence, func, add)

    def configure(self, **kwargs: Any) -> None:  # type: ignore[override]
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return

        if "command" in kwargs:
            self._user_command = kwargs.pop("command")

        state = kwargs.pop("state", None)
        values = kwargs.pop("values", None)
        fg = kwargs.pop("fg_color", None)
        bc = kwargs.pop("border_color", None)
        btc = kwargs.pop("button_color", None)
        bth = kwargs.pop("button_hover_color", None)
        tc = kwargs.pop("text_color", None)
        dfg = kwargs.pop("dropdown_fg_color", None)
        dh = kwargs.pop("dropdown_hover_color", None)
        dtc = kwargs.pop("dropdown_text_color", None)

        if state is not None:
            self._enabled = state != "disabled"
            col = self._text_color if self._enabled else self._border_color
            try:
                self._value_label.configure(text_color=col)
                self._arrow.configure(text_color=col)
            except Exception:
                pass
        if values is not None:
            self._values = list(values)
        if bc is not None:
            self._border_color = bc
            try:
                self._sep.configure(fg_color=bc)
            except Exception:
                pass
        if btc is not None:
            self._btn_color = btc
        if bth is not None:
            self._btn_hover = bth
        if tc is not None:
            self._text_color = tc
            if self._enabled:
                try:
                    self._value_label.configure(text_color=tc)
                    self._arrow.configure(text_color=tc)
                except Exception:
                    pass
        if dfg is not None:
            self._drop_bg = dfg
        if dh is not None:
            self._drop_hover = dh
        if dtc is not None:
            self._drop_text = dtc

        parent_kw: dict[str, Any] = {}
        if fg is not None:
            self._fg_color = fg
            parent_kw["fg_color"] = fg
        if bc is not None:
            parent_kw["border_color"] = bc
        all_kw = {**parent_kw, **kwargs}
        if all_kw:
            super().configure(**all_kw)

    config = configure

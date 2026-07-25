"""Discord-style in-app presentation of secondary windows (integrated mode).

With ``Settings.window_style == "integrated"`` the control panel's secondary
windows (settings, history, batch, announcement, themed dialogs) do not open
as separate OS windows: they are stripped of their titlebar
(``overrideredirect``), sized proportionally to the main window, centred over
it, and glued to it — they follow every move/resize, hide on minimize, and
never appear in the taskbar or Alt-Tab. Behind the open panel a borderless
black helper window at 60 % alpha dims the main window's content, so the
panel reads as an in-app modal while the content behind stays live.

Tkinter has no per-widget opacity — a frame inside the window cannot be
semi-transparent — which is why the dim layer is a *window*-level-alpha
toplevel pinned over the main window rather than a widget. On systems
without alpha support the overlay degrades to solid black.

The panels stay real Toplevels, so the existing window-building code
(the view mixins, the dialog functions) runs unchanged; only the window
decoration/placement differs. Esc closes the topmost panel.

Z-order rules (see the s16 dropdown lesson): ``transient()`` does nothing
for overrideredirect windows, so the main window's surface-restore lift
would bury the overlay and panels — AppGUI calls :meth:`ModalHost.raise_all`
after every lift, *before* re-raising dropdown popups (a popup can belong
to a panel and must stack above it).
"""

from __future__ import annotations

import sys
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field

import customtkinter as ctk

from gui.scaling import centered_position

# A panel may occupy at most this fraction of the main window (per axis);
# smaller main windows shrink the panels proportionally, larger ones never
# stretch a panel past its design size.
PANEL_FRACTION = 0.92

# The dim layer's opacity (0 = invisible, 1 = solid black).
DIM_ALPHA = 0.6

# Never shrink a panel below this (logical units) — below that nothing inside
# is usable anyway, and the main window's minsize keeps us out of this regime.
MIN_PANEL_W = 220
MIN_PANEL_H = 160

# Coalesce the <Configure> storm while the main window is dragged/resized.
_FOLLOW_DELAY_MS = 15

_FALLBACK_BORDER = "#263449"


def clamped_panel_size(
    design_w: int,
    design_h: int,
    main_w: float,
    main_h: float,
    fraction: float = PANEL_FRACTION,
) -> tuple[int, int]:
    """The size (logical units) a panel gets inside a main window: its design
    size, clamped to ``fraction`` of the main window per axis, with a usability
    floor. Pure function so the sizing rule is testable without a display."""
    w = min(int(design_w), int(main_w * fraction))
    h = min(int(design_h), int(main_h * fraction))
    return max(MIN_PANEL_W, w), max(MIN_PANEL_H, h)


@dataclass
class _Panel:
    win: tk.Toplevel
    design_w: int
    design_h: int
    close_command: Callable[[], None] | None = None
    close_button: ctk.CTkButton | None = field(default=None, repr=False)


class ModalHost:
    """Presents Toplevels as in-app modal panels locked over ``main``."""

    def __init__(self, main: tk.Misc) -> None:
        self._main = main
        self._overlay: tk.Toplevel | None = None
        self._panels: list[_Panel] = []
        self._follow_job: str | None = None
        self._colors: dict[str, str] | None = None
        main.bind("<Configure>", self._on_main_configure, add="+")
        main.bind("<Unmap>", self._on_main_unmap, add="+")
        main.bind("<Map>", self._on_main_map, add="+")
        # Esc pressed while the main window has focus (e.g. after a click on
        # its titlebar) still closes the topmost panel. Panels additionally
        # bind Esc themselves, so it works wherever the focus sits.
        main.bind("<Escape>", self._on_escape, add="+")

    # ── public API ───────────────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return bool(self._panels)

    def is_presented(self, win: tk.Misc) -> bool:
        return any(p.win is win for p in self._panels)

    def present(
        self,
        win: tk.Toplevel,
        design_w: int,
        design_h: int,
        *,
        close_command: Callable[[], None] | None = None,
        close_button: bool = False,
    ) -> None:
        """Turn ``win`` into an in-app panel: borderless, dimmed backdrop,
        centred + clamped over the main window, glued to it.

        ``close_command`` is what Esc (and the optional floating ✕ button)
        runs; ``None`` means the host never closes this window itself (the
        themed dialogs manage their own result/close logic).

        ``close_button`` floats a small ✕ in the panel's top-right corner —
        for the big windows, which lose their titlebar ✕ in this mode. It is
        created one event-loop turn later so it stacks above the content the
        caller builds after this call returns.
        """
        if self.is_presented(win):
            return
        try:
            # The clamp/centre math reads the main window's laid-out size.
            self._main.update_idletasks()
            win.overrideredirect(True)
        except tk.TclError:
            return  # cannot strip the titlebar: leave the window as-is
        # A borderless panel over a dim layer needs an edge of its own.
        try:
            tk.Toplevel.configure(
                win,
                highlightthickness=2,
                highlightbackground=self._border_color(),
                highlightcolor=self._border_color(),
            )
        except tk.TclError:
            pass
        # Windows 11 rounds the panel via DWM (no-op on Win10/other systems).
        if sys.platform == "win32":
            try:
                import ctypes

                win.update_idletasks()
                hwnd = ctypes.windll.user32.GetAncestor(win.winfo_id(), 2)
                # 33 = DWMWA_WINDOW_CORNER_PREFERENCE, 2 = DWMWCP_ROUND
                pref = ctypes.c_int(2)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 33, ctypes.byref(pref), ctypes.sizeof(pref)
                )
            except Exception:
                pass

        panel = _Panel(win, int(design_w), int(design_h), close_command)
        self._panels.append(panel)
        self._show_overlay()
        self._layout_panel(panel)
        # Full re-stack: the overlay moves up under this new topmost panel,
        # dimming any panel it opened over.
        self.raise_all()

        if close_command is not None:
            win.bind("<Escape>", lambda _e: self._close_panel(panel), add="+")
        win.bind(
            "<Destroy>",
            lambda e: self._release(win) if e.widget is win else None,
            add="+",
        )

        def _finish() -> None:
            if not self.is_presented(win):
                return
            if close_button:
                self._add_close_button(panel)
            self._focus_panel(win)

        # After the caller finished building the window's content.
        win.after(0, _finish)
        # CTkToplevel's titlebar-color helper withdraws + re-deiconifies the
        # window ~500 ms after creation, which can shuffle the stacking —
        # settle the z-order once that dance is over.
        win.after(600, self.raise_all)

    def update_design_size(self, win: tk.Misc, design_w: int, design_h: int) -> None:
        """Re-declare a panel's natural size (auto-height windows recompute it
        when their content changes) and re-clamp/re-centre it."""
        for panel in self._panels:
            if panel.win is win:
                panel.design_w = int(design_w)
                panel.design_h = int(design_h)
                self._layout_panel(panel)
                return

    def raise_all(self) -> None:
        """Re-assert the stacking order: main < older panels < dim overlay <
        topmost panel. The dim sits directly below the NEWEST popup, so a
        dialog over an open panel dims that panel too — only the newest
        popup is bright, focused and clickable; everything underneath is
        dark and click-blocked by the overlay. Also called after the main
        window lifted itself (surface restore)."""
        if not self._panels:
            return
        for panel in self._panels[:-1]:
            if panel.win.winfo_exists():
                self._sync_topmost(panel.win)
                try:
                    panel.win.lift()
                except tk.TclError:
                    pass
        if self._overlay is not None and self._overlay.winfo_exists():
            self._sync_topmost(self._overlay)
            try:
                self._overlay.lift()
            except tk.TclError:
                pass
        top = self._panels[-1]
        if top.win.winfo_exists():
            self._sync_topmost(top.win)
            try:
                top.win.lift()
            except tk.TclError:
                pass

    def update_chrome(self, colors: dict[str, str]) -> None:
        """Follow a runtime theme switch: panel borders + ✕ buttons."""
        self._colors = colors
        border = self._border_color()
        for panel in self._panels:
            if not panel.win.winfo_exists():
                continue
            try:
                tk.Toplevel.configure(
                    panel.win,
                    highlightbackground=border,
                    highlightcolor=border,
                )
            except tk.TclError:
                pass
            btn = panel.close_button
            if btn is not None and btn.winfo_exists():
                btn.configure(
                    fg_color=colors["button"],
                    hover_color=colors["button_hover"],
                    text_color=colors["text"],
                )

    # ── internals ────────────────────────────────────────────────────────────

    def _border_color(self) -> str:
        colors = self._colors or getattr(self._main, "_colors", None)
        if isinstance(colors, dict):
            return colors.get("border", _FALLBACK_BORDER)
        return _FALLBACK_BORDER

    def _close_panel(self, panel: _Panel) -> None:
        if panel.close_command is not None and panel.win.winfo_exists():
            panel.close_command()

    def _on_escape(self, _event: object | None = None) -> None:
        if self._panels:
            self._close_panel(self._panels[-1])

    def _on_overlay_click(self, _event: object | None = None) -> str:
        """Clicking the dim backdrop closes the topmost panel (Discord
        convention, same as Esc) — but only for windows the host may close
        itself (``close_command`` set). Dialogs (alerts/confirms/key entry)
        have explicit OK/Cancel semantics and are never dismissed by a stray
        click; their grab normally swallows the click before it gets here
        anyway. In that case just repair the stacking (the OS raises a
        clicked window — the dim must never end up above the panels) and
        hand focus back so Esc keeps working."""
        try:
            if self._panels and self._panels[-1].close_command is not None:
                top = self._panels[-1]
                self._main.after(0, lambda: self._close_panel(top))
                return "break"
            self._main.after(0, self.raise_all)
            if self._panels:
                win = self._panels[-1].win
                self._main.after(0, lambda: self._focus_panel(win))
        except tk.TclError:
            pass
        return "break"

    def _release(self, win: tk.Misc) -> None:
        self._panels = [p for p in self._panels if p.win is not win]
        if not self._panels:
            self._hide_overlay()
        elif self._panels[-1].win.winfo_exists():
            # The overlay drops back under the new topmost panel (it
            # brightens again), and focus returns so Esc keeps working.
            self.raise_all()
            self._focus_panel(self._panels[-1].win)

    def _focus_panel(self, win: tk.Toplevel) -> None:
        try:
            win.focus_force()
            # focus_force targets the toplevel itself — restore the widget
            # that held focus inside it (e.g. the key dialog's entry).
            inner = win.focus_lastfor()
            if inner is not win:
                inner.focus_set()
        except tk.TclError:
            pass

    def _add_close_button(self, panel: _Panel) -> None:
        colors = self._colors or getattr(self._main, "_colors", None) or {}
        btn = ctk.CTkButton(
            panel.win,
            text="✕",
            command=lambda: self._close_panel(panel),
            width=32,
            height=32,
            corner_radius=16,
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            fg_color=colors.get("button", "#1f2a44"),
            hover_color=colors.get("button_hover", "#263654"),
            text_color=colors.get("text", "#f8fafc"),
        )
        # Clearly inside the panel: clear of the 2 px border, the rounded
        # corner clip and the scrollbar column.
        btn.place(relx=1.0, x=-18, y=14, anchor="ne")
        panel.close_button = btn

    # ── geometry ─────────────────────────────────────────────────────────────

    def _main_alive(self) -> bool:
        try:
            return bool(self._main.winfo_exists())
        except tk.TclError:
            return False

    def _main_logical_size(self) -> tuple[float, float]:
        try:
            scaling = ctk.ScalingTracker.get_window_scaling(self._main)
        except Exception:
            scaling = 1.0
        scaling = scaling or 1.0
        return (
            self._main.winfo_width() / scaling,
            self._main.winfo_height() / scaling,
        )

    def _layout_panel(self, panel: _Panel) -> None:
        if not (self._main_alive() and panel.win.winfo_exists()):
            return
        main_w, main_h = self._main_logical_size()
        w, h = clamped_panel_size(panel.design_w, panel.design_h, main_w, main_h)
        x, y = centered_position(self._main, w, h)
        try:
            panel.win.geometry(f"{w}x{h}+{x}+{y}")
        except tk.TclError:
            pass

    def _layout_overlay(self) -> None:
        overlay = self._overlay
        if overlay is None or not overlay.winfo_exists() or not self._main_alive():
            return
        # Plain tk.Toplevel: geometry is physical px on every axis — match the
        # main window's client area exactly (the titlebar stays usable, so the
        # window can still be moved/minimized/closed while a panel is open).
        try:
            overlay.geometry(
                f"{self._main.winfo_width()}x{self._main.winfo_height()}"
                f"+{self._main.winfo_rootx()}+{self._main.winfo_rooty()}"
            )
        except tk.TclError:
            pass

    # ── overlay lifecycle ────────────────────────────────────────────────────

    def _show_overlay(self) -> None:
        if not self._main_alive():
            return
        if self._overlay is None or not self._overlay.winfo_exists():
            overlay = tk.Toplevel(self._main)
            overlay.overrideredirect(True)
            overlay.configure(bg="#000000")
            try:
                # 60 % black over the live content. Without alpha support the
                # overlay stays solid black — still a working dim layer.
                overlay.attributes("-alpha", DIM_ALPHA)
            except tk.TclError:
                pass
            # A click on any window normally activates AND raises it — which
            # would put the dim layer above the panels: everything goes dark
            # and every further click lands on the overlay (reported as "all
            # windows get dark and I can't do anything"). WS_EX_NOACTIVATE
            # stops the OS from ever foregrounding the overlay on click.
            if sys.platform == "win32":
                try:
                    import ctypes

                    overlay.update_idletasks()
                    hwnd = ctypes.windll.user32.GetAncestor(
                        overlay.winfo_id(), 2
                    )  # GA_ROOT
                    GWL_EXSTYLE = -20
                    WS_EX_NOACTIVATE = 0x08000000
                    style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                    ctypes.windll.user32.SetWindowLongW(
                        hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE
                    )
                    # Changing GWL_EXSTYLE resets a layered window's alpha
                    # RENDERING (hit-testing keeps working and Tk's cached
                    # -alpha still reads 0.6, but nothing is painted — the
                    # dim silently disappears). Re-apply it natively.
                    ctypes.windll.user32.SetLayeredWindowAttributes(
                        hwnd, 0, int(DIM_ALPHA * 255), 2  # LWA_ALPHA
                    )
                    # SWP_NOSIZE|NOMOVE|NOZORDER|FRAMECHANGED — let the style
                    # change take effect without disturbing the stacking.
                    ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
                except Exception:
                    pass
            # Swallow clicks, and repair the stacking in case the click still
            # raised the overlay (non-Windows fallback path).
            overlay.bind("<Button-1>", self._on_overlay_click)
            overlay.bind("<Escape>", self._on_escape)
            self._overlay = overlay
        else:
            try:
                self._overlay.deiconify()
            except tk.TclError:
                pass
        self._layout_overlay()
        self._sync_topmost(self._overlay)
        try:
            self._overlay.lift()
        except tk.TclError:
            pass

    def _hide_overlay(self) -> None:
        if self._overlay is not None and self._overlay.winfo_exists():
            try:
                self._overlay.withdraw()
            except tk.TclError:
                pass

    def _sync_topmost(self, win: tk.Toplevel) -> None:
        """Panels/overlay must share the main window's -topmost: below it they
        could never stack above a topmost main window; above it they would
        float over other apps once the user switches away."""
        try:
            topmost = bool(int(self._main.attributes("-topmost")))
        except (tk.TclError, ValueError, TypeError):
            topmost = False
        try:
            win.attributes("-topmost", topmost)
        except tk.TclError:
            pass

    # ── main-window follow ───────────────────────────────────────────────────

    def _on_main_configure(self, event: object | None = None) -> None:
        # <Configure> reaches the toplevel's bindtag for every descendant —
        # only the main window's own moves/resizes matter here (s16 lesson).
        if event is not None and str(getattr(event, "widget", "")) != str(self._main):
            return
        if not self._panels or self._follow_job is not None:
            return
        try:
            self._follow_job = self._main.after(_FOLLOW_DELAY_MS, self._follow_main)
        except tk.TclError:
            pass

    def _follow_main(self) -> None:
        self._follow_job = None
        if not (self._panels and self._main_alive()):
            return
        self._layout_overlay()
        for panel in self._panels:
            self._layout_panel(panel)

    def _on_main_unmap(self, event: object | None = None) -> None:
        if event is not None and str(getattr(event, "widget", "")) != str(self._main):
            return
        if not self._panels:
            return
        self._hide_overlay()
        for panel in self._panels:
            if panel.win.winfo_exists():
                try:
                    panel.win.withdraw()
                except tk.TclError:
                    pass

    def _on_main_map(self, event: object | None = None) -> None:
        if event is not None and str(getattr(event, "widget", "")) != str(self._main):
            return
        if not self._panels:
            return
        self._show_overlay()
        for panel in self._panels:
            if panel.win.winfo_exists():
                try:
                    panel.win.deiconify()
                except tk.TclError:
                    pass
        self._follow_main()
        self.raise_all()

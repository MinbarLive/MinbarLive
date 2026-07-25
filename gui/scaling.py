"""Global CustomTkinter scaling clamp so the app fits small high-DPI screens.

CTk geometry is in logical units: physical px = logical x DPI scaling. The
window sizes throughout the GUI were chosen on a 2560x1440 @125% monitor,
where they occupy ~50% of the screen. Laptops pair a *high* DPI with a
*small* panel, and the two multiply the wrong way — on 1920x1080 @150% the
same numbers fill 80-100% of the usable height, and at 175% the onboarding
wizard, the settings window and the main window are clipped outright (their
buttons land off-screen).

This module computes one factor that keeps the largest window inside
MAX_SCREEN_FRACTION of the work area and applies it to CTk's *global*
widget/window scaling — so every window (main, settings, batch, history,
onboarding, dialogs) is fixed from a single place.

The factor is never above 1.0: someone who picks 150% on a large monitor
still gets the bigger text they asked for. The clamp only engages when the
design genuinely would not fit, which is exactly the small-screen case.
"""

from __future__ import annotations

import sys

import customtkinter as ctk

from utils.logging import log

# The largest window the app can open, in CTk logical units: the onboarding
# wizard grown for Anthropic's stacked notes (672 tall) and the history
# viewer (900 wide). A factor that fits these fits every other window.
DESIGN_W = 900
DESIGN_H = 672

# A window may occupy at most this fraction of the usable screen area.
MAX_SCREEN_FRACTION = 0.85


def fit_factor(dpi: float, work_w: int, work_h: int) -> float:
    """Scale keeping DESIGN_W x DESIGN_H within MAX_SCREEN_FRACTION of the
    work area, given the monitor's DPI scaling. 1.0 means "no clamp needed".

    Pure function of its arguments so the sizing rule is testable without a
    display.
    """
    if dpi <= 0 or work_w <= 0 or work_h <= 0:
        return 1.0  # nonsense input: never shrink on a bad measurement
    return min(
        1.0,
        (work_w * MAX_SCREEN_FRACTION) / (DESIGN_W * dpi),
        (work_h * MAX_SCREEN_FRACTION) / (DESIGN_H * dpi),
    )


def centered_position(parent, width: int, height: int) -> tuple[int, int]:
    """The ``+x+y`` that centres a *logical* width x height child on ``parent``.

    winfo_rootx/rooty/width/height and the ``+x+y`` of geometry() are all
    physical px, while the WxH of geometry() is logical — so the child's size
    must be scaled up before it can be compared with the parent's. Subtracting
    the logical size directly (as every call site used to) drifts the child
    further off-centre the higher the display scaling.
    """
    scaling = ctk.ScalingTracker.get_window_scaling(parent)
    x = parent.winfo_rootx() + (parent.winfo_width() - int(width * scaling)) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - int(height * scaling)) // 2
    return x, y


def window_work_area(window) -> tuple[int, int, int, int]:
    """``(x, y, width, height)`` in physical px of the usable area of the
    monitor ``window`` currently sits on (taskbar excluded).

    Per *monitor*, not per desktop: growing a window to fit "the screen" must
    use the screen it is actually on, or a window on a small secondary display
    is grown to the primary's width and half of it ends up off-screen.
    Falls back to the primary work area, then to the full screen.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            class _MONITORINFO(ctypes.Structure):
                _fields_ = [
                    ("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD),
                ]

            hwnd = ctypes.windll.user32.GetAncestor(window.winfo_id(), 2)  # GA_ROOT
            # MONITOR_DEFAULTTONEAREST — a window dragged half off-screen still
            # resolves to the monitor it mostly covers.
            monitor = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                r = info.rcWork
                return r.left, r.top, r.right - r.left, r.bottom - r.top
        except Exception:
            pass
    w, h = _work_area(window)
    return 0, 0, w, h


def _work_area(window) -> tuple[int, int]:
    """Usable screen size in physical px, excluding the Windows taskbar."""
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            rect = wintypes.RECT()
            # SPI_GETWORKAREA: the desktop area minus the taskbar/appbars.
            if ctypes.windll.user32.SystemParametersInfoW(
                0x0030, 0, ctypes.byref(rect), 0
            ):
                return rect.right - rect.left, rect.bottom - rect.top
        except Exception:
            pass
    # Non-Windows (CTk reports DPI 1 there, so the clamp rarely engages) or
    # the query failed: fall back to the full screen.
    return window.winfo_screenwidth(), window.winfo_screenheight()


def apply_display_scaling(window, base_widget_scale: float) -> float:
    """Clamp CTk's global scaling to the monitor ``window`` is on.

    Call right after a CTk root is created and before its geometry is set.
    Idempotent — both roots (onboarding and the control panel) call it, and
    each recomputes absolute values from the DPI, which the clamp itself
    does not affect.

    Returns the widget scale actually applied (base_widget_scale when no
    clamp was needed), so the caller can keep using it.
    """
    try:
        dpi = ctk.ScalingTracker.get_window_dpi_scaling(window)
        work_w, work_h = _work_area(window)
        fit = fit_factor(dpi, work_w, work_h)
    except Exception as exc:
        # Never let a scaling probe stop the app from opening.
        log(f"Display scaling clamp skipped: {exc}", level="DEBUG")
        return base_widget_scale

    widget_scale = base_widget_scale * fit
    if fit < 1.0:
        log(
            f"Display scaling: work area {work_w}x{work_h} at DPI {dpi:.2f} is "
            f"too small for the {DESIGN_W}x{DESIGN_H} design — windows scaled "
            f"to {fit:.2f}",
            level="INFO",
        )
    ctk.set_window_scaling(fit)
    ctk.set_widget_scaling(widget_scale)
    return widget_scale

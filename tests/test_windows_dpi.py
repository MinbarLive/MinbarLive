"""Tests for the pre-Tk Windows DPI-awareness bootstrap."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from utils import windows_dpi

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _function(result=None, *, side_effect=None) -> Mock:
    return Mock(return_value=result, side_effect=side_effect)


def _user32(*, awareness: int = 0, context_result: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        GetThreadDpiAwarenessContext=_function(0x22),
        GetAwarenessFromDpiAwarenessContext=_function(awareness),
        SetProcessDpiAwarenessContext=_function(context_result),
        SetProcessDPIAware=_function(True),
    )


def test_non_windows_is_a_noop(monkeypatch):
    monkeypatch.setattr(windows_dpi.sys, "platform", "linux")

    assert windows_dpi.enable_windows_dpi_awareness() is False


def test_uses_customtkinter_compatible_per_monitor_context_before_fallbacks(
    monkeypatch,
):
    user32 = _user32()
    shcore = SimpleNamespace(SetProcessDpiAwareness=_function(0))
    monkeypatch.setattr(windows_dpi.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_dpi.ctypes,
        "windll",
        SimpleNamespace(user32=user32, shcore=shcore),
        raising=False,
    )

    assert windows_dpi.enable_windows_dpi_awareness() is True
    user32.SetProcessDpiAwarenessContext.assert_called_once()
    context = user32.SetProcessDpiAwarenessContext.call_args.args[0]
    assert windows_dpi.ctypes.c_ssize_t(context.value).value == -3
    shcore.SetProcessDpiAwareness.assert_not_called()
    user32.SetProcessDPIAware.assert_not_called()


def test_existing_per_monitor_context_is_idempotent(monkeypatch):
    user32 = _user32(awareness=2)
    monkeypatch.setattr(windows_dpi.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_dpi.ctypes,
        "windll",
        SimpleNamespace(user32=user32, shcore=SimpleNamespace()),
        raising=False,
    )

    assert windows_dpi.enable_windows_dpi_awareness() is True
    user32.SetProcessDpiAwarenessContext.assert_not_called()


def test_older_windows_uses_shcore_fallback(monkeypatch):
    user32 = _user32()
    del user32.SetProcessDpiAwarenessContext
    shcore = SimpleNamespace(SetProcessDpiAwareness=_function(0))
    monkeypatch.setattr(windows_dpi.sys, "platform", "win32")
    monkeypatch.setattr(
        windows_dpi.ctypes,
        "windll",
        SimpleNamespace(user32=user32, shcore=shcore),
        raising=False,
    )

    assert windows_dpi.enable_windows_dpi_awareness() is True
    shcore.SetProcessDpiAwareness.assert_called_once_with(2)
    user32.SetProcessDPIAware.assert_not_called()


def test_legacy_fallback_is_best_effort(monkeypatch):
    user32 = _user32()
    del user32.SetProcessDpiAwarenessContext
    windll = SimpleNamespace(user32=user32)
    monkeypatch.setattr(windows_dpi.sys, "platform", "win32")
    monkeypatch.setattr(windows_dpi.ctypes, "windll", windll, raising=False)

    assert windows_dpi.enable_windows_dpi_awareness() is True
    user32.SetProcessDPIAware.assert_called_once_with()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI API only")
def test_main_bootstrap_is_per_monitor_aware_in_fresh_process():
    """The real entry point must set DPI awareness before any Tk HWND exists."""

    probe = """
import ctypes
import main

user32 = ctypes.windll.user32
user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
context = user32.GetThreadDpiAwarenessContext()
print(user32.GetAwarenessFromDpiAwarenessContext(context))
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "2"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPI API only")
def test_native_window_coordinates_match_monitor_after_bootstrap():
    """Native placement and screeninfo must use the same physical pixels."""

    probe = """
import ctypes
import tkinter as tk
from ctypes import wintypes

import main
from screeninfo import get_monitors

try:
    root = tk.Tk()
except tk.TclError:
    print("SKIP")
    raise SystemExit(0)
root.withdraw()
window = tk.Toplevel(root)
window.withdraw()
window.update_idletasks()
user32 = ctypes.windll.user32
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.SetWindowPos.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowRect.restype = wintypes.BOOL
hwnd = user32.GetParent(window.winfo_id())
monitor = get_monitors()[-1]
assert user32.SetWindowPos(
    hwnd, None, monitor.x, monitor.y, monitor.width, monitor.height, 0x0004
)
window.update_idletasks()
rect = wintypes.RECT()
assert user32.GetWindowRect(hwnd, ctypes.byref(rect))
print(
    monitor.x, monitor.y, monitor.width, monitor.height,
    rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top,
)
window.destroy()
root.destroy()
"""
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    output = completed.stdout.strip()
    if output == "SKIP":
        pytest.skip("Tk display unavailable")
    values = [int(value) for value in output.split()]

    assert values[:4] == values[4:]


def test_packaged_app_manifest_preserves_windows_contracts():
    manifest = (PROJECT_ROOT / "MinbarLive.manifest").read_text(encoding="utf-8")
    compact = "".join(manifest.split())

    assert ">true/pm</dpiAware>" in compact
    assert ">PerMonitor</dpiAwareness>" in compact
    assert "PerMonitorV2" not in compact
    assert ">true</longPathAware>" in compact
    assert "Microsoft.Windows.Common-Controls" in manifest
    assert manifest.count("<supportedOS") == 5


def test_pyinstaller_spec_embeds_the_dpi_manifest():
    spec = (PROJECT_ROOT / "MinbarLive.spec").read_text(encoding="utf-8")

    assert 'MANIFEST_PATH = "MinbarLive.manifest"' in spec
    assert "manifest=MANIFEST_PATH" in spec

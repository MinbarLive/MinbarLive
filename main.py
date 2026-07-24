"""MinbarLive - Main Entry Point."""

import argparse
import os
import sys
from pathlib import Path

# DPI awareness must be configured before the first Tk/CustomTkinter window is
# created. CustomTkinter itself only calls SetProcessDpiAwareness when the first
# CTk object is built, so anything that queries Win32 coordinates or opens a
# plain Tk window before that point runs in a virtualized coordinate space.
# Doing it here makes the awareness deterministic from process start; CTk's
# later call is then a no-op (E_ACCESSDENIED, which ctypes ignores).
from utils.windows_dpi import enable_windows_dpi_awareness

enable_windows_dpi_awareness()

# Set Windows taskbar icon (must be done before tkinter imports)
# Note: sys.platform is always "win32" on Windows, even on 64-bit systems
if sys.platform == "win32":
    try:
        import ctypes

        # This tells Windows to use our app icon in the taskbar instead of Python's
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "MinbarLive.MinbarLive"
        )
    except (AttributeError, OSError):
        pass  # Not on Windows or windll unavailable


def _show_already_running_dialog() -> bool:
    """Show an 'already running' warning dialog.

    Returns True if the user chose 'Launch Anyway', False to abort.
    """
    import customtkinter as ctk

    # ── Load translations + theme from saved settings ─────────────────────
    def _load_settings_data() -> tuple[dict, str]:
        """Returns (translations_dict, theme_mode)."""
        try:
            from config import GUI_TRANSLATIONS_DIR
            from utils.json_helpers import load_json
            from utils.settings import (
                DEFAULT_GUI_LANGUAGE,
                DEFAULT_THEME_MODE,
                load_settings,
            )

            s = load_settings()
            lang = s.gui_language or DEFAULT_GUI_LANGUAGE
            theme = s.theme_mode or DEFAULT_THEME_MODE

            en_path = os.path.join(GUI_TRANSLATIONS_DIR, "en.json")
            base = load_json(en_path)
            if lang != "en":
                try:
                    return (
                        {
                            **base,
                            **load_json(
                                os.path.join(GUI_TRANSLATIONS_DIR, f"{lang}.json")
                            ),
                        },
                        theme,
                    )
                except Exception:
                    pass
            return base, theme
        except Exception:
            return {}, "dark"

    t, theme_mode = _load_settings_data()

    # ── Color palette (mirrors AppGUI._palette) ───────────────────────────
    if theme_mode == "light":
        c_bg = "#f8fafc"
        c_text = "#111827"
        c_btn_bg = "#e2e8f0"
        c_btn_hover = "#cbd5e1"
        c_btn_text = "#111827"
    else:
        c_bg = "#0f172a"
        c_text = "#f8fafc"
        c_btn_bg = "#1f2a44"
        c_btn_hover = "#263654"
        c_btn_text = "#f8fafc"

    # ── Icon paths (same logic as config.py) ──────────────────────────────
    _res_dir = (
        getattr(sys, "_MEIPASS", None)
        if getattr(sys, "frozen", False)
        else os.path.dirname(os.path.abspath(__file__))
    )
    _icon_ico = os.path.join(_res_dir or "", "public", "MinbarLive.ico")
    _icon_png = os.path.join(_res_dir or "", "public", "MinbarLive1.png")

    ctk.set_appearance_mode(theme_mode)
    ctk.set_default_color_theme("green")

    dlg = ctk.CTk()
    dlg.title(t.get("already_running_title", "MinbarLive is already running"))
    dlg.resizable(False, False)
    dlg.configure(fg_color=c_bg)

    W, H = 500, 195
    dlg.update_idletasks()
    sw = dlg.winfo_screenwidth()
    sh = dlg.winfo_screenheight()
    dlg.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 2}")

    from utils.icons import ICO_SUPPORTED, scaled_icon_photo

    if ICO_SUPPORTED and os.path.exists(_icon_ico):
        def set_win_icon():
            try:
                dlg.iconbitmap(_icon_ico)
            except Exception:
                pass
        dlg.after(200, set_win_icon)
    elif os.path.exists(_icon_png):
        try:
            dlg.iconphoto(True, scaled_icon_photo(_icon_png))
        except Exception:
            pass

    launched = [False]

    # ── Body ──────────────────────────────────────────────────────────────
    body = ctk.CTkFrame(dlg, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=24, pady=(22, 10))

    ctk.CTkLabel(
        body,
        text="?",
        font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
        text_color="#ffffff",
        fg_color="#1d4ed8",
        corner_radius=999,
        width=52,
        height=52,
    ).pack(side="left", anchor="n", padx=(0, 16))

    ctk.CTkLabel(
        body,
        text=t.get(
            "already_running_body",
            "MinbarLive is already running or is currently starting up!\n\n"
            "Unless you meant to do this, please shut down the\n"
            "existing instance before starting a new one.",
        ),
        font=ctk.CTkFont(family="Segoe UI", size=13),
        text_color=c_text,
        justify="left",
        anchor="w",
        wraplength=370,
    ).pack(side="left", fill="both", expand=True)

    # ── Buttons ───────────────────────────────────────────────────────────
    btns = ctk.CTkFrame(dlg, fg_color="transparent")
    btns.pack(fill="x", padx=24, pady=(0, 18))

    def _launch() -> None:
        launched[0] = True
        dlg.quit()

    def _cancel() -> None:
        dlg.quit()

    ctk.CTkButton(
        btns,
        text=t.get("already_running_launch_anyway", "Launch Anyway"),
        command=_launch,
        height=44,
        corner_radius=14,
        font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
        fg_color="#1d4ed8",
        hover_color="#1e40af",
        text_color="#ffffff",
    ).pack(side="right", padx=(6, 0))

    ctk.CTkButton(
        btns,
        text=t.get("dlg_cancel", "Cancel"),
        command=_cancel,
        height=44,
        corner_radius=14,
        font=ctk.CTkFont(family="Segoe UI", size=13),
        fg_color=c_btn_bg,
        hover_color=c_btn_hover,
        text_color=c_btn_text,
    ).pack(side="right")

    dlg.protocol("WM_DELETE_WINDOW", _cancel)
    # Raise above the already-running instance's window. lift()/focus_force()
    # alone lose to the other instance's foreground window — the warning then
    # opens behind it (reported on Windows).
    try:
        dlg.attributes("-topmost", True)
    except Exception:
        pass
    dlg.lift()
    dlg.focus_force()
    dlg.mainloop()

    try:
        dlg.destroy()
    except Exception:
        pass

    return launched[0]


def _acquire_posix_instance_lock(lock_dir: Path | None = None) -> int | None:
    """Acquire the single-instance lock on POSIX (Linux/macOS).

    Uses ``flock()`` on a lock file: the lock lives on the open file
    description and the kernel releases it when the process exits, so a crash
    leaves no stale lock (unlike a PID file). Returns the locked file
    descriptor on success — the caller keeps it open for the process lifetime
    — or ``None`` when another instance already holds the lock. Fails open
    (returns a harmless sentinel fd, never ``None``) if the lock file cannot be
    created, so a filesystem problem never blocks launch.
    """
    import fcntl

    from utils.app_paths import get_app_data_dir

    try:
        d = lock_dir if lock_dir is not None else get_app_data_dir()
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / "MinbarLive.lock"), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError:
        return -1  # cannot create the lock file → fail open, do not block launch
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None  # already held by another instance
    return fd


def main() -> None:
    # ── Single-instance guard ────────────────────────────────────────────────
    _instance_mutex = None  # Windows: named-mutex handle
    _instance_lock_fd = None  # POSIX: flock'd lock-file descriptor
    if sys.platform == "win32":
        import ctypes as _ctypes

        _MUTEX_NAME = "MinbarLive_SingleInstance"
        _instance_mutex = _ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        if _ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            if not _show_already_running_dialog():
                _ctypes.windll.kernel32.CloseHandle(_instance_mutex)
                sys.exit(0)
            # "Launch Anyway" — release handle so we don't block future instances
            _ctypes.windll.kernel32.CloseHandle(_instance_mutex)
            _instance_mutex = None
    else:
        # POSIX (Linux/macOS): an flock'd lock file, mirroring the Windows mutex.
        _instance_lock_fd = _acquire_posix_instance_lock()
        if _instance_lock_fd is None:  # lock held → another instance is running
            if not _show_already_running_dialog():
                sys.exit(0)
            # "Launch Anyway" — proceed without the lock; like Windows, this
            # instance then won't block a future one either.
    # The mutex handle / lock fd is never closed here, so the OS keeps it held
    # for the lifetime of this process — released automatically on exit.

    parser = argparse.ArgumentParser(description="MinbarLive - Real-time translation")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Set log level BEFORE importing modules that use logging
    if args.debug:
        import utils.logging as logging_module

        logging_module.LOG_LEVEL = "DEBUG"

    # Load .env early so provider clients can pick up *_API_KEY variables
    # (the keyring is checked first; env vars are the fallback)
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    from app_controller import AppController
    from config import ensure_directories
    from gui.app_gui import AppGUI
    from gui.onboarding import run_onboarding
    from utils.cleanup import run_cleanup
    from utils.settings import load_settings

    # Create necessary directories at startup
    ensure_directories()

    # Built before the wizard, which borrows it for the microphone step's
    # input-level meter (constructing it starts no threads).
    controller = AppController()

    # First-run setup wizard (own Tk root, before the main window so the
    # chosen GUI language/theme applies from the start)
    if not run_onboarding(controller):
        sys.exit(0)

    # Purge stale files (logs and user content gated separately)
    _s = load_settings()
    if _s.auto_cleanup_logs or _s.auto_cleanup_content:
        run_cleanup(
            clean_logs=_s.auto_cleanup_logs, clean_content=_s.auto_cleanup_content
        )

    gui = AppGUI(controller)
    gui.mainloop()


if __name__ == "__main__":
    main()

"""MinbarLive - Main Entry Point."""

import argparse
import os
import sys
from pathlib import Path

# The platform plugin is chosen when the QApplication is built, and the first
# QApplication can be the already-running dialog below — so this runs before
# any import that could reach Qt.
#
# Note what is NOT here any more: enable_windows_dpi_awareness(). Qt sets its
# own per-monitor-aware-v2 context and warns "SetProcessDpiAwarenessContext()
# failed: Access is denied." when the process is already marked aware, so
# calling it first does not duplicate Qt's work — it takes the setting away
# from it. Qt is DPI-aware natively and needs nothing here.
from gui.platform_setup import prepare_qt_platform

prepare_qt_platform()

# Set Windows taskbar icon (must be done before the first window)
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
    from gui.already_running import show_already_running_dialog

    return show_already_running_dialog()


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
    from utils.cleanup import run_cleanup
    from utils.settings import load_settings

    # Create necessary directories at startup
    ensure_directories()

    # Built before the wizard, which borrows it for the microphone step's
    # input-level meter (constructing it starts no threads).
    controller = AppController()

    # Purge stale files (logs and user content gated separately)
    _s = load_settings()
    if _s.auto_cleanup_logs or _s.auto_cleanup_content:
        run_cleanup(
            clean_logs=_s.auto_cleanup_logs, clean_content=_s.auto_cleanup_content
        )

    # The first-run wizard runs from inside gui.app.run, on the same
    # QApplication as the control panel, so the chosen language and theme apply
    # from the start.
    from gui.app import run

    sys.exit(run(controller))


if __name__ == "__main__":
    main()

"""One-time consented ffmpeg download into the app data dir (Windows).

Batch mode needs ffmpeg to convert anything that is not already a 16 kHz
WAV. Most users don't have it installed, and bundling a static build would
grow the EXE by ~90 MB — so the batch card offers a one-time download to
``%APPDATA%/MinbarLive/bin/ffmpeg.exe`` instead. ``_find_ffmpeg`` in
``batch/processor.py`` picks that copy up automatically; users with a
system ffmpeg never hit this module.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable

from utils.app_paths import get_app_data_dir
from utils.logging import log

# Official Windows release build recommended by ffmpeg.org (gyan.dev).
FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Approximate download size shown in the consent prompt (the release
# essentials zip has hovered around this for years).
FFMPEG_DOWNLOAD_MB = 90

_CHUNK_BYTES = 256 * 1024


class FfmpegDownloadCancelled(RuntimeError):
    """Raised when the cancel event is set mid-download."""


def bundled_ffmpeg_path() -> str:
    """Where the app-managed ffmpeg.exe lives (may not exist yet)."""
    return str(get_app_data_dir() / "bin" / "ffmpeg.exe")


def download_ffmpeg(
    progress_cb: Callable[[int], None] | None = None,
    cancel_event=None,
) -> str:
    """Download the ffmpeg zip and install ffmpeg.exe into the app data dir.

    Args:
        progress_cb: called with 0-100 as the download advances.
        cancel_event: optional threading.Event; when set, the download stops
            with FfmpegDownloadCancelled and nothing is installed.

    Returns:
        The path of the installed ffmpeg.exe.
    """
    target = bundled_ffmpeg_path()
    os.makedirs(os.path.dirname(target), exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        zip_path = os.path.join(tmp_dir, "ffmpeg.zip")
        request = urllib.request.Request(
            FFMPEG_ZIP_URL, headers={"User-Agent": "MinbarLive"}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(zip_path, "wb") as out:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise FfmpegDownloadCancelled()
                    chunk = resp.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress_cb and total:
                        progress_cb(min(99, done * 100 // total))
        extract_ffmpeg_exe(zip_path, target)

    if progress_cb:
        progress_cb(100)
    log(f"ffmpeg downloaded to {target}", level="INFO")
    return target


def extract_ffmpeg_exe(zip_path: str, target: str) -> str:
    """Extract ffmpeg.exe from a release zip to ``target`` (atomic replace)."""
    with zipfile.ZipFile(zip_path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.replace("\\", "/").lower().endswith("/bin/ffmpeg.exe")
            or name.lower() == "ffmpeg.exe"
        ]
        if not names:
            raise RuntimeError("ffmpeg.exe not found in the downloaded archive")
        partial = target + ".part"
        with archive.open(names[0]) as src, open(partial, "wb") as dst:
            shutil.copyfileobj(src, dst)
        os.replace(partial, target)
    return target

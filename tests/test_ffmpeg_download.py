"""Tests for the one-time ffmpeg download (no network — synthetic zips)."""

import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from batch import processor
from utils.ffmpeg_download import bundled_ffmpeg_path, extract_ffmpeg_exe


def _make_zip(path: Path, inner_name: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(inner_name, b"fake-ffmpeg-binary")


class TestExtractFfmpegExe:
    def test_extracts_from_release_layout(self, tmp_path):
        """gyan.dev release zips nest the exe under <build>/bin/ffmpeg.exe."""
        zip_path = tmp_path / "ffmpeg.zip"
        _make_zip(zip_path, "ffmpeg-7.1-essentials_build/bin/ffmpeg.exe")
        target = tmp_path / "bin" / "ffmpeg.exe"
        target.parent.mkdir()
        result = extract_ffmpeg_exe(str(zip_path), str(target))
        assert result == str(target)
        assert target.read_bytes() == b"fake-ffmpeg-binary"
        assert not target.with_suffix(".exe.part").exists()

    def test_missing_exe_in_archive_raises(self, tmp_path):
        zip_path = tmp_path / "ffmpeg.zip"
        _make_zip(zip_path, "readme.txt")
        target = tmp_path / "ffmpeg.exe"
        with pytest.raises(RuntimeError, match="not found"):
            extract_ffmpeg_exe(str(zip_path), str(target))
        assert not target.exists()

    def test_replaces_existing_copy(self, tmp_path):
        zip_path = tmp_path / "ffmpeg.zip"
        _make_zip(zip_path, "build/bin/ffmpeg.exe")
        target = tmp_path / "ffmpeg.exe"
        target.write_bytes(b"old")
        extract_ffmpeg_exe(str(zip_path), str(target))
        assert target.read_bytes() == b"fake-ffmpeg-binary"


class TestFindFfmpegBundled:
    def test_bundled_copy_found_when_which_fails(self, tmp_path, monkeypatch):
        """The app-managed download is picked up without a system install."""
        bundled = tmp_path / "bin" / "ffmpeg.exe"
        bundled.parent.mkdir()
        bundled.write_bytes(b"x")
        monkeypatch.setattr(processor.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            "utils.ffmpeg_download.bundled_ffmpeg_path", lambda: str(bundled)
        )
        assert processor._find_ffmpeg() == str(bundled)

    def test_system_ffmpeg_wins_over_bundled(self, monkeypatch):
        monkeypatch.setattr(
            processor.shutil, "which", lambda name: r"C:\tools\ffmpeg.exe"
        )
        assert processor._find_ffmpeg() == r"C:\tools\ffmpeg.exe"

    def test_bundled_path_is_under_app_data(self):
        path = bundled_ffmpeg_path()
        assert path.endswith(os.path.join("bin", "ffmpeg.exe"))
        assert "MinbarLive" in path


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

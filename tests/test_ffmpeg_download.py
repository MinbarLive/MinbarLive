"""Tests for the one-time ffmpeg download (no network — synthetic zips)."""

import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from batch import processor
from utils import ffmpeg_download
from utils.ffmpeg_download import (
    bundled_ffmpeg_path,
    extract_ffmpeg_exe,
    ffmpeg_install_command,
)


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


class TestFfmpegInKnownDirs:
    """Launched from Finder or an AppImage, the process gets a minimal PATH
    and `which` misses a Homebrew ffmpeg that is definitely installed."""

    def _make_exe(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        binary = directory / "ffmpeg"
        binary.write_bytes(b"x")
        os.chmod(binary, 0o755)  # no-op on Windows, required on POSIX
        return binary

    def test_found_in_a_prefix_that_is_not_on_path(self, tmp_path, monkeypatch):
        binary = self._make_exe(tmp_path / "opt" / "homebrew" / "bin")
        monkeypatch.setattr(
            processor, "_EXTRA_FFMPEG_DIRS", (str(binary.parent),)
        )
        assert processor._ffmpeg_in_known_dirs() == str(binary)

    def test_the_earlier_prefix_wins(self, tmp_path, monkeypatch):
        # Apple Silicon Homebrew before /usr/local, so a Mac carrying both an
        # arm64 and a leftover Intel copy runs the native one.
        first = self._make_exe(tmp_path / "first")
        second = self._make_exe(tmp_path / "second")
        monkeypatch.setattr(
            processor,
            "_EXTRA_FFMPEG_DIRS",
            (str(first.parent), str(second.parent)),
        )
        assert processor._ffmpeg_in_known_dirs() == str(first)

    def test_nothing_installed_is_still_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(processor, "_EXTRA_FFMPEG_DIRS", (str(tmp_path),))
        assert processor._ffmpeg_in_known_dirs() is None

    def test_a_directory_named_ffmpeg_is_not_mistaken_for_the_binary(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / "ffmpeg").mkdir()
        monkeypatch.setattr(processor, "_EXTRA_FFMPEG_DIRS", (str(tmp_path),))
        assert processor._ffmpeg_in_known_dirs() is None

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="the prefix scan is the non-Windows branch of _find_ffmpeg",
    )
    def test_find_ffmpeg_consults_the_prefixes(self, tmp_path, monkeypatch):
        """The wiring, not just the helper: `which` misses, nothing is
        bundled, and the prefix scan is what answers."""
        binary = self._make_exe(tmp_path / "bin")
        monkeypatch.setattr(processor.shutil, "which", lambda name: None)
        monkeypatch.setattr(
            processor, "_EXTRA_FFMPEG_DIRS", (str(binary.parent),)
        )
        assert processor._find_ffmpeg() == str(binary)


class TestFfmpegInstallCommand:
    """Step 1 of #38: off Windows there is no download to offer, so the
    error has to carry the command instead of being a dead end."""

    def test_macos_names_homebrew(self):
        assert ffmpeg_install_command("darwin") == "brew install ffmpeg"

    def test_windows_has_none_because_the_card_offers_a_download(self):
        assert ffmpeg_install_command("win32") is None

    @pytest.mark.parametrize(
        "installed,expected",
        [
            ("apt-get", "sudo apt install ffmpeg"),
            ("dnf", "sudo dnf install ffmpeg"),
            ("pacman", "sudo pacman -S ffmpeg"),
            ("zypper", "sudo zypper install ffmpeg"),
        ],
    )
    def test_linux_names_the_package_manager_it_finds(
        self, installed, expected, monkeypatch
    ):
        monkeypatch.setattr(
            ffmpeg_download.shutil,
            "which",
            lambda name: "/usr/bin/" + name if name == installed else None,
        )
        assert ffmpeg_install_command("linux") == expected

    def test_an_unknown_distribution_still_gets_an_answer(self, monkeypatch):
        # Or the same minimal PATH that hid ffmpeg in the first place. A
        # command that may be wrong is correctable; no command is the dead end.
        monkeypatch.setattr(ffmpeg_download.shutil, "which", lambda name: None)
        assert ffmpeg_install_command("linux") == "sudo apt install ffmpeg"

    def test_the_platform_argument_is_only_for_tests(self):
        # Production callers pass nothing and must get this machine's answer.
        assert ffmpeg_install_command() == ffmpeg_install_command(sys.platform)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

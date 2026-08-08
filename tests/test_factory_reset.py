"""Tests for utils/factory_reset.py — the irreversible one.

Read ``_never_touch_the_real_app_data`` below before adding a test here. This
file drives code whose entire job is to delete the app-data directory and write
nothing back, and both halves have a way of reaching the real one.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.factory_reset as fr
import utils.logging as ulog
import utils.settings as settings_module


@pytest.fixture(autouse=True)
def app_data_root(tmp_path, monkeypatch):
    """Point every app-data path in this file inside tmp_path.

    Autouse and not optional. ``get_app_data_dir`` is imported BY NAME into
    both modules, so patching one of them leaves the other aimed at the real
    directory — and the two failure modes are silent and destructive:
    ``factory_reset`` deletes the developer's own settings, history and
    recordings, and ``save_settings`` writes a defaults file over their real
    one. The second is not hypothetical: it happened during this file's own
    mutation review, because the test patched ``factory_reset``'s copy of the
    name and then called ``save_settings``, which used ``utils.settings``'s.
    (gui/AGENTS.md makes the same point about ``save_settings`` itself.)
    """
    root = tmp_path / "MinbarLive"
    monkeypatch.setattr(fr, "get_app_data_dir", lambda: root)
    monkeypatch.setattr(settings_module, "get_app_data_dir", lambda: root)
    return root


@pytest.fixture
def data_dir(app_data_root):
    """A populated stand-in for %APPDATA%/MinbarLive."""
    root = app_data_root
    (root / "history").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "history" / "2026-08-08.txt").write_text("a session", encoding="utf-8")
    (root / "settings.json").write_text('{"onboarding_completed": true}', "utf-8")
    return root


@pytest.fixture(autouse=True)
def _never_leak_the_write_block():
    """The block is process-wide: a test that leaves it set turns every later
    save_settings in the run into a silent no-op."""
    yield
    settings_module.block_writes(False)


@pytest.fixture
def no_keys(monkeypatch):
    """No provider has a configured key — the machine-independent baseline.

    Without this the result depends on whose keychain the suite is running on,
    and on a developer box it would really delete their keys.
    """
    import providers

    monkeypatch.setattr(providers, "has_configured_key", lambda p: False)
    monkeypatch.setattr(providers, "clear_api_key", lambda p: None)


class TestTheFolder:
    def test_the_whole_tree_goes(self, data_dir, no_keys):
        result = fr.factory_reset()
        assert result.data_dir_removed
        assert not data_dir.exists()
        assert result.ok
        assert result.errors == []

    def test_a_folder_that_was_never_there_counts_as_removed(
        self, app_data_root, no_keys
    ):
        assert not app_data_root.exists()  # the fixture creates no tree
        result = fr.factory_reset()
        assert result.ok
        assert result.data_dir_removed

    def test_a_failed_rmtree_is_reported_not_raised(
        self, data_dir, monkeypatch, no_keys
    ):
        def boom(_path):
            raise OSError("in use by another process")

        monkeypatch.setattr(fr.shutil, "rmtree", boom)
        result = fr.factory_reset()
        assert not result.ok
        assert not result.data_dir_removed
        assert any("in use by another process" in e for e in result.errors)
        assert data_dir.exists()

    def test_removed_is_read_back_from_disk_not_inferred(
        self, data_dir, monkeypatch, no_keys
    ):
        # A partial rmtree raises on the first file it cannot delete and leaves
        # the rest standing. "Removed" has to mean the folder is gone, so the
        # flag comes from exists() — an rmtree that silently does nothing must
        # not report success.
        monkeypatch.setattr(fr.shutil, "rmtree", lambda _p: None)
        result = fr.factory_reset()
        assert not result.data_dir_removed
        assert not result.ok


class TestTheKeychain:
    def test_configured_keys_are_cleared_and_named(
        self, data_dir, monkeypatch
    ):
        import providers

        held = {"openai", "deepgram"}
        monkeypatch.setattr(providers, "has_configured_key", lambda p: p in held)
        monkeypatch.setattr(providers, "clear_api_key", lambda p: held.discard(p))
        result = fr.factory_reset()
        assert result.keys_removed == ["openai", "deepgram"]
        assert held == set()
        assert result.ok

    def test_a_provider_with_no_key_is_not_reported_as_removed(
        self, data_dir, no_keys
    ):
        result = fr.factory_reset()
        assert result.keys_removed == []
        assert result.ok

    def test_a_key_that_survives_the_delete_is_an_error(self, data_dir, monkeypatch):
        # clear_api_key returns None and swallows the keychain's refusal
        # (delete_api_key_from_keyring logs it and returns False), so trusting
        # the call would report a key as removed while it is still there.
        import providers

        monkeypatch.setattr(providers, "has_configured_key", lambda p: p == "gemini")
        monkeypatch.setattr(providers, "clear_api_key", lambda p: None)
        result = fr.factory_reset()
        assert result.keys_removed == []
        assert any("gemini" in e for e in result.errors)
        assert not result.ok
        # …and the folder is still deleted: doing as much as possible and
        # naming the rest beats leaving the user with both halves.
        assert result.data_dir_removed

    def test_one_provider_raising_does_not_stop_the_others(
        self, data_dir, monkeypatch
    ):
        import providers

        held = {"openai", "gemini", "anthropic", "deepgram"}

        def clear(provider):
            if provider == "gemini":
                raise RuntimeError("keychain locked")
            held.discard(provider)

        monkeypatch.setattr(providers, "has_configured_key", lambda p: p in held)
        monkeypatch.setattr(providers, "clear_api_key", clear)
        result = fr.factory_reset()
        assert result.keys_removed == ["openai", "anthropic", "deepgram"]
        assert any("keychain locked" in e for e in result.errors)


class TestNothingWritesTheFolderBackAfterwards:
    """The two ways the folder comes back the moment it is deleted."""

    def test_save_settings_is_blocked_after_a_successful_reset(
        self, data_dir, no_keys
    ):
        # closeEvent persists the window geometry, and save_settings mkdirs its
        # parent — so without the block the app recreates the folder on the way
        # out, with onboarding_completed still true. The next launch would then
        # skip the wizard and the reset would have deleted the history for
        # nothing.
        fr.factory_reset()
        assert not data_dir.exists()
        settings_module.save_settings(settings_module.Settings())
        # The folder itself, not just the file: save_settings mkdirs the parent
        # before writing, so a blocked write has to leave neither behind.
        assert not data_dir.exists(), "save_settings put the app-data folder back"
        assert not (data_dir / "settings.json").exists()

    def test_a_failed_reset_does_not_block_writes(
        self, data_dir, monkeypatch, no_keys
    ):
        # Nothing was deleted, so the install is still live and its settings
        # still matter. Blocking here would silently drop real preferences.
        monkeypatch.setattr(fr.shutil, "rmtree", lambda _p: None)
        fr.factory_reset()
        assert settings_module._writes_blocked is False

    def test_logging_does_not_recreate_the_log_directory(self, tmp_path):
        # factory_reset logs its own outcome AFTER the delete, and the panel
        # keeps logging for as long as it is up. _write_to_file opens in "a"
        # mode and creates no directory, which is the whole reason that is
        # safe — if it ever grew a mkdir, "the folder is gone" would stop being
        # true a millisecond after it was reported.
        missing = tmp_path / "logs-that-are-gone"
        original = ulog.LOGS_DIR
        ulog.LOGS_DIR = str(missing)
        try:
            ulog.log("after the reset", level="INFO")
        finally:
            ulog.LOGS_DIR = original
        assert not missing.exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

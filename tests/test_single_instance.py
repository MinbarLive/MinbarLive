"""POSIX single-instance lock (main._acquire_posix_instance_lock).

flock() is POSIX-only, so these are skipped on Windows (which uses a named
mutex instead). They run in Linux/macOS CI.
"""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX flock lock; Windows uses a named mutex",
)


def test_second_acquire_detects_running_instance(tmp_path):
    import main

    fd1 = main._acquire_posix_instance_lock(tmp_path)
    assert fd1 is not None and fd1 >= 0
    try:
        # A second acquire opens an independent file description, so flock()
        # must report the lock as already held (returns None).
        assert main._acquire_posix_instance_lock(tmp_path) is None
    finally:
        os.close(fd1)


def test_acquire_succeeds_after_release(tmp_path):
    import main

    fd1 = main._acquire_posix_instance_lock(tmp_path)
    assert fd1 is not None and fd1 >= 0
    os.close(fd1)  # releasing the fd drops the flock

    fd2 = main._acquire_posix_instance_lock(tmp_path)
    assert fd2 is not None and fd2 >= 0
    os.close(fd2)


def test_creates_lock_dir_if_missing(tmp_path):
    import main

    target = tmp_path / "nested" / "MinbarLive"
    assert not target.exists()
    fd = main._acquire_posix_instance_lock(target)
    try:
        assert fd is not None and fd >= 0
        assert (target / "MinbarLive.lock").exists()
    finally:
        os.close(fd)

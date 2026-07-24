"""Tests for utils/logging.py file persistence.

The file append is serialised by a lock: Windows' CRT implements append mode
as a seek-to-end followed by a write, which is not atomic across handles —
two threads logging within the same millisecond could interleave those steps
and tear a line (observed live 2026-07-24 in a real session log: a complete
line followed by the surviving tail of the clobbered one).
"""

import re
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import utils.logging as ulog


def _drain_log_queue() -> None:
    while not ulog.log_queue.empty():
        try:
            ulog.log_queue.get_nowait()
        except Exception:
            break


def test_concurrent_log_calls_never_tear_lines(tmp_path, monkeypatch):
    monkeypatch.setattr(ulog, "LOGS_DIR", str(tmp_path))
    n_threads, n_lines = 8, 50
    barrier = threading.Barrier(n_threads)

    def worker(tid: int) -> None:
        barrier.wait()  # release together to force write collisions
        for i in range(n_lines):
            ulog.log(f"thread {tid} line {i} " + "x" * 40, level="INFO")

    threads = [
        threading.Thread(target=worker, args=(t,)) for t in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)
    _drain_log_queue()

    lines: list[str] = []
    for f in tmp_path.glob("*.log"):  # normally one file; two if midnight rolls
        lines.extend(f.read_text(encoding="utf-8").splitlines())
    assert len(lines) == n_threads * n_lines
    well_formed = re.compile(
        r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[INFO\] thread \d+ line \d+ x{40}$"
    )
    for line in lines:
        assert well_formed.match(line), f"torn log line: {line!r}"


def test_log_writes_one_well_formed_line(tmp_path, monkeypatch):
    monkeypatch.setattr(ulog, "LOGS_DIR", str(tmp_path))
    ulog.log("hello world", level="WARNING")
    _drain_log_queue()
    files = list(tmp_path.glob("*.log"))
    assert len(files) == 1
    (line,) = files[0].read_text(encoding="utf-8").splitlines()
    assert re.match(r"^\[\d{2}:\d{2}:\d{2}\.\d{3}\] \[WARNING\] hello world$", line)

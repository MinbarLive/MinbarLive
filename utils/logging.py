"""Logging utilities with queue-based async logging and file persistence."""

from __future__ import annotations

import os
import queue
import re
from datetime import datetime

from config import LOGS_DIR

# Log queue for thread-safe logging (consumed by GUI)
log_queue: queue.Queue[str] = queue.Queue()

# Log level configuration
LOG_LEVEL = "INFO"
_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 25, "ERROR": 30}
_SECRET_SHAPE_RE = re.compile(
    r"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_*.-]{4,}|AIza[A-Za-z0-9_-]{20,})"
)
_AUTH_VALUE_RE = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|bearer)(\s*[:=]?\s*)([A-Za-z0-9._-]{12,})"
)


def redact_sensitive_text(value: object) -> str:
    """Remove credential-shaped values before they reach memory or disk logs."""

    text = _SECRET_SHAPE_RE.sub("[REDACTED]", str(value))
    return _AUTH_VALUE_RE.sub(r"\1\2[REDACTED]", text)


def _write_to_file(formatted_msg: str) -> None:
    """Append a log message to today's log file."""
    try:
        date_str = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(LOGS_DIR, f"{date_str}.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(formatted_msg + "\n")
    except Exception:  # noqa: BLE001 – logging must never crash the app
        pass


def log(msg: str, level: str = "INFO") -> None:
    """
    Add a log message to the queue with timestamp and persist to daily log file.
    Messages below the current LOG_LEVEL are ignored.
    """
    if _LEVEL_ORDER.get(level, 20) < _LEVEL_ORDER.get(LOG_LEVEL, 20):
        return
    timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    formatted = f"[{timestamp}] [{level}] {redact_sensitive_text(msg)}"
    log_queue.put(formatted)
    _write_to_file(formatted)

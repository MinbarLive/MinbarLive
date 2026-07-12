"""File retention cleanup for logs and history directories."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta

from config import (
    BATCH_DIR,
    BATCH_RETENTION_DAYS,
    HISTORY_DIR,
    HISTORY_RETENTION_DAYS,
    LOGS_DIR,
    LOGS_RETENTION_DAYS,
)
from utils.logging import log

# Matches filenames like 2026-03-07.log or 2026-03-07.txt
_DATE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\w+$")
# Matches batch records/sidecars like 2026-03-07_153012_khutbah.txt / .summary
_BATCH_DATE_PATTERN = re.compile(r"^(\d{4}-\d{2}-\d{2})_\d{6}_.*\.\w+$")


def _purge_old_files(
    directory: str, retention_days: int, pattern: re.Pattern = _DATE_PATTERN
) -> int:
    """Delete files older than *retention_days* based on the date in the filename.

    Only files whose name matches *pattern* (leading ``YYYY-MM-DD`` in group 1)
    are considered. Returns the number of files deleted.
    """
    if not os.path.isdir(directory):
        return 0

    cutoff = datetime.now() - timedelta(days=retention_days)
    deleted = 0

    for filename in os.listdir(directory):
        match = pattern.match(filename)
        if not match:
            continue
        try:
            file_date = datetime.strptime(match.group(1), "%Y-%m-%d")
        except ValueError:
            continue

        if file_date < cutoff:
            try:
                os.remove(os.path.join(directory, filename))
                deleted += 1
            except OSError as e:
                log(f"Cleanup: failed to delete {filename}: {e}", level="WARNING")

    return deleted


def run_cleanup(clean_logs: bool = True, clean_content: bool = True) -> None:
    """Purge stale files. Logs are diagnostics; history + batch are user
    content — the two are gated separately so content is never deleted unless
    the user opted in. Safe to call at every startup.
    """
    logs_removed = _purge_old_files(LOGS_DIR, LOGS_RETENTION_DAYS) if clean_logs else 0
    history_removed = (
        _purge_old_files(HISTORY_DIR, HISTORY_RETENTION_DAYS) if clean_content else 0
    )
    batch_removed = (
        _purge_old_files(BATCH_DIR, BATCH_RETENTION_DAYS, _BATCH_DATE_PATTERN)
        if clean_content
        else 0
    )

    if logs_removed or history_removed or batch_removed:
        log(
            f"Cleanup: removed {logs_removed} log(s), {history_removed} "
            f"history file(s), {batch_removed} batch file(s)",
            level="INFO",
        )

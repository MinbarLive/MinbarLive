"""History logging for transcriptions and translations."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from config import BATCH_DIR, HISTORY_DIR, LOGS_DIR
from utils.logging import log
from utils.settings import load_settings

# Matches entry lines written by log_transcription_and_translation:
# "[HH:MM:SS] XX: text"
_ENTRY_RE = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\] ([A-Z]{2}): (.*)$")
_PROCESSING_PREFIX = "[Processing time]"
# Header lines of a batch record: "# source: <name>", "# formats: srt,txt",
# "# langs: <source name>|<target name>". All sit before the first blank line.
_BATCH_SOURCE_PREFIX = "# source: "
_BATCH_FORMATS_PREFIX = "# formats: "
_BATCH_LANGS_PREFIX = "# langs: "
_VALID_FORMATS = ("srt", "txt")
# Matches daily history filenames like "2026-07-03.txt"
_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.txt$")
# Matches daily log filenames like "2026-07-03.log"
_LOG_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.log$")
# Matches batch record filenames like "2026-07-03_153012_khutbah.txt"
_BATCH_FILENAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_(\d{6})_.*\.txt$")


@dataclass
class HistoryEntry:
    """One transcription or translation line from a history file."""

    time: str  # "HH:MM:SS"
    lang: str  # two-letter tag as written, e.g. "AR", "GE"
    text: str


@dataclass
class HistorySession:
    """Summary of one daily history file for the session viewer."""

    date: str  # "YYYY-MM-DD"
    path: str
    start_time: str  # "HH:MM"
    end_time: str  # "HH:MM"
    duration_minutes: int  # first→last span (metadata; not the displayed value)
    active_seconds: int  # real translation time — idle gaps between runs excluded
    language_pair: str  # e.g. "AR → GE"; comma-joined if it changed mid-day
    entry_count: int  # transcription/translation pairs
    has_summary: bool = False  # a saved AI summary sidecar exists for this day


@dataclass
class LogFile:
    """One daily log file for the log viewer."""

    date: str  # "YYYY-MM-DD"
    path: str
    size_kb: int


@dataclass
class BatchRun:
    """Summary of one batch-processed file for the batch viewer."""

    date: str  # "YYYY-MM-DD" the file was processed
    time: str  # "HH:MM" the file was processed
    source_name: str  # original file basename, e.g. "khutbah.mp4"
    path: str  # the batch record file
    duration_minutes: int  # first→last span (metadata; not the displayed value)
    active_seconds: int  # audio length covered by the transcript (idle excluded)
    language_pair: str  # e.g. "AR → GE"
    entry_count: int  # transcription/translation pairs
    has_summary: bool = False
    # Output formats viewable/exportable for this run, e.g. ["srt", "txt"].
    formats: list[str] = field(default_factory=list)


def summary_path(history_path: str) -> str:
    """Sidecar path for a daily history file's saved summary.

    ``.../history/2026-07-03.txt`` → ``.../history/2026-07-03.summary``.
    The single-extension, date-prefixed name means the retention cleanup
    (``utils.cleanup``) purges it on the same schedule as the history file,
    while ``list_history_sessions`` (which matches ``*.txt``) ignores it.
    """
    return os.path.splitext(history_path)[0] + ".summary"


def read_summary(history_path: str) -> str | None:
    """Return the saved summary for a daily history file, or None if absent."""
    path = summary_path(history_path)
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def write_summary(history_path: str, text: str) -> None:
    """Persist a generated summary next to its daily history file."""
    with open(summary_path(history_path), "w", encoding="utf-8") as f:
        f.write(text)


def log_transcription_and_translation(
    transcription: str,
    translation: str,
    duration: float | None = None,
) -> None:
    """
    Log a transcription and its translation to the daily history file.

    Args:
        transcription: The original transcribed text.
        translation: The translated text.
        duration: Optional processing duration in seconds.
    """
    try:
        settings = load_settings()
        source_lang = settings.source_language[:2].upper()  # e.g., "AR", "TR", "UR"
        target_lang = settings.target_language[:2].upper()  # e.g., "DE", "EN", "FR"

        date_str = datetime.now().strftime("%Y-%m-%d")
        log_path = os.path.join(HISTORY_DIR, f"{date_str}.txt")
        timestamp = datetime.now().strftime("%H:%M:%S")

        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"[{timestamp}] {source_lang}: {transcription}\n")
            f.write(f"[{timestamp}] {target_lang}: {translation}\n")
            if duration is not None:
                f.write(f"[Processing time]: {duration:.2f}s\n")
            f.write("\n")
    except Exception as e:
        log(f"History write error: {e}", level="ERROR")


def parse_history_file(path: str) -> list[HistoryEntry]:
    """
    Parse a daily history file into entries.

    Lines that match the entry format start a new entry; "[Processing time]"
    and blank lines are skipped; any other line is a continuation of the
    previous entry's text (transcriptions may contain newlines).
    """
    entries: list[HistoryEntry] = []
    with open(path, encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            match = _ENTRY_RE.match(line)
            if match:
                entries.append(HistoryEntry(*match.groups()))
            elif line.startswith(_PROCESSING_PREFIX) or not line.strip():
                continue
            elif entries:
                entries[-1].text += "\n" + line
    return entries


def pair_entries(
    entries: list[HistoryEntry],
) -> list[tuple[HistoryEntry, HistoryEntry | None]]:
    """
    Group entries into (transcription, translation) pairs by write order.

    The writer always emits source then target, so consecutive entries form a
    pair; a trailing unmatched entry (interrupted write) pairs with None.
    """
    pairs: list[tuple[HistoryEntry, HistoryEntry | None]] = []
    for i in range(0, len(entries), 2):
        source = entries[i]
        target = entries[i + 1] if i + 1 < len(entries) else None
        pairs.append((source, target))
    return pairs


def _language_pairs(
    pairs: list[tuple[HistoryEntry, HistoryEntry | None]],
) -> str:
    """Comma-joined distinct language pairs (e.g. "AR → GE"), in order seen."""
    seen: list[str] = []
    for source, target in pairs:
        if target is None:
            continue
        label = f"{source.lang} → {target.lang}"
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


# Gaps longer than this between consecutive entries mean the app was stopped
# between two separate runs (the daily file holds all of a day's sessions with
# no delimiter) — that idle time is excluded from the reported duration. A gap
# tolerates normal within-khutbah pauses; distinct runs are minutes-to-hours
# apart.
_ACTIVE_GAP_SECONDS = 300  # 5 minutes


def _duration_seconds(start: str, end: str) -> int:
    try:
        delta = datetime.strptime(end, "%H:%M:%S") - datetime.strptime(
            start, "%H:%M:%S"
        )
        return max(0, int(delta.total_seconds()))
    except ValueError:
        return 0


def _duration_minutes(start: str, end: str) -> int:
    return _duration_seconds(start, end) // 60


def _active_seconds(times: list[str]) -> int:
    """Real translation time across a daily file: the sum of gaps between
    consecutive entries, excluding idle gaps over ``_ACTIVE_GAP_SECONDS`` that
    separate distinct runs. Within one run every gap is small, so this equals
    the sum of each run's own span — no whole-day-span inflation."""
    total = 0
    for earlier, later in zip(times, times[1:], strict=False):
        gap = _duration_seconds(earlier, later)
        if gap <= _ACTIVE_GAP_SECONDS:
            total += gap
    return total


def list_history_sessions() -> list[HistorySession]:
    """Summarize all daily history files, newest first. Files without any
    parseable entries are skipped."""
    if not os.path.isdir(HISTORY_DIR):
        return []

    sessions: list[HistorySession] = []
    for filename in os.listdir(HISTORY_DIR):
        match = _FILENAME_RE.match(filename)
        if not match:
            continue
        path = os.path.join(HISTORY_DIR, filename)
        try:
            entries = parse_history_file(path)
        except OSError as e:
            log(f"History read error for {filename}: {e}", level="WARNING")
            continue
        if not entries:
            continue

        pairs = pair_entries(entries)
        sessions.append(
            HistorySession(
                date=match.group(1),
                path=path,
                start_time=entries[0].time[:5],
                end_time=entries[-1].time[:5],
                duration_minutes=_duration_minutes(entries[0].time, entries[-1].time),
                active_seconds=_active_seconds([e.time for e in entries]),
                language_pair=_language_pairs(pairs),
                entry_count=len(pairs),
                has_summary=os.path.exists(summary_path(path)),
            )
        )

    sessions.sort(key=lambda s: s.date, reverse=True)
    return sessions


def list_log_files() -> list[LogFile]:
    """List all daily log files, newest first."""
    if not os.path.isdir(LOGS_DIR):
        return []

    logs: list[LogFile] = []
    for filename in os.listdir(LOGS_DIR):
        match = _LOG_FILENAME_RE.match(filename)
        if not match:
            continue
        path = os.path.join(LOGS_DIR, filename)
        try:
            size_kb = max(1, os.path.getsize(path) // 1024)
        except OSError as e:
            log(f"Log read error for {filename}: {e}", level="WARNING")
            continue
        logs.append(LogFile(date=match.group(1), path=path, size_kb=size_kb))

    logs.sort(key=lambda f: f.date, reverse=True)
    return logs


def _offset_hms(seconds: float) -> str:
    """Format an audio offset (seconds) as HH:MM:SS for a batch record line."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total // 60 % 60:02d}:{total % 60:02d}"


def _slugify(name: str) -> str:
    """Filesystem-safe slug from a source stem (the real name is kept in the
    record header; this just keeps filenames readable and unique-ish)."""
    stem = os.path.splitext(os.path.basename(name))[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("_")
    return (slug or "file")[:40]


def _normalize_formats(formats: str | list[str] | None) -> list[str]:
    """Coerce a formats argument ("srt"/"txt"/"both" or a list) to an ordered
    list of valid format ids. Unknown/empty inputs yield ``[]`` (legacy)."""
    if formats is None:
        return []
    if isinstance(formats, str):
        if formats == "both":
            return ["srt", "txt"]
        return [formats] if formats in _VALID_FORMATS else []
    return [f for f in formats if f in _VALID_FORMATS]


def write_batch_record(
    source_name: str,
    source_lang: str,
    target_lang: str,
    entries: list[tuple[float, str, str]],
    formats: str | list[str] | None = None,
) -> str | None:
    """Persist a completed batch run as a history-format record in BATCH_DIR.

    The line format matches the daily history files, so parse_history_file,
    pair_entries and the session summarizer all work on it unchanged. The
    timestamp column holds the segment's audio offset rather than wall clock.

    Args:
        source_name: Original file basename (kept verbatim in a header line).
        source_lang / target_lang: Full language names; the two-letter tag is
            derived exactly as the live history writer does.
        entries: (start_seconds, transcription, translation) per segment.
        formats: What the user exported ("srt"/"txt"/"both" or a list) — kept
            in a header line so the history viewer knows which formats to offer.

    Returns:
        The record path, or None if there was nothing to write or the write
        failed (batch persistence must never break the SRT export).
    """
    if not entries:
        return None
    try:
        src_tag = source_lang[:2].upper()
        tgt_tag = target_lang[:2].upper()
        now = datetime.now()
        filename = (
            f"{now.strftime('%Y-%m-%d')}_{now.strftime('%H%M%S')}"
            f"_{_slugify(source_name)}.txt"
        )
        path = os.path.join(BATCH_DIR, filename)
        fmt_list = _normalize_formats(formats)
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"{_BATCH_SOURCE_PREFIX}{source_name}\n")
            if fmt_list:
                f.write(f"{_BATCH_FORMATS_PREFIX}{','.join(fmt_list)}\n")
            f.write(f"{_BATCH_LANGS_PREFIX}{source_lang}|{target_lang}\n")
            f.write("\n")
            for start_s, transcription, translation in entries:
                ts = _offset_hms(start_s)
                f.write(f"[{ts}] {src_tag}: {transcription}\n")
                f.write(f"[{ts}] {tgt_tag}: {translation}\n\n")
        return path
    except Exception as e:
        log(f"Batch record write error: {e}", level="ERROR")
        return None


def _read_batch_source_name(path: str, fallback: str) -> str:
    """Return the original source name stored in a batch record's header."""
    try:
        with open(path, encoding="utf-8") as f:
            first = f.readline().rstrip("\n")
        if first.startswith(_BATCH_SOURCE_PREFIX):
            return first[len(_BATCH_SOURCE_PREFIX) :]
    except OSError:
        pass
    return fallback


def _read_batch_header(path: str) -> dict[str, str]:
    """Parse the '# key: value' header lines (before the first blank line)."""
    header: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if not line.strip():
                    break
                if line.startswith("# ") and ": " in line:
                    key, _, value = line[2:].partition(": ")
                    header[key.strip()] = value
    except OSError:
        pass
    return header


def batch_srt_path(record_path: str) -> str:
    """Sidecar SRT path for a batch record. The exact subtitles produced by the
    run are kept here so the history viewer can re-export them without needing
    the (possibly moved/deleted) source file."""
    return os.path.splitext(record_path)[0] + ".srt"


def read_batch_formats(record_path: str) -> list[str]:
    """Exported formats declared in the record header (empty for legacy)."""
    value = _read_batch_header(record_path).get("formats", "")
    return [f for f in value.split(",") if f in _VALID_FORMATS]


def read_batch_languages(record_path: str) -> tuple[str, str] | None:
    """Full (source, target) language names from the record header, or None for
    legacy records that only stored the two-letter tags."""
    value = _read_batch_header(record_path).get("langs", "")
    source, sep, target = value.partition("|")
    if sep and source and target:
        return source, target
    return None


def batch_available_formats(record_path: str) -> list[str]:
    """Formats the history viewer can show for a run. New runs declare this in
    the header; legacy records fall back to the SRT sidecar's presence (else
    txt-only — the transcript is always regenerable from the record)."""
    formats = read_batch_formats(record_path)
    if formats:
        return formats
    legacy = ["txt"]
    if os.path.exists(batch_srt_path(record_path)):
        legacy.insert(0, "srt")
    return legacy


def list_batch_runs() -> list[BatchRun]:
    """Summarize all batch records, newest first. Records without any
    parseable entries are skipped."""
    if not os.path.isdir(BATCH_DIR):
        return []

    runs: list[BatchRun] = []
    for filename in os.listdir(BATCH_DIR):
        match = _BATCH_FILENAME_RE.match(filename)
        if not match:
            continue
        path = os.path.join(BATCH_DIR, filename)
        try:
            entries = parse_history_file(path)
        except OSError as e:
            log(f"Batch read error for {filename}: {e}", level="WARNING")
            continue
        if not entries:
            continue

        pairs = pair_entries(entries)
        date = match.group(1)
        hhmmss = match.group(2)
        runs.append(
            BatchRun(
                date=date,
                time=f"{hhmmss[:2]}:{hhmmss[2:4]}",
                source_name=_read_batch_source_name(path, filename),
                path=path,
                duration_minutes=_duration_minutes(
                    entries[0].time, entries[-1].time
                ),
                active_seconds=_active_seconds([e.time for e in entries]),
                language_pair=_language_pairs(pairs),
                entry_count=len(pairs),
                has_summary=os.path.exists(summary_path(path)),
                formats=batch_available_formats(path),
            )
        )

    runs.sort(key=lambda r: (r.date, r.time), reverse=True)
    return runs

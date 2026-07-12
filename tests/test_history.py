"""Tests for the read-side history API (parser, pairing, session listing)."""

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from utils.history import (
    HistoryEntry,
    batch_available_formats,
    batch_srt_path,
    list_batch_runs,
    list_history_sessions,
    list_log_files,
    log_transcription_and_translation,
    pair_entries,
    parse_history_file,
    read_batch_formats,
    read_batch_languages,
    read_summary,
    summary_path,
    write_batch_record,
    write_summary,
)

SAMPLE = (
    "[00:52:53] AR: النص الأول\n"
    "[00:52:53] DE: Erster Text\n"
    "[Processing time]: 9.39s\n"
    "\n"
    "[00:53:05] AR: النص الثاني\n"
    "\n"
    "[00:53:05] DE: Zweiter Text\n"
    "[Processing time]: 3.16s\n"
    "\n"
)


def _write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


class TestParseHistoryFile:
    def test_parses_entries_and_skips_noise(self, tmp_path):
        path = tmp_path / "2026-07-03.txt"
        _write(path, SAMPLE)

        entries = parse_history_file(str(path))

        assert len(entries) == 4
        assert entries[0] == HistoryEntry("00:52:53", "AR", "النص الأول")
        assert entries[1] == HistoryEntry("00:52:53", "DE", "Erster Text")
        assert entries[2].time == "00:53:05"
        assert all("Processing time" not in e.text for e in entries)

    def test_continuation_lines_join_previous_entry(self, tmp_path):
        path = tmp_path / "2026-07-03.txt"
        _write(
            path,
            "[10:00:00] AR: line one\nline two\n\n[10:00:00] DE: Zeile eins\n\n",
        )

        entries = parse_history_file(str(path))

        assert len(entries) == 2
        assert entries[0].text == "line one\nline two"
        assert entries[1].text == "Zeile eins"

    def test_leading_garbage_without_entry_is_ignored(self, tmp_path):
        path = tmp_path / "2026-07-03.txt"
        _write(path, "orphan line\n[10:00:00] AR: text\n")

        entries = parse_history_file(str(path))

        assert len(entries) == 1
        assert entries[0].text == "text"


class TestPairEntries:
    def test_pairs_in_write_order(self):
        entries = [
            HistoryEntry("10:00:00", "AR", "a"),
            HistoryEntry("10:00:00", "DE", "b"),
            HistoryEntry("10:00:12", "AR", "c"),
            HistoryEntry("10:00:12", "DE", "d"),
        ]
        pairs = pair_entries(entries)
        assert pairs == [(entries[0], entries[1]), (entries[2], entries[3])]

    def test_odd_trailing_entry_pairs_with_none(self):
        entries = [
            HistoryEntry("10:00:00", "AR", "a"),
            HistoryEntry("10:00:00", "DE", "b"),
            HistoryEntry("10:00:12", "AR", "c"),
        ]
        pairs = pair_entries(entries)
        assert pairs[-1] == (entries[2], None)

    def test_empty(self):
        assert pair_entries([]) == []


class TestListHistorySessions:
    def test_summarizes_files_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.HISTORY_DIR", str(tmp_path))
        _write(tmp_path / "2026-07-01.txt", SAMPLE)
        _write(
            tmp_path / "2026-07-02.txt",
            "[09:00:00] AR: a\n[09:00:00] DE: b\n\n"
            "[10:30:30] AR: c\n[10:30:30] DE: d\n\n",
        )
        _write(tmp_path / "notes.txt", "[09:00:00] AR: ignored\n")
        _write(tmp_path / "2026-07-03.txt", "no entries here\n")

        sessions = list_history_sessions()

        assert [s.date for s in sessions] == ["2026-07-02", "2026-07-01"]
        newest = sessions[0]
        assert newest.start_time == "09:00"
        assert newest.end_time == "10:30"
        assert newest.duration_minutes == 90  # first→last span, kept as metadata
        # Two entries 90 min apart are two separate runs, not 90 min of
        # translation: the idle gap is excluded, so active time is ~0.
        assert newest.active_seconds == 0
        assert newest.language_pair == "AR → DE"
        assert newest.entry_count == 2
        assert newest.path == str(tmp_path / "2026-07-02.txt")

    def test_sub_minute_run_reports_seconds(self, tmp_path, monkeypatch):
        # A short clip must carry sub-minute precision so the UI can show
        # seconds instead of a useless "0 min".
        monkeypatch.setattr("utils.history.HISTORY_DIR", str(tmp_path))
        _write(
            tmp_path / "2026-07-05.txt",
            "[09:00:00] AR: a\n[09:00:00] DE: b\n\n"
            "[09:00:35] AR: c\n[09:00:35] DE: d\n\n",
        )
        session = list_history_sessions()[0]
        assert session.active_seconds == 35  # one continuous run

    def test_active_time_excludes_idle_between_runs(self, tmp_path, monkeypatch):
        # Two runs on the same day (a 24s one at 09:00, a 12s one at 14:00)
        # must report 36s of translation, not the ~5h whole-day span.
        monkeypatch.setattr("utils.history.HISTORY_DIR", str(tmp_path))
        _write(
            tmp_path / "2026-07-06.txt",
            "[09:00:00] AR: a\n[09:00:00] DE: a\n\n"
            "[09:00:12] AR: b\n[09:00:12] DE: b\n\n"
            "[09:00:24] AR: c\n[09:00:24] DE: c\n\n"
            "[14:00:00] AR: d\n[14:00:00] DE: d\n\n"
            "[14:00:12] AR: e\n[14:00:12] DE: e\n\n",
        )
        session = list_history_sessions()[0]
        assert session.active_seconds == 36
        assert session.duration_minutes == 300  # span is still ~5h

    def test_language_pair_change_is_listed_once_each(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.HISTORY_DIR", str(tmp_path))
        _write(
            tmp_path / "2026-07-01.txt",
            "[09:00:00] AR: a\n[09:00:00] DE: b\n\n"
            "[09:10:00] EN: c\n[09:10:00] DE: d\n\n"
            "[09:20:00] AR: e\n[09:20:00] DE: f\n\n",
        )

        sessions = list_history_sessions()

        assert sessions[0].language_pair == "AR → DE, EN → DE"

    def test_missing_directory_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "utils.history.HISTORY_DIR", str(tmp_path / "does-not-exist")
        )
        assert list_history_sessions() == []

    def test_has_summary_reflects_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.HISTORY_DIR", str(tmp_path))
        _write(tmp_path / "2026-07-01.txt", SAMPLE)
        _write(tmp_path / "2026-07-02.txt", SAMPLE)
        # A saved summary exists only for the 1st → its session is flagged.
        write_summary(str(tmp_path / "2026-07-01.txt"), "A summary.")

        by_date = {s.date: s for s in list_history_sessions()}

        assert by_date["2026-07-01"].has_summary is True
        assert by_date["2026-07-02"].has_summary is False
        # The .summary sidecar is not itself listed as a session.
        assert "2026-07-01.summary" not in [os.path.basename(s.path) for s in
                                            list_history_sessions()]

    def test_roundtrip_with_writer(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.HISTORY_DIR", str(tmp_path))
        monkeypatch.setattr(
            "utils.history.load_settings",
            lambda: SimpleNamespace(source_language="Arabic", target_language="German"),
        )
        log_transcription_and_translation("النص", "Der Text", duration=1.23)
        log_transcription_and_translation("ثاني", "Zweiter", duration=None)

        assert len(os.listdir(tmp_path)) == 1
        sessions = list_history_sessions()

        assert len(sessions) == 1
        assert sessions[0].entry_count == 2
        assert sessions[0].language_pair == "AR → GE"
        entries = parse_history_file(sessions[0].path)
        assert [e.text for e in entries] == ["النص", "Der Text", "ثاني", "Zweiter"]


class TestSummarySidecar:
    def test_summary_path_replaces_extension(self):
        p = summary_path(os.path.join("x", "history", "2026-07-03.txt"))
        assert os.path.basename(p) == "2026-07-03.summary"

    def test_write_then_read_roundtrip(self, tmp_path):
        hist = str(tmp_path / "2026-07-03.txt")
        assert read_summary(hist) is None  # absent → None
        write_summary(hist, "Two lines\nof summary.")
        assert read_summary(hist) == "Two lines\nof summary."

    def test_sidecar_matches_cleanup_date_pattern(self):
        # utils.cleanup purges dated single-extension files; the sidecar name
        # must match so summaries expire with their history file.
        from utils.cleanup import _DATE_PATTERN

        assert _DATE_PATTERN.match("2026-07-03.summary")
        # ...but the two-dot history filename must NOT be caught as a session.
        from utils.history import _FILENAME_RE

        assert _FILENAME_RE.match("2026-07-03.summary") is None


class TestListLogFiles:
    def test_lists_log_files_newest_first(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.LOGS_DIR", str(tmp_path))
        _write(tmp_path / "2026-07-01.log", "line\n")
        _write(tmp_path / "2026-07-03.log", "a\nb\n")
        _write(tmp_path / "notes.txt", "ignored\n")  # not a .log

        logs = list_log_files()

        assert [f.date for f in logs] == ["2026-07-03", "2026-07-01"]
        assert all(f.size_kb >= 1 for f in logs)

    def test_missing_directory_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.LOGS_DIR", str(tmp_path / "nope"))
        assert list_log_files() == []


class TestBatchRecords:
    def test_roundtrip_write_and_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path))
        path = write_batch_record(
            "khutbah.mp4",
            "Arabic",
            "German",
            [(0.0, "النص", "Der Text"), (12.0, "ثاني", "Zweiter")],
        )
        assert path is not None and os.path.dirname(path) == str(tmp_path)

        runs = list_batch_runs()
        assert len(runs) == 1
        run = runs[0]
        assert run.source_name == "khutbah.mp4"
        assert run.language_pair == "AR → GE"
        assert run.entry_count == 2
        # The record parses back with the shared history parser.
        entries = parse_history_file(run.path)
        assert [e.text for e in entries] == ["النص", "Der Text", "ثاني", "Zweiter"]

    def test_empty_entries_writes_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path))
        assert write_batch_record("x.mp4", "Arabic", "German", []) is None
        assert os.listdir(tmp_path) == []
        assert list_batch_runs() == []

    def test_has_summary_reflects_sidecar(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path))
        path = write_batch_record(
            "talk.wav", "Turkish", "English", [(0.0, "a", "b")]
        )
        assert list_batch_runs()[0].has_summary is False
        write_summary(path, "A summary.")
        assert list_batch_runs()[0].has_summary is True

    def test_missing_directory_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path / "nope"))
        assert list_batch_runs() == []


class TestBatchFormats:
    def test_header_records_formats_and_languages(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path))
        path = write_batch_record(
            "k.mp4", "Arabic", "German", [(0.0, "a", "b")], formats="both"
        )
        assert read_batch_formats(path) == ["srt", "txt"]
        assert read_batch_languages(path) == ("Arabic", "German")
        assert list_batch_runs()[0].formats == ["srt", "txt"]

    def test_txt_only_header_offers_only_txt(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path))
        path = write_batch_record(
            "k.mp4", "Turkish", "English", [(0.0, "a", "b")], formats="txt"
        )
        assert read_batch_formats(path) == ["txt"]
        assert batch_available_formats(path) == ["txt"]

    def test_batch_srt_path_swaps_extension(self):
        assert batch_srt_path(os.path.join("d", "2026_k.txt")).endswith("2026_k.srt")

    def test_legacy_record_without_header(self, tmp_path, monkeypatch):
        monkeypatch.setattr("utils.history.BATCH_DIR", str(tmp_path))
        p = tmp_path / "2026-07-01_120000_legacy.txt"
        _write(p, "# source: legacy.mp4\n\n[00:00:00] AR: a\n[00:00:00] DE: b\n\n")
        assert read_batch_formats(str(p)) == []
        assert read_batch_languages(str(p)) is None
        # No sidecar → txt-only (the transcript is always regenerable).
        assert batch_available_formats(str(p)) == ["txt"]
        # An SRT sidecar next to the record makes SRT available too.
        (tmp_path / "2026-07-01_120000_legacy.srt").write_text("1\n", encoding="utf-8")
        assert batch_available_formats(str(p)) == ["srt", "txt"]
        assert list_batch_runs()[0].formats == ["srt", "txt"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

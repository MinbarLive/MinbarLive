"""Qt session history viewer.

Port of the read side of ``gui/history_view.py``, including its four tabs:
sessions, batch runs, cost and logs. A "session" is one daily history file —
the writer logs per day and does not delimit sessions within a day, so the
list shows days.

All parsing comes from ``utils/history.py`` and ``utils/cost_display.py``,
neither of which imports a GUI toolkit; this module only renders and offers
the per-entry actions (summarise, copy, export, delete).
"""

from __future__ import annotations

import os
import shutil

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from utils.cost_display import cost_breakdown_lines, cost_rows, cost_window_total
from utils.cost_tracking import list_cost_sessions
from utils.history import (
    list_batch_runs,
    list_history_sessions,
    list_log_files,
    pair_entries,
    parse_history_file,
    read_summary,
    write_summary,
)
from utils.logging import log

# (tab id, translation key, English fallback), in the Tk tab order.
_TABS = (
    ("history", "history_tab_sessions", "History"),
    ("batch", "history_tab_batch", "Batch"),
    ("cost", "history_tab_cost", "Cost"),
    ("logs", "history_tab_logs", "Log"),
)


class HistoryWindow(QDialog):
    """Read-only viewer. Takes a translator rather than the panel: it needs
    nothing else from it, and that keeps it constructible in tests without a
    real control panel."""

    def __init__(self, translate, parent=None):
        super().__init__(parent)
        self._t = translate
        self._panel = parent
        self.setWindowTitle(self._t("history_title", "Session history"))
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.resize(1180, 720)

        self._tab = "history"
        self._rows: list = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(12)
        outer.addLayout(self._tab_bar())

        splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        self.entry_list = QListWidget()
        self.entry_list.setWordWrap(True)
        self.entry_list.currentRowChanged.connect(self._on_select)
        left_box.addWidget(self.entry_list)

        right = QWidget()
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(0, 0, 0, 0)
        right_box.setSpacing(8)
        self.header = QLabel("")
        self.header.setObjectName("heading")
        self.header.setWordWrap(True)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        right_box.addWidget(self.header)
        right_box.addWidget(self.detail, 1)
        right_box.addLayout(self._action_bar())

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 840])
        outer.addWidget(splitter, 1)

        self.empty_label = QLabel(self._t("history_empty", "No sessions recorded yet."))
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.empty_label)

        self._reload()

    # ── chrome ───────────────────────────────────────────────────────────
    def _tab_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        self._tab_group = QButtonGroup(self)
        self._tab_group.setExclusive(True)
        for index, (tab_id, key, fallback) in enumerate(_TABS):
            button = QPushButton(self._t(key, fallback))
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setChecked(tab_id == self._tab)
            self._tab_group.addButton(button, index)
            row.addWidget(button)
        self._tab_group.idClicked.connect(self._on_tab)
        row.addStretch(1)
        return row

    def _action_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self.summarise_btn = QPushButton(self._t("history_summarise", "Summarise"))
        self.summarise_btn.clicked.connect(self._on_summarise)
        self.copy_btn = QPushButton(self._t("history_copy", "Copy"))
        self.copy_btn.clicked.connect(self._on_copy)
        self.export_btn = QPushButton(self._t("history_export", "Export"))
        self.export_btn.clicked.connect(self._on_export)
        self.delete_btn = QPushButton(self._t("history_delete", "Delete"))
        self.delete_btn.clicked.connect(self._on_delete)
        row.addStretch(1)
        for button in (
            self.summarise_btn,
            self.copy_btn,
            self.export_btn,
            self.delete_btn,
        ):
            row.addWidget(button)
        return row

    # ── data ─────────────────────────────────────────────────────────────
    def _on_tab(self, index: int) -> None:
        tab = _TABS[index][0]
        if tab == self._tab:
            return
        self._tab = tab
        self._reload()

    def _reload(self) -> None:
        loaders = {
            "history": list_history_sessions,
            "batch": list_batch_runs,
            "cost": list_cost_sessions,
            "logs": list_log_files,
        }
        try:
            self._rows = list(loaders[self._tab]())
        except Exception as exc:  # noqa: BLE001 - a broken file must not block the UI
            log(f"History listing failed ({self._tab}): {exc}", level="ERROR")
            self._rows = []
        if self._tab == "cost":
            # cost_rows carries the display strings; keep the raw sessions too
            # so the detail pane can break a session down per provider.
            self._cost_sessions = self._rows
            self._rows = cost_rows(self._cost_sessions)

        self.entry_list.clear()
        for row in self._rows:
            item = QListWidgetItem(self._row_label(row))
            self.entry_list.addItem(item)

        has_any = bool(self._rows)
        self.empty_label.setVisible(not has_any)
        self.empty_label.setText(self._empty_text())
        self.entry_list.setVisible(has_any)
        self._sync_actions()
        if has_any:
            self.entry_list.setCurrentRow(0)
        else:
            self.header.setText("")
            self.detail.setPlainText("")

    def _empty_text(self) -> str:
        if self._tab == "batch":
            return self._t("history_batch_empty", "No files processed yet.")
        if self._tab == "cost":
            return self._t("cost_empty", "No cost recorded yet.")
        return self._t("history_empty", "No sessions recorded yet.")

    def _row_label(self, row) -> str:
        """Three lines per row: what it is, when, and how much of it — the same
        shape the Tk list shows."""
        if self._tab == "logs":
            return f"{row.date}\n{row.size_kb} KB"
        if self._tab == "cost":
            return f"{row.title}\n{row.subtitle}"
        minutes = max(1, round(row.active_seconds / 60))
        mark = " ★" if getattr(row, "has_summary", False) else ""
        if self._tab == "batch":
            return (
                f"{row.source_name}{mark}\n"
                f"{row.date} {row.time}  ·  {minutes} min\n"
                f"{row.language_pair}  ·  {row.entry_count}"
            )
        return (
            f"{row.date}{mark}\n"
            f"{row.start_time}–{row.end_time}  ·  {minutes} min\n"
            f"{row.language_pair}  ·  {row.entry_count}"
        )

    def _current(self):
        row = self.entry_list.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _on_select(self, _row: int) -> None:
        entry = self._current()
        self._sync_actions()
        if entry is None:
            self.header.setText("")
            self.detail.setPlainText("")
            return
        if self._tab == "logs":
            self.header.setText(entry.date)
            self.detail.setPlainText(self._read_text(entry.path))
        elif self._tab == "cost":
            self.header.setText(entry.title)
            self.detail.setPlainText(self._render_cost(entry))
        else:
            self.header.setText(self._detail_header(entry))
            self.detail.setPlainText(self._render_transcript(entry))

    def _sync_actions(self) -> None:
        entry = self._current()
        has_file = entry is not None and self._tab in ("history", "batch", "logs")
        # Summarising sends the transcript to the translation provider, which
        # only makes sense for a transcript.
        self.summarise_btn.setEnabled(
            entry is not None and self._tab in ("history", "batch")
        )
        self.copy_btn.setEnabled(entry is not None)
        self.export_btn.setEnabled(has_file)
        self.delete_btn.setEnabled(has_file)

    def _detail_header(self, entry) -> str:
        if self._tab == "batch":
            return (
                f"{entry.source_name}  ·  {entry.date} {entry.time}  ·  "
                f"{entry.language_pair}"
            )
        return (
            f"{entry.date}  ·  {entry.start_time}–{entry.end_time}  ·  "
            f"{entry.language_pair}"
        )

    @staticmethod
    def _read_text(path: str) -> str:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError as exc:
            log(f"Read failed for {path}: {exc}", level="ERROR")
            return ""

    def _render_cost(self, row) -> str:
        session = next(
            (
                s
                for s in getattr(self, "_cost_sessions", [])
                if str(s.get("id", "")) == row.session_id
            ),
            None,
        )
        window = cost_window_total(getattr(self, "_cost_sessions", []))
        head = self._t("cost_last_30_days", "Last 30 days: {total} ({sessions})").format(
            total=window.total, sessions=window.sessions
        )
        if session is None:
            return head
        return f"{head}\n\n" + cost_breakdown_lines(
            session,
            estimate_note=self._t(
                "cost_estimate_note", "Estimate — public list prices"
            ),
            unpriced_note=self._t("cost_unpriced", "unpriced"),
            requests_label=self._t("cost_requests", "requests"),
        )

    def _render_transcript(self, entry) -> str:
        try:
            entries = parse_history_file(entry.path)
        except Exception as exc:  # noqa: BLE001
            log(f"History parse failed for {entry.path}: {exc}", level="ERROR")
            return self._t("history_read_error", "Could not read this session.")

        lines: list[str] = []
        try:
            summary = read_summary(entry.path)
        except Exception:  # noqa: BLE001
            summary = None
        if summary:
            lines += [
                self._t("history_summary_heading", "Summary"),
                "",
                summary,
                "",
                "—" * 40,
                "",
            ]

        for transcription, translation in pair_entries(entries):
            lines.append(
                f"[{transcription.time}] {transcription.lang}: {transcription.text}"
            )
            # Same-language runs record an identical pair; render it once, as
            # the Tk viewer does.
            if translation is not None and translation.text != transcription.text:
                lines.append(
                    f"[{translation.time}] {translation.lang}: {translation.text}"
                )
            lines.append("")
        return "\n".join(lines).rstrip()

    # ── actions ──────────────────────────────────────────────────────────
    def _on_copy(self) -> None:
        QGuiApplication.clipboard().setText(self.detail.toPlainText())
        self.header.setToolTip(self._t("history_copied", "Copied"))

    def _on_export(self) -> None:
        entry = self._current()
        if entry is None:
            return
        suggestion = os.path.basename(getattr(entry, "path", "export.txt"))
        target, _ = QFileDialog.getSaveFileName(
            self, self._t("history_export", "Export"), suggestion
        )
        if not target:
            return
        try:
            shutil.copyfile(entry.path, target)
        except OSError as exc:
            QMessageBox.warning(self, "MinbarLive", str(exc))

    def _on_delete(self) -> None:
        entry = self._current()
        if entry is None:
            return
        answer = QMessageBox.question(
            self,
            self._t("history_delete", "Delete"),
            self._t("history_delete_confirm", "Delete this record permanently?"),
        )
        if answer != QMessageBox.Yes:
            return
        try:
            os.remove(entry.path)
        except OSError as exc:
            QMessageBox.warning(self, "MinbarLive", str(exc))
            return
        self._reload()

    def _on_summarise(self) -> None:
        """Summarise the selected transcript with the active provider.

        Runs inline: it is one request the operator explicitly asked for, and
        the button is disabled meanwhile so it cannot be queued twice.
        """
        entry = self._current()
        if entry is None:
            return
        settings = getattr(self._panel, "settings", None)
        if settings is None:
            return
        self.summarise_btn.setEnabled(False)
        self.summarise_btn.setText(self._t("summary_generating", "Summarising…"))
        QGuiApplication.processEvents()
        try:
            from utils.session_summary import summarize_session_file

            summary = summarize_session_file(
                entry.path,
                target_language=settings.target_language,
                provider_id=settings.ai_provider,
                session_label=self._detail_header(entry),
            )
            write_summary(entry.path, summary)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            log(f"Summary failed: {exc}", level="ERROR")
            QMessageBox.warning(
                self,
                self._t("summary_title", "Summary"),
                self._t("summary_failed", "Could not create a summary: {error}").format(
                    error=exc
                ),
            )
        else:
            self._reload()
        finally:
            self.summarise_btn.setText(self._t("history_summarise", "Summarise"))
            self._sync_actions()

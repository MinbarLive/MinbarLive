"""Qt session history viewer.

Port of the read side of ``gui/history_view.py``. A "session" is one daily
history file — the writer logs per day and does not delimit sessions within a
day, so the list shows days.

All parsing comes from ``utils/history.py``, which imports no GUI toolkit and
is reused unchanged. This module only renders.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from utils.history import (
    list_history_sessions,
    pair_entries,
    parse_history_file,
    read_summary,
)
from utils.logging import log


class HistoryWindow(QDialog):
    """Read-only viewer. Takes a translator rather than the panel: it needs
    nothing else from it, and that keeps it constructible in tests without a
    real control panel."""

    def __init__(self, translate, parent=None):
        super().__init__(parent)
        self._t = translate
        self.setWindowTitle(self._t("history_title", "Session history"))
        self.resize(1100, 700)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        splitter = QSplitter(Qt.Horizontal)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(6)
        self.session_list = QListWidget()
        self.session_list.currentRowChanged.connect(self._on_select)
        left_layout.addWidget(self.session_list)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)
        self.header = QLabel("")
        self.header.setObjectName("heading")
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        right_layout.addWidget(self.header)
        right_layout.addWidget(self.transcript)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 800])
        outer.addWidget(splitter)

        self.empty_label = QLabel(self._t("history_empty", "No sessions recorded yet."))
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        outer.addWidget(self.empty_label)

        self._sessions = []
        self._load()

    # ── data ─────────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            self._sessions = list_history_sessions()
        except Exception as exc:  # noqa: BLE001 - a broken file must not block the UI
            log(f"History listing failed: {exc}", level="ERROR")
            self._sessions = []

        self.session_list.clear()
        for session in self._sessions:
            minutes = max(1, round(session.active_seconds / 60))
            label = (
                f"{session.date}\n"
                f"{session.start_time}–{session.end_time}  ·  {minutes} min\n"
                f"{session.language_pair}  ·  {session.entry_count}"
            )
            item = QListWidgetItem(label)
            if session.has_summary:
                item.setToolTip(self._t("history_has_summary", "Has a saved summary"))
            self.session_list.addItem(item)

        has_any = bool(self._sessions)
        self.empty_label.setVisible(not has_any)
        self.session_list.setVisible(has_any)
        if has_any:
            self.session_list.setCurrentRow(0)

    def _on_select(self, row: int) -> None:
        if not (0 <= row < len(self._sessions)):
            self.header.setText("")
            self.transcript.setPlainText("")
            return
        session = self._sessions[row]
        self.header.setText(
            f"{session.date}  ·  {session.start_time}–{session.end_time}  ·  "
            f"{session.language_pair}"
        )
        self.transcript.setPlainText(self._render(session))

    def _render(self, session) -> str:
        try:
            entries = parse_history_file(session.path)
        except Exception as exc:  # noqa: BLE001
            log(f"History parse failed for {session.path}: {exc}", level="ERROR")
            return self._t("history_read_error", "Could not read this session.")

        lines: list[str] = []
        try:
            summary = read_summary(session.path)
        except Exception:  # noqa: BLE001
            summary = None
        if summary:
            heading = self._t("history_summary_heading", "Summary")
            lines += [heading, "", summary, "", "—" * 40, ""]

        for transcription, translation in pair_entries(entries):
            lines.append(f"[{transcription.time}] {transcription.lang}: {transcription.text}")
            # Same-language runs record an identical pair; render it once, as
            # the Tk viewer does.
            if translation is not None and translation.text != transcription.text:
                lines.append(f"[{translation.time}] {translation.lang}: {translation.text}")
            lines.append("")
        return "\n".join(lines).rstrip()

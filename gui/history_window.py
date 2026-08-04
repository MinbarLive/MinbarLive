"""Qt session history viewer + summary dialog.

Port of ``gui/history_view.py``, including its four tabs (sessions, batch runs,
cost and logs), its list-row shape, the batch SRT|TXT preview toggle, the spend
chart on the cost tab and the separate "Summarise session" dialog. A "session"
is one daily history file — the writer logs per day and does not delimit
sessions within a day, so the list shows days.

All parsing comes from ``utils/history.py``, ``utils/cost_display.py`` and
``utils/cost_tracking.py``, none of which imports a GUI toolkit; this module
only renders and offers the per-entry actions.
"""

from __future__ import annotations

import os
import shutil
import threading

from PySide6.QtCore import QObject, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QPainter,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from gui.dialogs import ask_yes_no, show_message
from gui.widgets import Dropdown
from utils.cost_display import (
    cost_bars,
    cost_breakdown_lines,
    cost_rows,
    cost_window_by_provider,
    cost_window_total,
    provider_label,
)
from utils.cost_tracking import delete_cost_session, list_cost_sessions
from utils.history import (
    batch_srt_path,
    list_batch_runs,
    list_history_sessions,
    list_log_files,
    pair_entries,
    parse_history_file,
    read_batch_languages,
    read_summary,
    summary_path,
    write_summary,
)
from utils.logging import log

# Opening size, in the units the Tk viewer uses: big enough for a list beside a
# readable transcript column, small enough not to cover the panel behind it.
HISTORY_WINDOW_W = 900
HISTORY_WINDOW_H = 560

# The summary dialog is mostly generated text, but stays column-shaped so the
# summary keeps a readable line length.
SUMMARY_WINDOW_W = 560
SUMMARY_WINDOW_H = 480

# Session list: the width it opens at, and how far the splitter lets it be
# dragged down, so the transcript beside it stays a readable column.
LIST_W = 280
LIST_W_MIN = 170

# Space between the list and the transcript beside it. It lives in the right
# pane's own margin rather than in the splitter handle: the handle measured 0px
# wide here (the stylesheet's ``QSplitter::handle { width }`` never reached it,
# and the two panes ended up overlapping by a pixel), so the list border and the
# transcript border sat directly against each other.
PANE_GAP = 14

# Widths the action buttons hold in the wide layout. Dropped in the narrow one,
# where the three secondary actions share the row equally instead.
#
# There is deliberately no breakpoint constant beside them: the width the wide
# layout stops fitting at is these numbers plus the list, the margins and four
# translated button labels, so it is measured from the built layout (see
# _measure_width_modes) rather than written down and left to drift. The Tk
# viewer hard-codes 560 (gui/history_view.py HISTORY_NARROW_W) and reflows
# within it; here the switch happens exactly when side-by-side no longer fits.
_ACTION_PRIMARY_W = 160
_ACTION_SECONDARY_W = 120

# Marks a record that already has a saved AI summary.
SUMMARY_MARK = "📝 "

# (tab id, translation key, English fallback), in the Tk tab order.
_TABS = (
    ("history", "history_tab_sessions", "History"),
    ("batch", "history_tab_batch", "Batch"),
    ("cost", "history_tab_cost", "Cost"),
    ("logs", "history_tab_logs", "Log"),
)

_TAB_IDS = tuple(tab for tab, _key, _fallback in _TABS)

# Tabs whose records are transcripts, i.e. the ones Summarise applies to.
_TRANSCRIPT_TABS = ("history", "batch")


class RowDelegate(QStyledItemDelegate):
    """Paints a list row: a bold title with an optional right-aligned tag, over
    a muted detail line — the shape the Tk list rows have.

    A delegate rather than one item widget per row, because both lines have to
    shorten themselves. The rows carry long text ("2026-07-31 · GE → EN, AR →
    EN, AR → GE · 52"); asking a widget for its full width made the item boxes
    wider than the viewport, so the text ran out of its own rounded box. Here
    the elision happens at paint time against the width the row actually got,
    so it is right at every window size without any resize bookkeeping.
    """

    SUB_ROLE = Qt.UserRole + 1  # the muted detail line
    TAG_ROLE = Qt.UserRole + 2  # short right-aligned badge ("SRT+TXT", "~")
    # How to shorten the title. Dates and language pairs read left-to-right, so
    # they lose their tail; a filename is set to ElideMiddle so it keeps both
    # its start and its extension.
    ELIDE_ROLE = Qt.UserRole + 3

    _GAP = 3  # between the two lines
    _TAG_PAD = 10  # between the title and the tag beside it

    def _fonts(self, base: QFont) -> tuple[QFont, QFont, QFont]:
        title = QFont(base)
        title.setBold(True)
        sub = QFont(base)
        sub.setPointSizeF(max(6.0, base.pointSizeF() - 0.75))
        tag = QFont(sub)
        tag.setBold(True)
        return title, sub, tag

    def sizeHint(self, option, index) -> QSize:  # noqa: N802 - Qt API
        # The base hint is one line plus whatever padding the stylesheet gives
        # an item, so adding the second line's height keeps that chrome right
        # without this delegate having to know the numbers.
        base = super().sizeHint(option, index)
        _title, sub, _tag = self._fonts(option.font)
        # Width 0: the row elides itself, and a real width here would make the
        # list demand it and push the transcript out of the window.
        return QSize(0, base.height() + QFontMetrics(sub).height() + self._GAP)

    def paint(self, painter, option, index) -> None:
        from gui.theme import current_colors

        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        title = opt.text
        opt.text = ""  # the box only; both lines are drawn below
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)

        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget)
        colors = current_colors()
        title_font, sub_font, tag_font = self._fonts(opt.font)
        title_metrics = QFontMetrics(title_font)
        sub_metrics = QFontMetrics(sub_font)

        painter.save()
        tag = index.data(self.TAG_ROLE) or ""
        tag_width = 0
        if tag:
            tag_width = QFontMetrics(tag_font).horizontalAdvance(tag) + self._TAG_PAD
            painter.setFont(tag_font)
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(
                QRect(
                    rect.right() - tag_width + self._TAG_PAD,
                    rect.top(),
                    tag_width - self._TAG_PAD,
                    title_metrics.height(),
                ),
                Qt.AlignRight | Qt.AlignVCenter,
                tag,
            )

        title_rect = QRect(
            rect.left(),
            rect.top(),
            max(0, rect.width() - tag_width),
            title_metrics.height(),
        )
        painter.setFont(title_font)
        painter.setPen(QColor(colors["text"]))
        painter.drawText(
            title_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            title_metrics.elidedText(
                title,
                index.data(self.ELIDE_ROLE) or Qt.ElideRight,
                title_rect.width(),
            ),
        )

        sub_rect = QRect(
            rect.left(),
            title_rect.bottom() + self._GAP,
            rect.width(),
            sub_metrics.height(),
        )
        painter.setFont(sub_font)
        painter.setPen(QColor(colors["muted"]))
        painter.drawText(
            sub_rect,
            Qt.AlignLeft | Qt.AlignVCenter,
            sub_metrics.elidedText(
                index.data(self.SUB_ROLE) or "", Qt.ElideRight, sub_rect.width()
            ),
        )
        painter.restore()


class CostChart(QWidget):
    """Spend across recent sessions, newest on the right.

    The Tk viewer draws this on a raw ``tk.Canvas``; here it is a painted
    widget. Clicking a bar selects that session — the highlighted bar and the
    selected list row are the same thing, so the chart may as well drive it.
    """

    selected = Signal(str)  # session id of the clicked bar

    HEIGHT = 248
    _PAD_X = 16
    _PAD_TOP = 82  # room for the 30-day header + per-provider sub-line
    _PAD_BOTTOM = 46  # room for the two-line date labels
    _BAR_MAX_W = 46

    def __init__(self, translate, parent=None):
        super().__init__(parent)
        self._t = translate
        self._sessions: list = []
        self._bars: list = []
        self._selected_id = ""
        self.setFixedHeight(self.HEIGHT)
        self.setCursor(Qt.PointingHandCursor)

    def set_data(self, sessions: list, selected_id: str = "") -> None:
        self._sessions = list(sessions)
        self._bars = cost_bars(self._sessions)
        self._selected_id = selected_id
        self.update()

    def set_selected(self, session_id: str) -> None:
        if session_id == self._selected_id:
            return
        self._selected_id = session_id
        self.update()

    def _slot_width(self) -> float:
        return (self.width() - 2 * self._PAD_X) / max(1, len(self._bars))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        """Whole slot column, not just the bar: a sub-cent bar is two pixels
        tall and would be unhittable."""
        if not self._bars:
            return
        index = int((event.position().x() - self._PAD_X) // max(1.0, self._slot_width()))
        if 0 <= index < len(self._bars):
            self.selected.emit(self._bars[index].session_id)

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        from gui.theme import current_colors

        if not self._bars:
            return
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        width = self.width()

        window = cost_window_total(self._sessions, days=30)
        header = self._t(
            "cost_last_30_days", "Last 30 days: {total} · {count} sessions"
        ).format(total=window.total, count=window.sessions)
        font = self.font()
        font.setPointSizeF(11.0)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(colors["text"]))
        painter.drawText(
            QRectF(self._PAD_X, 6, width - 2 * self._PAD_X, 24),
            Qt.AlignLeft | Qt.AlignVCenter,
            header,
        )

        by_provider = cost_window_by_provider(self._sessions, days=30)
        if by_provider:
            font.setPointSizeF(9.0)
            font.setBold(False)
            painter.setFont(font)
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(
                QRectF(self._PAD_X, 32, width - 2 * self._PAD_X, 20),
                Qt.AlignLeft | Qt.AlignVCenter,
                "   ·   ".join(
                    f"{provider_label(p.provider)} {p.total}" for p in by_provider
                ),
            )

        plot_h = self.height() - self._PAD_TOP - self._PAD_BOTTOM
        peak = max((b.value for b in self._bars), default=0.0)
        slot = self._slot_width()
        bar_w = min(self._BAR_MAX_W, slot * 0.6)
        label_font = self.font()
        label_font.setPointSizeF(7.5)
        label_font.setBold(False)
        painter.setFont(label_font)

        for index, bar in enumerate(self._bars):
            cx = self._PAD_X + slot * (index + 0.5)
            frac = (bar.value / peak) if peak > 0 else 0.0
            bar_h = max(2.0, frac * plot_h)
            y1 = self._PAD_TOP + plot_h
            rect = QRectF(cx - bar_w / 2, y1 - bar_h, bar_w, bar_h)
            active = bar.session_id == self._selected_id
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(colors["accent"] if active else colors["button"]))
            radius = min(4.0, bar_h / 2)
            painter.drawRoundedRect(rect, radius, radius)

            painter.setPen(QColor(colors["text"] if active else colors["muted"]))
            painter.drawText(
                QRectF(cx - slot / 2, y1 - bar_h - 18, slot, 16),
                Qt.AlignHCenter | Qt.AlignBottom,
                self._bar_amount(bar.value),
            )
            painter.setPen(QColor(colors["muted"]))
            painter.drawText(
                QRectF(cx - slot / 2, y1 + 4, slot, self._PAD_BOTTOM - 6),
                Qt.AlignHCenter | Qt.AlignTop,
                bar.label,
            )
        painter.end()

    @staticmethod
    def _bar_amount(value: float) -> str:
        if value <= 0:
            return "$0"
        if value < 0.01:
            return "<¢1"
        return f"${value:.2f}"


class _SummaryWorker(QObject):
    """Runs the summary request on a plain thread, reporting back as signals."""

    done = Signal(str)
    failed = Signal(str)

    def start(self, path: str, label: str, language: str, provider_id: str) -> None:
        def _run() -> None:
            from utils.session_summary import summarize_session_file

            try:
                self.done.emit(
                    summarize_session_file(
                        path,
                        target_language=language,
                        provider_id=provider_id,
                        session_label=label,
                    )
                )
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator
                self.failed.emit(str(exc))

        threading.Thread(target=_run, daemon=True, name="session-summary").start()


class SummaryDialog(QDialog):
    """Summarise one record with a chosen provider and output language.

    Its own window rather than an inline action: the request takes seconds,
    picks a provider and a language, and the result is a document the operator
    reads, copies or saves.
    """

    saved = Signal()  # a summary was written next to the record

    def __init__(self, item, translate, settings, texts, parent=None):
        super().__init__(parent)
        self._t = translate
        self._item = item
        self._settings = settings
        self._texts = texts or {}
        self._running = False

        self.setWindowTitle(self._t("summary_title", "Summarise session"))
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.resize(SUMMARY_WINDOW_W, SUMMARY_WINDOW_H)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(10)

        header = QLabel(f"{item.date} · {item.language_pair}")
        header.setObjectName("muted")
        outer.addWidget(header)

        outer.addLayout(self._choices_row())

        self.generate_btn = QPushButton(self._t("summary_generate", "Create summary"))
        self.generate_btn.setObjectName("accent")
        self.generate_btn.setMinimumHeight(44)
        self.generate_btn.clicked.connect(self._on_generate)
        outer.addWidget(self.generate_btn)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        outer.addWidget(self.status)

        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        outer.addWidget(self.text, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.save_btn = QPushButton(self._t("summary_save", "Save…"))
        self.save_btn.setMinimumWidth(120)
        self.save_btn.clicked.connect(self._on_save)
        self.copy_btn = QPushButton(self._t("summary_copy", "Copy"))
        self.copy_btn.setMinimumWidth(120)
        self.copy_btn.clicked.connect(self._on_copy)
        buttons.addWidget(self.save_btn)
        buttons.addWidget(self.copy_btn)
        outer.addLayout(buttons)

        self.worker = _SummaryWorker(self)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)

        # A summary generated earlier lives next to the record — load it so the
        # user does not have to pay for it twice. The button still regenerates.
        existing = read_summary(item.path)
        if existing and existing.strip():
            self.text.setPlainText(existing)
            self.status.setText(self._t("summary_loaded", "Saved summary loaded"))
        self._sync_buttons()

    # ── build ────────────────────────────────────────────────────────────
    def _choices_row(self) -> QHBoxLayout:
        from providers import PROVIDER_CHOICES, has_usable_key
        from utils.settings import (
            TARGET_LANGUAGE_NAMES,
            language_canonical_name,
            language_display_name,
        )

        row = QHBoxLayout()
        row.setSpacing(12)

        provider_col = QVBoxLayout()
        provider_col.setSpacing(4)
        caption = QLabel(self._t("summary_provider", "AI provider"))
        caption.setObjectName("field")
        provider_col.addWidget(caption)
        # Providers with a usable key first; if none has one, offer them all so
        # picking one leads to the key dialog rather than to an empty dropdown.
        choices = [
            (name, pid) for name, pid in PROVIDER_CHOICES if has_usable_key(pid)
        ] or list(PROVIDER_CHOICES)
        self.provider_combo = Dropdown()
        for name, pid in choices:
            self.provider_combo.addItem(name, pid)
        wanted = getattr(self._settings, "last_summary_provider", "") or getattr(
            self._settings, "ai_provider", ""
        )
        index = self.provider_combo.findData(wanted)
        self.provider_combo.setCurrentIndex(max(0, index))
        provider_col.addWidget(self.provider_combo)
        row.addLayout(provider_col, 1)

        language_col = QVBoxLayout()
        language_col.setSpacing(4)
        caption = QLabel(self._t("summary_language", "Summary language"))
        caption.setObjectName("field")
        language_col.addWidget(caption)
        # Native endonyms in the dropdown, canonical English names everywhere
        # else (storage + the prompt) — the split every language dropdown uses.
        self.language_combo = Dropdown()
        for name in TARGET_LANGUAGE_NAMES:
            self.language_combo.addItem(language_display_name(name), name)
        wanted = language_canonical_name(
            getattr(self._settings, "last_summary_language", "")
            or getattr(self._settings, "target_language", "")
        )
        index = self.language_combo.findData(wanted)
        self.language_combo.setCurrentIndex(max(0, index))
        language_col.addWidget(self.language_combo)
        row.addLayout(language_col, 1)
        return row

    # ── actions ──────────────────────────────────────────────────────────
    def _sync_buttons(self) -> None:
        has_text = bool(self.text.toPlainText().strip())
        self.save_btn.setEnabled(has_text and not self._running)
        self.copy_btn.setEnabled(has_text and not self._running)
        self.generate_btn.setEnabled(not self._running)
        self.provider_combo.setEnabled(not self._running)
        self.language_combo.setEnabled(not self._running)

    def _on_generate(self) -> None:
        if self._running:
            return
        from gui.api_keys import ensure_keys
        from providers import has_usable_key

        provider_id = self.provider_combo.currentData()
        language = self.language_combo.currentData()
        if not provider_id or not language:
            return
        if not has_usable_key(provider_id):
            ensure_keys([provider_id], self._texts, self)
            if not has_usable_key(provider_id):
                return

        if self._settings is not None:
            from utils.settings import save_settings

            self._settings.last_summary_provider = provider_id
            self._settings.last_summary_language = language
            save_settings(self._settings)

        self._running = True
        self._sync_buttons()
        self.status.setText(self._t("summary_generating", "Summarising…"))
        self.text.setPlainText("")
        label = f"{self._item.date} · {self._item.language_pair}"
        log(f"SUMMARY started ({provider_id}, {language}): {self._item.path}")
        self.worker.start(self._item.path, label, language, provider_id)

    def _on_done(self, text: str) -> None:
        self._running = False
        self.text.setPlainText(text)
        self.status.setText("")
        self._sync_buttons()
        try:
            write_summary(self._item.path, text)
            self._item.has_summary = True
            self.saved.emit()
        except OSError as exc:
            log(f"SUMMARY persist failed: {exc}", level="WARNING")
        log("SUMMARY done")

    def _on_failed(self, message: str) -> None:
        self._running = False
        self._sync_buttons()
        self.status.setText(
            self._t("summary_failed", "Summary failed: {error}").format(error=message)
        )
        log(f"SUMMARY failed: {message}", level="ERROR")

    def _on_copy(self) -> None:
        text = self.text.toPlainText().strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        _flash(self.copy_btn, self._t("summary_copied", "Copied"),
               self._t("summary_copy", "Copy"))

    def _on_save(self) -> None:
        text = self.text.toPlainText().strip()
        if not text:
            return
        target, _filter = QFileDialog.getSaveFileName(
            self,
            self._t("summary_save", "Save…"),
            f"MinbarLive_{self._item.date}_summary.txt",
            f"Text files (*.txt);;{self._t('batch_all_files', 'All files')} (*.*)",
        )
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as exc:
            log(f"Summary export failed: {exc}", level="ERROR")
            _warn(self, self._t("summary_save", "Save…"), str(exc), self._t)
            return
        log(f"Summary exported to {target}")
        _flash(self.save_btn, self._t("summary_saved", "Saved"),
               self._t("summary_save", "Save…"))


def _flash(button: QPushButton, message: str, restore: str, msec: int = 1500) -> None:
    """Confirm an action on the button itself, then put its label back.

    The button is passed as the timer's context object, so closing the window
    within the delay cancels the callback instead of reaching a deleted widget.
    """
    button.setText(message)
    QTimer.singleShot(msec, button, lambda: button.setText(restore))


def _warn(parent: QWidget, title: str, body: str, translate=None) -> None:
    """Report a failed save/export/delete in the app's own dialog."""
    show_message(parent, title, body, kind="error", translate=translate)


class HistoryWindow(QDialog):
    """Read-only viewer over the recorded sessions, batch runs, cost and logs."""

    def __init__(self, translate, parent=None, settings=None, initial_tab="history"):
        super().__init__(parent)
        # Both before the first resize() below: it can deliver a resizeEvent,
        # and that reads them. ``_narrow = None`` so the first real pass always
        # runs; ``_wide_min_w`` is replaced with the measured value once the
        # layout exists, and until then no width counts as narrow.
        self._narrow: bool | None = None
        self._wide_min_w = 0
        self._t = translate
        self._panel = parent
        self._settings = (
            settings if settings is not None else getattr(parent, "settings", None)
        )
        self.setWindowTitle(self._t("history_title", "Session history"))
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())
        self.resize(HISTORY_WINDOW_W, HISTORY_WINDOW_H)

        self._tab = initial_tab if initial_tab in _TAB_IDS else "history"
        self._rows: list = []
        self._cost_sessions: list = []
        # Which format the batch preview shows ("srt"/"txt").
        self._batch_format: str | None = None
        self._summary_dialog: SummaryDialog | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(12)
        outer.addLayout(self._tab_bar())

        self.splitter = QSplitter(Qt.Horizontal)
        left = QWidget()
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(0, 0, 0, 0)
        self.entry_list = QListWidget()
        self.entry_list.setItemDelegate(RowDelegate(self.entry_list))
        # The delegate elides both lines to whatever width a row gets, so a
        # horizontal scroll bar could only ever be an artefact.
        self.entry_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.entry_list.currentRowChanged.connect(self._on_select)
        left_box.addWidget(self.entry_list)
        left.setMinimumWidth(LIST_W_MIN)

        right = QWidget()
        self._right_box = right_box = QVBoxLayout(right)
        right_box.setContentsMargins(PANE_GAP, 0, 0, 0)
        right_box.setSpacing(8)
        self.header = QLabel("")
        self.header.setObjectName("heading")
        self.header.setWordWrap(True)
        right_box.addWidget(self.header)
        self.format_bar = self._format_bar()
        right_box.addWidget(self.format_bar)
        self.cost_chart = CostChart(self._t)
        self.cost_chart.selected.connect(self._on_cost_bar)
        right_box.addWidget(self.cost_chart)
        self.cost_chart.setVisible(False)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        right_box.addWidget(self.detail, 1)
        self.empty_label = QLabel("")
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignCenter)
        # Stretched so it sits in the middle of the pane the hidden transcript
        # would have filled. A hidden widget's stretch is ignored, so this does
        # nothing while there is something to show.
        right_box.addWidget(self.empty_label, 1)
        right_box.addWidget(self._action_bar())

        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        outer.addWidget(self.splitter, 1)

        # Something has to hold the buttons before the window is measurable;
        # the real choice is made in _measure_width_modes, on first show.
        self._apply_width_mode(False)

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

    def _format_bar(self) -> QWidget:
        """SRT | TXT toggle — shown only for a batch run that holds both."""
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        self.format_buttons: dict[str, QPushButton] = {}
        for fmt in ("srt", "txt"):
            button = QPushButton(fmt.upper())
            button.setObjectName("tab")
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setMinimumWidth(70)
            button.clicked.connect(lambda _checked, f=fmt: self._on_format(f))
            row.addWidget(button)
            self.format_buttons[fmt] = button
        row.addStretch(1)
        bar.setVisible(False)
        return bar

    def _action_bar(self) -> QWidget:
        """Summarise, then Delete / Copy / Save…

        A container holding two rows rather than one bare row: below
        ``NARROW_W`` the three secondary actions drop to the second one, and
        moving buttons between rows needs somewhere to move them. Which row
        each sits in is decided by :meth:`_apply_width_mode`, never here.
        """
        bar = QWidget()
        box = QVBoxLayout(bar)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        self._action_top = QHBoxLayout()
        self._action_top.setSpacing(8)
        self._action_bottom = QHBoxLayout()
        self._action_bottom.setSpacing(8)
        box.addLayout(self._action_top)
        box.addLayout(self._action_bottom)

        self.summarise_btn = QPushButton(self._t("history_summarise", "Summarise"))
        self.summarise_btn.setObjectName("accent")
        self.summarise_btn.clicked.connect(self._on_summarise)
        self.delete_btn = QPushButton(self._t("history_delete", "Delete"))
        self.delete_btn.setObjectName("danger")
        self.delete_btn.clicked.connect(self._on_delete)
        self.copy_btn = QPushButton(self._t("history_copy", "Copy"))
        self.copy_btn.clicked.connect(self._on_copy)
        self.export_btn = QPushButton(self._t("history_export", "Save…"))
        self.export_btn.clicked.connect(self._on_export)
        return bar

    # ── responsive layout ────────────────────────────────────────────────
    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if not self._wide_min_w:
            self._measure_width_modes()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._apply_width_mode(self.width() < self._wide_min_w)

    def _measure_width_modes(self) -> None:
        """Take the breakpoint and the window's floor from the built layout.

        Two numbers, neither written down:

        ``_wide_min_w`` is the breakpoint. The width at which side-by-side
        stops fitting is exactly the width to stop using it at, and it is the
        sum of the list, the margins and four *translated* button labels — so
        it belongs to the window, not to a constant.

        The floor then has to be the NARROW layout's minimum, or the two
        constrain each other into never switching: left at the wide
        arrangement's ~765 the window could not be dragged narrow enough to
        reach the mode that lowers it, and narrow mode would only ever apply to
        in-app panels, which are resized as child widgets and bypass the
        minimum entirely.

        On first show rather than in ``__init__`` because a pre-show layout
        does not report settled minimums: measured there, BOTH arrangements
        come back as the narrow number and the viewer never leaves the wide one
        (the same trap the pre-show note in ``gui/AGENTS.md`` records).
        """
        layout = self.layout()
        self._apply_width_mode(False)
        layout.activate()
        self._wide_min_w = layout.minimumSize().width()
        self._apply_width_mode(True)
        layout.activate()
        self.setMinimumWidth(layout.minimumSize().width())
        self._apply_width_mode(self.width() < self._wide_min_w)

    def _apply_width_mode(self, narrow: bool) -> None:
        """Lay the viewer out for the width it has.

        Wide: list beside transcript, one row of actions. Narrow: the splitter
        turns vertical, so list and transcript each keep the FULL width and the
        operator still chooses the share between them, and the actions wrap to
        two rows so none of them is pushed off the edge.

        Tk shows one pane at a time here, with a ← button back to the list
        (``gui/history_view.py`` ``_apply_history_pane_visibility``). Stacking
        reaches the same place without a second view state to track, and
        without a row selection that navigates away from the list it was made
        in.
        """
        if narrow == self._narrow:
            return
        self._narrow = narrow

        # Rebuild both rows from empty. Taking an item out leaves its button
        # parented to the bar but unmanaged, which is fine because every one of
        # them is re-added below.
        for row in (self._action_top, self._action_bottom):
            while row.count():
                row.takeAt(0)

        self.summarise_btn.setMinimumWidth(0 if narrow else _ACTION_PRIMARY_W)
        secondary = (self.delete_btn, self.copy_btn, self.export_btn)
        for button in secondary:
            button.setMinimumWidth(0 if narrow else _ACTION_SECONDARY_W)

        self._action_top.addWidget(self.summarise_btn)
        if narrow:
            # A third of the row each, rather than a fixed width that would put
            # them back over the edge this mode exists to keep them inside.
            for button in secondary:
                self._action_bottom.addWidget(button, 1)
        else:
            self._action_top.addStretch(1)
            for button in secondary:
                self._action_top.addWidget(button)

        # PANE_GAP lives in the right pane's own margin, so it has to move to
        # whichever side the pane is now on — left of it when side by side,
        # above it when stacked. Left where it was, the transcript's heading
        # sits flush against the bottom row of the list.
        self._right_box.setContentsMargins(
            *((0, PANE_GAP, 0, 0) if narrow else (PANE_GAP, 0, 0, 0))
        )
        self.splitter.setOrientation(Qt.Vertical if narrow else Qt.Horizontal)
        # Stretch and sizes are per orientation, so both are re-stated here.
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        if narrow:
            # A third to the list: enough rows to pick from, most of the room
            # to what was picked.
            height = max(self.height(), HISTORY_WINDOW_H)
            self.splitter.setSizes([height // 3, height - height // 3])
        else:
            self.splitter.setSizes([LIST_W, HISTORY_WINDOW_W - LIST_W])

    # ── data ─────────────────────────────────────────────────────────────
    def show_tab(self, tab_id: str) -> None:
        """Switch to a tab from outside — the batch window's "Show in history"
        lands on the run it just produced, not on the session list."""
        if tab_id not in _TAB_IDS:
            return
        index = _TAB_IDS.index(tab_id)
        self._tab_group.button(index).setChecked(True)
        self._on_tab(index)

    def _on_tab(self, index: int) -> None:
        tab = _TABS[index][0]
        if tab == self._tab:
            return
        self._tab = tab
        self._close_summary()
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
            # cost_rows carries the display strings; keep the raw sessions too,
            # for the breakdown and the chart.
            self._cost_sessions = self._rows
            self._rows = cost_rows(
                self._cost_sessions,
                duration_fmt=self._t("history_minutes", "{minutes} min"),
                seconds_fmt=self._t("history_seconds", "{seconds} s"),
            )

        # Only the batch tab's titles are filenames, and those keep their
        # extension by losing their middle instead of their tail.
        elide = Qt.ElideMiddle if self._tab == "batch" else Qt.ElideRight
        self.entry_list.blockSignals(True)
        self.entry_list.clear()
        for row in self._rows:
            title, subtitle, tag = self._row_text(row)
            item = QListWidgetItem(title)
            item.setData(RowDelegate.SUB_ROLE, subtitle)
            item.setData(RowDelegate.TAG_ROLE, tag)
            item.setData(RowDelegate.ELIDE_ROLE, elide)
            self.entry_list.addItem(item)
        self.entry_list.blockSignals(False)

        has_any = bool(self._rows)
        self.entry_list.setVisible(has_any)
        self.detail.setVisible(has_any)
        self.empty_label.setVisible(not has_any)
        self.empty_label.setText(self._empty_text())
        self.cost_chart.setVisible(has_any and self._tab == "cost")
        if has_any:
            self.entry_list.setCurrentRow(0)
        else:
            self.format_bar.setVisible(False)
            self.header.setText("")
            self.detail.setPlainText("")
        self._sync_actions()

    def _empty_text(self) -> str:
        if self._tab == "batch":
            return self._t("history_batch_empty", "No processed files yet")
        if self._tab == "cost":
            return self._t("cost_empty", "No cost data yet")
        return self._t("history_empty", "No sessions recorded yet")

    def _duration(self, seconds: int) -> str:
        """Seconds under a minute (so short clips don't all read "0 min"),
        whole minutes above."""
        if seconds < 60:
            return self._t("history_seconds", "{seconds} s").format(seconds=seconds)
        return self._t("history_minutes", "{minutes} min").format(
            minutes=seconds // 60
        )

    def _row_text(self, row) -> tuple[str, str, str | None]:
        """(title, detail line, right-aligned tag) for one list row."""
        if self._tab == "logs":
            return row.date, f"{row.size_kb} KB", None
        if self._tab == "cost":
            return row.title, row.subtitle, "~" if row.estimated else None

        entries = self._t("history_entries", "{count} entries").format(
            count=row.entry_count
        )
        mark = SUMMARY_MARK if getattr(row, "has_summary", False) else ""
        if self._tab == "batch":
            detail = (
                f"{row.date} {row.time} ({self._duration(row.active_seconds)})"
                f" · {row.language_pair} · {entries}"
            )
            badge = "+".join(f.upper() for f in row.formats)
            return f"{mark}{row.source_name}", detail, badge or None
        detail = (
            f"{row.start_time}–{row.end_time}"
            f" ({self._duration(row.active_seconds)}) · {entries}"
        )
        return f"{mark}{row.date} · {row.language_pair}", detail, None

    def _current(self):
        row = self.entry_list.currentRow()
        return self._rows[row] if 0 <= row < len(self._rows) else None

    def _on_select(self, _row: int) -> None:
        entry = self._current()
        self._sync_actions()
        # A batch-only affordance; _show_batch brings it back where it belongs.
        if self._tab != "batch":
            self.format_bar.setVisible(False)
        if entry is None:
            self.header.setText("")
            self.detail.setPlainText("")
            return
        if self._tab == "logs":
            self.header.setText(entry.date)
            self.detail.setPlainText(self._read_text(entry.path))
            self.detail.moveCursor(QTextCursor.End)  # newest lines at the bottom
        elif self._tab == "cost":
            self.header.setText(entry.title)
            self.cost_chart.set_data(self._cost_sessions, entry.session_id)
            self.detail.setPlainText(self._cost_text(entry))
        elif self._tab == "batch":
            self.header.setText(self._detail_header(entry))
            self._show_batch(entry)
        else:
            self.header.setText(self._detail_header(entry))
            self.detail.setPlainText(self._transcript_text(entry))

    def _sync_actions(self) -> None:
        entry = self._current()
        # Summarising sends a transcript to a translation provider; the raw log
        # and the cost records have nothing to summarise.
        self.summarise_btn.setVisible(self._tab in _TRANSCRIPT_TABS)
        self.summarise_btn.setEnabled(entry is not None)
        for button in (self.copy_btn, self.export_btn, self.delete_btn):
            button.setEnabled(entry is not None)

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
            return str(exc)

    # ── cost tab ─────────────────────────────────────────────────────────
    def _cost_text(self, row) -> str:
        session = self._cost_session(row.session_id)
        if session is None:
            return ""
        return cost_breakdown_lines(
            session,
            estimate_note=self._t("cost_estimate_note", "Estimate — public list prices"),
            unpriced_note=self._t("cost_unpriced", "unpriced"),
            requests_label=self._t("cost_requests", "requests"),
        )

    def _cost_session(self, session_id: str):
        return next(
            (s for s in self._cost_sessions if str(s.get("id", "")) == session_id),
            None,
        )

    def _on_cost_bar(self, session_id: str) -> None:
        for index, row in enumerate(self._rows):
            if getattr(row, "session_id", None) == session_id:
                self.entry_list.setCurrentRow(index)
                return

    # ── batch tab ────────────────────────────────────────────────────────
    def _show_batch(self, run) -> None:
        # Keep the current format if the run still offers it, else the first
        # available — SRT is listed first, so it wins whenever both exist.
        available = run.formats or ["txt"]
        if self._batch_format not in available:
            self._batch_format = available[0]
        # The toggle is only meaningful for a run that holds both formats.
        self.format_bar.setVisible(len(available) > 1)
        self._sync_format_buttons()
        self.detail.setPlainText(self._batch_text(run))

    def _sync_format_buttons(self) -> None:
        for name, button in self.format_buttons.items():
            button.setChecked(name == self._batch_format)

    def _on_format(self, fmt: str) -> None:
        run = self._current()
        if (
            run is not None
            and self._tab == "batch"
            and fmt != self._batch_format
            and fmt in (run.formats or [])
        ):
            self._batch_format = fmt
            self.detail.setPlainText(self._batch_text(run))
        # Always re-sync: these are plain toggles, so clicking the active one
        # (or one this run does not offer) must not leave both looking checked.
        self._sync_format_buttons()

    def _batch_text(self, run) -> str:
        """The exact SRT from the stored sidecar, or the rebuilt transcript."""
        if self._batch_format == "srt":
            try:
                with open(batch_srt_path(run.path), encoding="utf-8-sig") as handle:
                    return handle.read()
            except OSError as exc:
                log(f"Batch SRT read error for {run.path}: {exc}", level="WARNING")
                # Fall through to the transcript so the pane is never blank.
        return self._batch_transcript(run)

    def _batch_transcript(self, run) -> str:
        """Rebuild the plain transcript document from the record — identical to
        the .txt the run wrote next to the source file."""
        from batch.text_writer import build_text

        try:
            entries = parse_history_file(run.path)
        except Exception as exc:  # noqa: BLE001
            log(f"Batch read error for {run.path}: {exc}", level="WARNING")
            entries = []
        records = [
            (0.0, source.text, target.text if target is not None else source.text)
            for source, target in pair_entries(entries)
        ]
        languages = read_batch_languages(run.path)
        if languages:
            source_name, target_name = languages
        else:  # legacy record: fall back to the two-letter tags
            source_name = entries[0].lang if entries else ""
            target_name = entries[1].lang if len(entries) > 1 else ""
        return build_text(records, source_name, target_name)

    # ── history tab ──────────────────────────────────────────────────────
    def _transcript_text(self, entry) -> str:
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
        text = self.detail.toPlainText().strip()
        if not text:
            return
        QGuiApplication.clipboard().setText(text)
        _flash(self.copy_btn, self._t("history_copied", "Copied"),
               self._t("history_copy", "Copy"))

    def _on_export(self) -> None:
        entry = self._current()
        if entry is None:
            return
        # Batch runs and cost records export what is SHOWN (the chosen format,
        # the breakdown); the other tabs copy the source file itself.
        if self._tab in ("batch", "cost"):
            self._export_shown_text(entry)
            return
        extension = ".log" if self._tab == "logs" else ".txt"
        target = self._ask_save_path(
            f"MinbarLive_{entry.date}{extension}",
            "Log files" if self._tab == "logs" else "Text files",
            extension,
        )
        if not target:
            return
        try:
            shutil.copyfile(entry.path, target)
        except OSError as exc:
            log(f"Export failed: {exc}", level="ERROR")
            _warn(self, self._t("history_export", "Save…"), str(exc), self._t)
            return
        log(f"Exported to {target}")

    def _export_shown_text(self, entry) -> None:
        text = self.detail.toPlainText().rstrip("\n")
        if not text.strip():
            return
        if self._tab == "cost":
            suggestion, label, extension = "MinbarLive_cost.txt", "Text files", ".txt"
        else:
            fmt = self._batch_format or "txt"
            extension = f".{fmt}"
            stem = os.path.splitext(entry.source_name)[0] or f"MinbarLive_{entry.date}"
            suggestion = f"{stem}{extension}"
            label = "SubRip subtitles" if fmt == "srt" else "Text files"
        target = self._ask_save_path(suggestion, label, extension)
        if not target:
            return
        try:
            # UTF-8 with BOM, matching what the batch writers produce.
            with open(target, "w", encoding="utf-8-sig", newline="\n") as handle:
                handle.write(text + "\n")
        except OSError as exc:
            log(f"Export failed: {exc}", level="ERROR")
            _warn(self, self._t("history_export", "Save…"), str(exc), self._t)
            return
        log(f"Exported to {target}")

    def _ask_save_path(self, suggestion: str, label: str, extension: str) -> str:
        target, _filter = QFileDialog.getSaveFileName(
            self,
            self._t("history_export", "Save…"),
            suggestion,
            f"{label} (*{extension});;"
            f"{self._t('batch_all_files', 'All files')} (*.*)",
        )
        return target

    def _on_delete(self) -> None:
        entry = self._current()
        if entry is None:
            return
        if not self._confirm(
            self._t("history_delete", "Delete"),
            self._t(
                "history_delete_confirm",
                "Delete this history file?\nThis cannot be undone.",
            ),
        ):
            return
        # Cost sessions live in their own store, keyed by id, not as a file.
        if self._tab == "cost":
            if delete_cost_session(entry.session_id):
                log("Deleted cost session")
            self._reload()
            return
        try:
            os.remove(entry.path)
            # A record's sidecars (saved summary, stored SRT) go with it.
            if self._tab in _TRANSCRIPT_TABS:
                self._remove_if_present(summary_path(entry.path))
            if self._tab == "batch":
                self._remove_if_present(batch_srt_path(entry.path))
        except OSError as exc:
            log(f"Delete failed: {exc}", level="ERROR")
            _warn(self, self._t("history_delete", "Delete"), str(exc), self._t)
            return
        log(f"Deleted: {entry.path}")
        self._reload()

    @staticmethod
    def _remove_if_present(path: str) -> None:
        if os.path.exists(path):
            os.remove(path)

    def _confirm(self, title: str, body: str) -> bool:
        return ask_yes_no(
            self, title, body, default_yes=False, translate=self._t
        )

    # ── summary dialog ───────────────────────────────────────────────────
    def _on_summarise(self) -> None:
        """Both sessions and batch runs are history-format records, so one
        dialog serves either (BatchRun exposes the same fields it reads)."""
        entry = self._current()
        if entry is None or self._tab not in _TRANSCRIPT_TABS:
            return
        if self._summary_dialog is not None and self._summary_dialog.isVisible():
            self._summary_dialog.raise_()
            self._summary_dialog.activateWindow()
            return
        settings = self._settings
        if settings is None:
            from utils.settings import load_settings

            settings = load_settings()
        self._summary_dialog = SummaryDialog(
            entry,
            self._t,
            settings,
            getattr(self._panel, "texts", {}),
            self,
        )
        # A newly written summary flips the 📝 marker on the row it came from.
        row = self.entry_list.currentRow()
        self._summary_dialog.saved.connect(lambda: self._mark_summarised(row))
        self._summary_dialog.show()

    def _mark_summarised(self, row: int) -> None:
        item = self.entry_list.item(row)
        if item is not None and not item.text().startswith(SUMMARY_MARK):
            item.setText(f"{SUMMARY_MARK}{item.text()}")

    def _close_summary(self) -> None:
        if self._summary_dialog is not None:
            self._summary_dialog.close()
            self._summary_dialog = None

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._close_summary()
        super().closeEvent(event)

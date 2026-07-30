"""Qt announcement window: show a message large on the subtitle screen.

Port of ``gui/announce_view.py``. Recents and favourites are plain lists on
``Settings``, so the state model is reused unchanged; this renders it and asks
the control panel to put the message on the overlay.

Going through the panel matters: with the default hide policy there is no
overlay while the pipeline is stopped, and "the talk starts in 10 minutes" is
exactly the message you want to show *before* starting. The panel creates the
overlay on demand and tears it down again when the announcement is cleared.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from config import (
    ANNOUNCEMENT_DURATIONS_SECONDS,
    ANNOUNCEMENT_FAVORITES_MAX,
    ANNOUNCEMENT_HISTORY_MAX,
)
from gui_qt.widgets import Dropdown
from utils.logging import log
from utils.settings import save_settings

# Translation keys for the duration choices, in ANNOUNCEMENT_DURATIONS_SECONDS
# order. The trailing 0 means "until stopped".
_DURATION_KEYS = (
    ("announce_duration_10s", "10 seconds"),
    ("announce_duration_30s", "30 seconds"),
    ("announce_duration_1m", "1 minute"),
    ("announce_duration_5m", "5 minutes"),
    ("announce_duration_until_stopped", "Until stopped"),
)
_DEFAULT_DURATION_INDEX = 1  # 30 seconds

# How much of a stored message a row button shows before eliding.
_PREVIEW_CHARS = 42


class AnnounceWindow(QDialog):
    def __init__(self, translate, settings, panel=None):
        # ``panel`` is the overlay's owner, which is normally also the Qt
        # parent — but not necessarily a widget: tests pass a stand-in that
        # only implements show_announcement/clear_announcement, and Qt would
        # reject that as a parent.
        super().__init__(panel if isinstance(panel, QWidget) else None)
        self._t = translate
        self.settings = settings
        self._panel = panel
        self._auto_clear = QTimer(self)
        self._auto_clear.setSingleShot(True)
        self._auto_clear.timeout.connect(self.stop_announcement)

        self.setWindowTitle(self._t("announce_title", "Announcement"))
        if isinstance(panel, QWidget):
            self.setWindowIcon(panel.windowIcon())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)
        # The favourites and recents lists change length as the operator pins
        # and deletes entries; the window has to follow instead of clipping.
        outer.setSizeConstraint(QLayout.SetMinimumSize)

        title = QLabel("⚑  " + self._t("announce_title", "Announcement"))
        title.setObjectName("hero")
        sub = QLabel(
            self._t("announce_sub", "Show a message large on the subtitle screen")
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        outer.addWidget(title)
        outer.addWidget(sub)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(self._t("announce_title", "Announcement"))
        self.text.setFixedHeight(104)
        outer.addWidget(self.text)

        duration_caption = QLabel(self._t("announce_duration_label", "Show for"))
        duration_caption.setObjectName("field")
        self.duration_combo = Dropdown()
        self.duration_combo.addItems([self._t(k, f) for k, f in _DURATION_KEYS])
        self.duration_combo.setCurrentIndex(self._valid_duration_index())
        self.duration_combo.currentIndexChanged.connect(self._on_duration_changed)
        outer.addWidget(duration_caption)
        outer.addWidget(self.duration_combo)

        self.stop_on_live_stop = QCheckBox(
            self._t("announce_stop_on_live_stop", "Stop when the translation stops")
        )
        self.stop_on_live_stop.setChecked(self.settings.stop_announcement_on_live_stop)
        self.stop_on_live_stop.toggled.connect(self._on_stop_on_live_stop)
        outer.addWidget(self.stop_on_live_stop)

        self.favorites_box, self._favorite_rows = self._list_card(
            "announce_favorites", "Favorites"
        )
        self.recent_box, self._recent_rows = self._list_card(
            "announce_recent", "Recently used"
        )
        outer.addWidget(self.favorites_box)
        outer.addWidget(self.recent_box)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.send_btn = QPushButton(self._t("announce_send", "Send"))
        self.send_btn.setObjectName("accent")
        self.send_btn.setMinimumHeight(44)
        self.send_btn.clicked.connect(self.send_announcement)
        self.stop_btn = QPushButton(self._t("announce_stop", "Stop"))
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.clicked.connect(self.stop_announcement)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        outer.addLayout(buttons)

        self._refresh_lists()
        self._sync_stop_button()
        self.resize(520, self.sizeHint().height())

    # ── helpers ──────────────────────────────────────────────────────────
    def _valid_duration_index(self) -> int:
        index = self.settings.announcement_duration_index
        return (
            index
            if 0 <= index < len(ANNOUNCEMENT_DURATIONS_SECONDS)
            else _DEFAULT_DURATION_INDEX
        )

    def _list_card(self, key: str, fallback: str) -> tuple[QFrame, QVBoxLayout]:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(6)
        heading = QLabel(self._t(key, fallback))
        heading.setObjectName("field")
        box.addWidget(heading)
        rows = QVBoxLayout()
        rows.setSpacing(4)
        box.addLayout(rows)
        return card, rows

    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
            elif item.layout() is not None:
                AnnounceWindow._clear(item.layout())

    @staticmethod
    def _preview(text: str) -> str:
        collapsed = " ".join(text.split())
        return (
            collapsed
            if len(collapsed) <= _PREVIEW_CHARS
            else collapsed[: _PREVIEW_CHARS - 1] + "…"
        )

    def _refresh_lists(self) -> None:
        self._clear(self._favorite_rows)
        self._clear(self._recent_rows)

        favorites = list(self.settings.announcement_favorites)
        for text in favorites:
            self._favorite_rows.addWidget(self._entry_row(text, starred=True))
        # Hidden entirely while empty, so users who never pin anything see no
        # extra clutter.
        self.favorites_box.setVisible(bool(favorites))

        # Favourited texts live only in Favorites — never duplicated here.
        recents = [t for t in self.settings.announcement_history if t not in favorites]
        if not recents:
            empty = QLabel(self._t("announce_recent_empty", "No recent announcements"))
            empty.setObjectName("muted")
            self._recent_rows.addWidget(empty)
        for text in recents:
            self._recent_rows.addWidget(self._entry_row(text, starred=False))
        self._resize_to_content()

    def _resize_to_content(self) -> None:
        """Grow (or shrink) to the content's natural height — pinning a fifth
        favourite must not push the Send button out of the window."""
        self.layout().activate()
        self.resize(self.width(), self.sizeHint().height())

    def _entry_row(self, text: str, *, starred: bool) -> QWidget:
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        use = QPushButton(self._preview(text))
        use.setObjectName("row")
        use.setToolTip(text)
        use.clicked.connect(lambda: self.text.setPlainText(text))
        row.addWidget(use, 1)

        star = QPushButton("★" if starred else "☆")
        star.setObjectName("icon")
        star.setFixedSize(34, 34)
        star.setToolTip(self._t("announce_favorites", "Favorites"))
        star.clicked.connect(
            (lambda: self._unfavorite(text)) if starred else (lambda: self._favorite(text))
        )
        row.addWidget(star)

        if not starred:
            # Recents are deletable; a pinned favourite is removed by unpinning
            # it, which is what the filled star does.
            delete = QPushButton("✕")
            delete.setObjectName("icon")
            delete.setFixedSize(34, 34)
            delete.clicked.connect(lambda: self._delete_recent(text))
            row.addWidget(delete)
        return holder

    # ── list mutations ───────────────────────────────────────────────────
    def _favorite(self, text: str) -> None:
        """Pin a recent text and drop the now-redundant copy from Recent.

        Refuses once the cap is reached rather than silently evicting the
        oldest favourite — dropping a pinned reminder to make room would
        defeat the point of pinning it.
        """
        favorites = list(self.settings.announcement_favorites)
        if text not in favorites and len(favorites) >= ANNOUNCEMENT_FAVORITES_MAX:
            QMessageBox.information(
                self,
                self._t("announce_favorites", "Favorites"),
                self._t(
                    "announce_favorites_full",
                    "You can pin up to {max} favorites. Remove one before "
                    "adding another.",
                ).format(max=ANNOUNCEMENT_FAVORITES_MAX),
            )
            return
        favorites = [t for t in favorites if t != text]
        favorites.insert(0, text)
        self.settings.announcement_favorites = favorites[:ANNOUNCEMENT_FAVORITES_MAX]
        self.settings.announcement_history = [
            t for t in self.settings.announcement_history if t != text
        ]
        save_settings(self.settings)
        self._refresh_lists()

    def _unfavorite(self, text: str) -> None:
        """Unpin a text. It is not restored to Recent — sending it again will
        naturally re-add it there."""
        self.settings.announcement_favorites = [
            t for t in self.settings.announcement_favorites if t != text
        ]
        save_settings(self.settings)
        self._refresh_lists()

    def _delete_recent(self, text: str) -> None:
        self.settings.announcement_history = [
            t for t in self.settings.announcement_history if t != text
        ]
        save_settings(self.settings)
        self._refresh_lists()

    def _on_duration_changed(self, index: int) -> None:
        self.settings.announcement_duration_index = index
        save_settings(self.settings)

    def _on_stop_on_live_stop(self, checked: bool) -> None:
        self.settings.stop_announcement_on_live_stop = checked
        save_settings(self.settings)

    # ── actions ──────────────────────────────────────────────────────────
    def send_announcement(self) -> None:
        text = self.text.toPlainText().strip()
        if not text or self._panel is None:
            return
        self._panel.show_announcement(text)
        self._remember(text)
        log("Announcement shown on the subtitle overlay.", level="INFO")

        seconds = ANNOUNCEMENT_DURATIONS_SECONDS[self.duration_combo.currentIndex()]
        # A new send replaces any current announcement, timer included.
        self._auto_clear.stop()
        if seconds > 0:  # 0 means "until stopped"
            self._auto_clear.start(seconds * 1000)
        self._sync_stop_button()

    def stop_announcement(self) -> None:
        self._auto_clear.stop()
        if self._panel is not None:
            self._panel.clear_announcement()
        self._sync_stop_button()

    def _sync_stop_button(self) -> None:
        active = self._panel is not None and self._panel.has_active_announcement()
        self.stop_btn.setEnabled(active)
        self.stop_btn.setObjectName("danger" if active else "")
        self.stop_btn.style().unpolish(self.stop_btn)
        self.stop_btn.style().polish(self.stop_btn)

    def _remember(self, text: str) -> None:
        """Most-recent-first, de-duplicated, capped. Favourites are skipped:
        they are pinned in their own list and would otherwise occupy a rotating
        history slot as well — and show up twice."""
        if text in self.settings.announcement_favorites:
            return
        history = [t for t in self.settings.announcement_history if t != text]
        history.insert(0, text)
        self.settings.announcement_history = history[:ANNOUNCEMENT_HISTORY_MAX]
        save_settings(self.settings)
        self._refresh_lists()

    # Deliberately no closeEvent override: closing this window is not the same
    # as stopping the message, so the announcement stays on screen AND the
    # auto-clear timer must keep running. Cancelling it here would strand a
    # timed announcement on the subtitle screen indefinitely. The panel holds a
    # reference to this window, so the timer survives the window being hidden.

"""Qt announcement window: show a message large on the subtitle screen.

Port of ``gui/announce_view.py``. Recents and favourites are plain lists on
``Settings``, so the state model is reused unchanged; this only renders it and
drives ``SubtitleWindow.set_announcement`` / ``clear_announcement``.
"""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from config import (
    ANNOUNCEMENT_DURATIONS_SECONDS,
    ANNOUNCEMENT_FAVORITES_MAX,
    ANNOUNCEMENT_HISTORY_MAX,
)
from gui_qt.widgets import SegmentedControl
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


class AnnounceWindow(QDialog):
    def __init__(self, translate, settings, get_overlay, parent=None):
        super().__init__(parent)
        self._t = translate
        self.settings = settings
        self._get_overlay = get_overlay
        self._auto_clear = QTimer(self)
        self._auto_clear.setSingleShot(True)
        self._auto_clear.timeout.connect(self.stop_announcement)

        self.setWindowTitle(self._t("announce_title", "Announcement"))
        self.resize(620, 560)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 20, 20, 20)
        outer.setSpacing(14)

        sub = QLabel(
            self._t("announce_sub", "Show a message large on the subtitle screen")
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        outer.addWidget(sub)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(self._t("announce_title", "Announcement"))
        self.text.setFixedHeight(110)
        outer.addWidget(self.text)

        duration_row = QHBoxLayout()
        duration_row.addWidget(QLabel(self._t("announce_duration_label", "Show for")))
        self.duration_segment = SegmentedControl(
            [self._t(k, f) for k, f in _DURATION_KEYS],
            self._valid_duration_index(),
        )
        self.duration_segment.changed.connect(self._on_duration_changed)
        duration_row.addWidget(self.duration_segment, 1)
        outer.addLayout(duration_row)

        self.favorites_box = self._list_card("announce_favorites", "Favorites")
        self.recent_box = self._list_card("announce_recent", "Recently used")
        outer.addWidget(self.favorites_box)
        outer.addWidget(self.recent_box)

        buttons = QHBoxLayout()
        self.send_btn = QPushButton(self._t("announce_send", "Send"))
        self.send_btn.setObjectName("accent")
        self.send_btn.clicked.connect(self.send_announcement)
        self.stop_btn = QPushButton(self._t("announce_stop", "Stop"))
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(self.stop_announcement)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(self.stop_btn)
        outer.addLayout(buttons)
        outer.addStretch(1)

        self._refresh_lists()

    # ── helpers ──────────────────────────────────────────────────────────
    def _valid_duration_index(self) -> int:
        idx = self.settings.announcement_duration_index
        return idx if 0 <= idx < len(ANNOUNCEMENT_DURATIONS_SECONDS) else 1

    def _list_card(self, key: str, fallback: str) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 12, 16, 12)
        box.setSpacing(6)
        heading = QLabel(self._t(key, fallback))
        heading.setObjectName("heading")
        box.addWidget(heading)
        card._rows = QVBoxLayout()  # populated by _refresh_lists
        card._rows.setSpacing(4)
        box.addLayout(card._rows)
        return card

    @staticmethod
    def _clear(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _refresh_lists(self) -> None:
        self._clear(self.favorites_box._rows)
        self._clear(self.recent_box._rows)

        for text in self.settings.announcement_favorites:
            self.favorites_box._rows.addLayout(self._entry_row(text, starred=True))

        recents = [
            t
            for t in self.settings.announcement_history
            if t not in self.settings.announcement_favorites
        ]
        if not recents:
            empty = QLabel(self._t("announce_recent_empty", "No recent announcements"))
            empty.setObjectName("muted")
            self.recent_box._rows.addWidget(empty)
        for text in recents:
            self.recent_box._rows.addLayout(self._entry_row(text, starred=False))

    def _entry_row(self, text: str, *, starred: bool) -> QHBoxLayout:
        row = QHBoxLayout()
        use = QPushButton(text if len(text) <= 60 else text[:57] + "…")
        use.setToolTip(text)
        use.clicked.connect(lambda: self.text.setPlainText(text))
        star = QPushButton("★" if starred else "☆")
        star.setFixedWidth(44)
        star.clicked.connect(lambda: self._toggle_favorite(text))
        row.addWidget(use, 1)
        row.addWidget(star)
        return row

    def _toggle_favorite(self, text: str) -> None:
        favorites = list(self.settings.announcement_favorites)
        if text in favorites:
            favorites.remove(text)
        else:
            if len(favorites) >= ANNOUNCEMENT_FAVORITES_MAX:
                self._notice(
                    self._t(
                        "announce_favorites_full",
                        "At most {max} favorites can be pinned.",
                    ).format(max=ANNOUNCEMENT_FAVORITES_MAX)
                )
                return
            favorites.insert(0, text)
        self.settings.announcement_favorites = favorites
        save_settings(self.settings)
        self._refresh_lists()

    def _notice(self, message: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.information(self, "MinbarLive", message)

    def _on_duration_changed(self, index: int) -> None:
        self.settings.announcement_duration_index = index
        save_settings(self.settings)

    # ── actions ──────────────────────────────────────────────────────────
    def send_announcement(self) -> None:
        text = self.text.toPlainText().strip()
        overlay = self._get_overlay()
        if not text or overlay is None:
            return
        overlay.set_announcement(text)
        self._remember(text)

        seconds = ANNOUNCEMENT_DURATIONS_SECONDS[self.duration_segment.current_index()]
        self._auto_clear.stop()
        if seconds > 0:  # 0 means "until stopped"
            self._auto_clear.start(seconds * 1000)

    def stop_announcement(self) -> None:
        self._auto_clear.stop()
        overlay = self._get_overlay()
        if overlay is not None:
            overlay.clear_announcement()

    def _remember(self, text: str) -> None:
        """Most-recent-first, de-duplicated, capped. Favourites are excluded —
        they have their own list and would otherwise occupy both."""
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

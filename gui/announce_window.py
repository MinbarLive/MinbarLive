"""Announcement window: show a message large on the subtitle screen.

Recents and favourites are plain lists on ``Settings``; this window renders them
and asks the control panel to put the message on the overlay.

Going through the panel matters: with the default hide policy there is no
overlay while the pipeline is stopped, and "the talk starts in 10 minutes" is
exactly the message you want to show *before* starting. The panel creates the
overlay on demand and tears it down again when the announcement is cleared.

Laid out like the batch window — hero, cards, and a Send/Stop bar that does not
scroll. The lists grow as favourites and recents are pinned, and Send has to
stay reachable however long they get.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from config import (
    ANNOUNCEMENT_DURATIONS_SECONDS,
    ANNOUNCEMENT_FAVORITES_MAX,
    ANNOUNCEMENT_HISTORY_MAX,
)
from gui.dialogs import show_message
from gui.widgets import Dropdown, field
from gui.window_size import SECONDARY_WINDOW_W, apply_content_size
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

# Width the window is laid out for; the height follows the content — see
# _resize_to_content. Shared with the settings and batch windows, which are the
# same shape (see gui/window_size.py), so the three open at one size.
ANNOUNCE_WINDOW_W = SECONDARY_WINDOW_W

_PAD = 16
_CARD_GAP = 12
_CARD_PAD = 16


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
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setSizeAdjustPolicy(QAbstractScrollArea.AdjustToContents)
        self.body = QWidget()
        column = QVBoxLayout(self.body)
        column.setContentsMargins(_PAD, _PAD, _PAD, _PAD)
        column.setSpacing(_CARD_GAP)
        column.addLayout(self._hero())
        column.addWidget(self._compose_card())

        self.favorites_box, self._favorite_rows = self._list_card(
            "announce_favorites", "Favorites"
        )
        self.recent_box, self._recent_rows = self._list_card(
            "announce_recent", "Recently used"
        )
        column.addWidget(self.favorites_box)
        column.addWidget(self.recent_box)
        column.addStretch(1)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

        self.action_bar = self._action_bar()
        outer.addWidget(self.action_bar)

        self._refresh_lists()
        self._sync_stop_button()
        self._resize_to_content()

    # ── build ────────────────────────────────────────────────────────────
    def _hero(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(12)
        symbol = QLabel("⚑")
        symbol.setObjectName("card_symbol")
        symbol.setFixedSize(44, 44)
        symbol.setAlignment(Qt.AlignCenter)
        row.addWidget(symbol, 0, Qt.AlignTop)

        stack = QVBoxLayout()
        stack.setSpacing(2)
        title = QLabel(self._t("announce_title", "Announcement"))
        title.setObjectName("card_title")
        sub = QLabel(
            self._t("announce_sub", "Show a message large on the subtitle screen")
        )
        sub.setObjectName("muted")
        sub.setWordWrap(True)
        stack.addWidget(title)
        stack.addWidget(sub)
        row.addLayout(stack, 1)
        return row

    def _compose_card(self) -> QFrame:
        """The message, how long it shows, and what happens on Stop.

        Deliberately without a heading of its own: the hero directly above
        already names it, and a second "Announcement" one line down is noise.
        """
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(_CARD_PAD, _CARD_PAD - 2, _CARD_PAD, _CARD_PAD - 2)
        box.setSpacing(10)

        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(self._t("announce_title", "Announcement"))
        self.text.setFixedHeight(104)
        box.addWidget(self.text)

        self.duration_combo = Dropdown([self._t(k, f) for k, f in _DURATION_KEYS])
        self.duration_combo.setCurrentIndex(self._valid_duration_index())
        self.duration_combo.currentIndexChanged.connect(self._on_duration_changed)
        box.addWidget(
            field(self._t("announce_duration_label", "Show for"), self.duration_combo)
        )

        self.stop_on_live_stop = QCheckBox(
            self._t("announce_stop_on_live_stop", "Stop when the translation stops")
        )
        self.stop_on_live_stop.setChecked(self.settings.stop_announcement_on_live_stop)
        self.stop_on_live_stop.toggled.connect(self._on_stop_on_live_stop)
        box.addWidget(self.stop_on_live_stop)
        return card

    def _action_bar(self) -> QWidget:
        """Send / Stop, pinned below the scroll area.

        Five favourites and five recents make the content taller than a laptop
        screen; laid out with the cards, Send went off the bottom edge.
        """
        holder = QWidget()
        box = QHBoxLayout(holder)
        box.setContentsMargins(_PAD, 4, _PAD, _PAD - 2)
        box.setSpacing(8)
        self.send_btn = QPushButton(self._t("announce_send", "Send"))
        self.send_btn.setObjectName("accent")
        self.send_btn.setMinimumHeight(44)
        self.send_btn.clicked.connect(self.send_announcement)
        self.stop_btn = QPushButton(self._t("announce_stop", "Stop"))
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.clicked.connect(self.stop_announcement)
        box.addWidget(self.send_btn)
        box.addWidget(self.stop_btn)
        return holder

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
        box.setContentsMargins(_CARD_PAD, _CARD_PAD - 4, _CARD_PAD, _CARD_PAD - 4)
        box.setSpacing(8)
        heading = QLabel(self._t(key, fallback))
        heading.setObjectName("heading")
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

    @staticmethod
    def _add_row(rows: QVBoxLayout, widget: QWidget) -> None:
        """Add a row AND show it.

        A widget added to the layout of a window that is already on screen
        stays hidden until the layout next activates — and a hidden item is
        skipped entirely by the layout's size hint, so a height measured in
        this same call describes the list without the rows just added. That is
        what made the window lag one entry behind its content and open too
        short for it. Showing it here is safe: addWidget has parented it, so
        this cannot be the "parentless widget shown as a top-level window"
        trap.
        """
        rows.addWidget(widget)
        widget.setVisible(True)

    def _refresh_lists(self) -> None:
        self._clear(self._favorite_rows)
        self._clear(self._recent_rows)

        favorites = list(self.settings.announcement_favorites)
        for text in favorites:
            self._add_row(self._favorite_rows, self._entry_row(text, starred=True))
        # Hidden entirely while empty, so users who never pin anything see no
        # extra clutter.
        self.favorites_box.setVisible(bool(favorites))

        # Favourited texts live only in Favorites — never duplicated here.
        recents = [t for t in self.settings.announcement_history if t not in favorites]
        if not recents:
            empty = QLabel(self._t("announce_recent_empty", "No recent announcements"))
            empty.setObjectName("muted")
            self._add_row(self._recent_rows, empty)
        for text in recents:
            self._add_row(self._recent_rows, self._entry_row(text, starred=False))
        self._resize_to_content()

    def _natural_height(self) -> int:
        """Height the cards + the action bar want at the window's width."""
        layout = self.body.layout()
        layout.activate()
        if layout.hasHeightForWidth():
            body = layout.totalHeightForWidth(ANNOUNCE_WINDOW_W)
        else:
            body = layout.totalSizeHint().height()
        return body + self.action_bar.sizeHint().height()

    def _resize_to_content(self) -> None:
        """Grow (or shrink) to the content's natural height — pinning a fifth
        favourite must not push the Send button out of the window.

        Two things this has to get right, and the first version got neither:
        adding or removing a row invalidates the layout through a POSTED event,
        so a height read in the same call describes the list as it was one
        change ago (the window then lagged a row behind, and clipped); and past
        the shared cap the window must stop growing and let the cards scroll
        instead.
        """
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        apply_content_size(self, self._natural_height())

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
            show_message(
                self,
                self._t("announce_favorites", "Favorites"),
                self._t(
                    "announce_favorites_full",
                    "You can pin up to {max} favorites.\n"
                    "Remove a favorite before adding another.",
                ).format(max=ANNOUNCEMENT_FAVORITES_MAX),
                kind="info",
                translate=self._t,
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
        # Sending can CREATE the overlay (hide-on-stop), and an always-on-top
        # overlay comes up above this window. Take the front back, or the
        # operator is left looking at the message they just sent instead of at
        # the window they sent it from.
        self._raise()

    def stop_announcement(self) -> None:
        self._auto_clear.stop()
        if self._panel is not None:
            self._panel.clear_announcement()
        self._sync_stop_button()

    def release(self) -> None:
        """Give up a pending auto-clear before this window is destroyed.

        Closing the window keeps a running announcement — the panel owns that
        state — but DESTROYING it takes the auto-clear timer with it, and the
        message would then stay on the overlay for good. The two paths that
        destroy this window (a GUI-language change and a window-style change)
        are both rare enough that clearing a timed message early is the lesser
        surprise. An "until stopped" announcement has no timer to lose and is
        left alone.
        """
        if self._auto_clear.isActive():
            self.stop_announcement()

    def _raise(self) -> None:
        """Keep this window in front, as the Tk one does.

        A plain ``raise_()`` cannot rise above an always-on-top window, and
        sending an announcement can CREATE one (the overlay). The panel is
        asked to put this window on the same footing first — Tk's
        ``_sync_announce_topmost`` followed by lift/focus.
        """
        if not self.isVisible():
            return
        bring = getattr(self._panel, "bring_to_front", None)
        if callable(bring):
            bring(self)
            return
        self.raise_()
        self.activateWindow()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        # The window is kept alive and re-shown, so the panel may have cleared
        # the announcement (a session stop) while it was hidden.
        super().showEvent(event)
        self._sync_stop_button()
        # Re-measure: a card's rows only report their height once the window
        # they are in has been shown, so the size computed while it was still
        # off screen is a list short.
        self._resize_to_content()

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

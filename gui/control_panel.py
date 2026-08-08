"""The control panel: four cards in a reflowing 1/2/3-column grid, a header
button row, and a collapsible log panel on the right.

The arrangement was carried over from the CustomTkinter panel deliberately and
should not be "modernised" without a reason an operator would recognise: they
know where things are, and "it moved" is a real cost. That includes the control
types — segmented buttons for the theme and both 3-way selectors, −/+ steppers
for font size and scroll speed, a slider only for height. Don't swap in
dropdowns.

What is deliberately absent is the measurement machinery Tk needed to achieve
that layout: a manual responsive scale factor, an idle-requeueing correction
loop for card alignment (which divided by the wrong scale factor and froze the
panel in PR #43), collapsed-margin arithmetic, work-area probing. Qt layouts do
that arithmetic. Only the column count is ours to decide — see the responsive
card grid section below.
"""

from __future__ import annotations

import os
import re
import threading

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from config import AUTO_STOP_INACTIVITY_SECONDS, ICON_PATH_PNG, ICON_PATH_PNG_ON_DARK
from gui.api_keys import activate_stored_keys, ensure_keys
from gui.card_grid import CardGrid
from gui.control_state import (
    STRATEGY_IDS,
    apply_strategy,
    current_strategy_index,
    effective_subtitle_mode,
    repair_default_provider,
    required_key_providers,
    subtitle_mode_choices,
    visible_provider_choices,
)
from gui.device_list import (
    BLACKHOLE_URL,
    find_input_device_position,
    get_input_devices,
    loopback_supported,
)
from gui.dialogs import show_message
from gui.i18n import load_gui_translations
from gui.icons import app_icon
from gui.modal_host import ModalHost
from gui.pipeline_bridge import PipelineBridge, streaming_enabled
from gui.subtitle_window import SubtitleWindow
from gui.theme import current_colors
from gui.widgets import (
    CONTROL_H,
    AudioLevelBar,
    Card,
    Dropdown,
    Expander,
    SegmentedControl,
    Slider,
    Stepper,
    field,
    set_titlebar_dark,
    set_window_on_top,
)
from gui.window_size import MAX_SCREEN_SHARE
from providers import (
    PROVIDER_CHOICES,
    TRANSCRIPTION_PROVIDER_CHOICES,
    get_default_model,
    get_model_choices,
)
from utils.cost_tracking import (
    begin_cost_session,
    cancel_cost_session,
    end_cost_session,
    flush_cost_history,
)
from utils.logging import log, log_queue
from utils.settings import (
    ALWAYS_ON_TOP_MODES,
    BACKDROP_OPACITY_MAX,
    BACKDROP_OPACITY_MIN,
    DEFAULT_SOURCE_FONT_SIZE_BASE,
    PIPELINE_MODE_STREAMING,
    SOURCE_FONT_SIZE_BASE_MAX,
    SOURCE_FONT_SIZE_BASE_MIN,
    SOURCE_LANGUAGES,
    STATIC_LIFT_PERCENT_MAX,
    STATIC_LIFT_PERCENT_MIN,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    SUBTITLE_HIDE_MODES,
    TARGET_LANGUAGE_DISPLAY_NAMES,
    TARGET_LANGUAGE_NAMES,
    WINDOW_HEIGHT_PERCENT_MAX,
    WINDOW_HEIGHT_PERCENT_MIN,
    language_canonical_name,
    language_display_name,
    load_settings,
    save_settings,
)

# The attributes holding the four secondary windows, in the order they are
# torn down. One list, because "for every secondary window" is asked three
# times (always-on-top, a language change, a window-style change).
_SECONDARY_WINDOWS = (
    "_settings_window",
    "_history_window",
    "_batch_window",
    "_announce_window",
)
# With the log open the sidebar keeps this width and the log takes the rest —
# the Tk arrangement. The window is only widened when it cannot hold both.
_SIDEBAR_W_WITH_LOG = 500
_LOG_PANEL_MIN_W = 340
# How often a running session is checked for inactivity, and how often its
# in-progress cost record is written to disk. Both are the Tk panel's numbers:
# the check is cheap, and 30 s bounds what a crash can lose.
_INACTIVITY_CHECK_MS = 15_000
_COST_FLUSH_MS = 30_000
# Delay before an auto-started session begins, so the panel is painted and the
# operator can see what is happening (and reach Stop) before a provider
# handshake starts. The Tk panel waits the same 700 ms.
_AUTO_START_DELAY_MS = 700
# Edge length of the round "?" / swap buttons — the height of the dropdown they
# sit beside (CONTROL_H), so a control row reads as one row.
_HELP_BTN_PX = CONTROL_H
# Breathing room above each section heading inside the Advanced card, so its
# three groups read as groups rather than one long list.
_SECTION_GAP = 8
# Step applied to the original-text divisor per −/+ click, as in gui/typography.
_SOURCE_FONT_STEP = 5.0
# The shipped font_size_base, i.e. the 100% the size stepper counts from.
_FONT_SIZE_BASE_DEFAULT = 40

# Opening size when nothing is stored, both measured rather than chosen.
#
# WIDTH: two columns with room to spare. The 2→3 threshold is a window width of
# **1040** (CardGrid._COL3_MIN_W is 1030 and _available_width reserves the scroll
# bar), and three columns pins the Advanced card open — a different, denser panel
# than the one people are shown in the setup videos. 1000 leaves 40 px of margin
# before that, so a theme or font change cannot tip a fresh install into it.
#
# HEIGHT: the two-column card stack needs a **659 px** window to fit without the
# card area's vertical scroll bar — identical in all six GUI languages, measured
# on a real panel. The old default was 640, nineteen pixels short, so every
# first launch opened already scrolled. 780 clears it with headroom for a card
# that grows later.
#
# Both are clamped to the screen by _default_size: a figure that suits a 2048px
# monitor must not open off the bottom of a 1366x768 laptop.
_DEFAULT_W = 1000
_DEFAULT_H = 780
# Height floor. Small enough that the panel can be dragged down to a corner of
# the screen; everything above it scrolls. The WIDTH floor is not a constant —
# it is measured from the cards, see _apply_minimum_size.
_MIN_WINDOW_H = 420
# The stored "WxH+X+Y" geometry, shared with the Tk panel so both trees read
# one settings file.
_GEOMETRY_RE = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")

def _readable_on(hex_color: str) -> str:
    """Black or white label text, whichever stays legible on ``hex_color``."""
    try:
        r, g, b = (int(hex_color[i : i + 2], 16) for i in (1, 3, 5))
    except (IndexError, ValueError):
        return "#000000"
    # Rec. 601 luma — good enough to pick a contrasting label colour.
    return "#000000" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


_HIDE_MODE_KEYS = {
    "never": ("mode_never", "Never"),
    "stopped": ("mode_when_stopped", "When stopped"),
    "always": ("mode_always", "Always"),
}
_AOT_MODE_KEYS = {
    "never": ("mode_never", "Never"),
    "running": ("mode_when_running", "While running"),
    "always": ("mode_always", "Always"),
}


class ControlPanel(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.settings = load_settings()
        # Heals a stored "Use default" + non-default provider written by early
        # onboarding; must run before any dropdown reads the value.
        stale = repair_default_provider(self.settings)
        if stale:
            log(f"Repaired stale default provider: {stale}", level="INFO")
            save_settings(self.settings)
        self.texts = load_gui_translations(self.settings.gui_language)
        self.subtitle_window: SubtitleWindow | None = None
        self._running = False
        # A Start/Stop in flight on a worker thread (see on_start/on_stop).
        self._starting = False
        self._stopping = False
        self._start_error: Exception | None = None
        self._stop_error: Exception | None = None
        # The device Start began connecting on, and one picked while it was
        # still connecting — applied by _finish_start once the session is up.
        self._started_device: int | None = None
        self._pending_device: int | None = None
        self._announcement_active = False
        self._announcement_text = ""
        # The manual engine+model a "recommended" tick overrode, so unticking
        # can put it back. Session-only: ticking is the state that persists.
        self._manual_translation: tuple[str, str] | None = None
        self._manual_transcription: tuple[str, str] | None = None
        self._log_collapsed = self.settings.log_panel_collapsed
        # (width before opening the log, width it left the window at). Closing
        # hands the first back while the second is still in force — a window
        # the user dragged wider meanwhile is theirs, not ours to undo.
        # Session-only: a stored geometry already records the size on exit.
        self._log_widen: tuple[int, int] | None = None
        activate_stored_keys()

        self.bridge = PipelineBridge(controller, self)
        self.bridge.translation.connect(self._on_translation)
        self.bridge.live_text.connect(self._on_live_text)
        self.bridge.audio_device_lost.connect(self._on_device_lost)

        self.setWindowTitle(self._t("window_title", "MinbarLive"))
        self._apply_window_icon()
        # _build() ends in _apply_log_panel_widths, which sets the window's
        # minimum — before any geometry is applied, so a stored size is never
        # clamped against a stale minimum.
        self._build()
        if not self._restore_window_geometry():
            self.resize(self._default_size())
        self._restore_maximized_state()
        # _build() laid the grid out against the pre-resize size; redo it now
        # that the window has its real width, so the first paint is already
        # right instead of one <Configure> behind.
        self._relayout_columns()

        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._drain_logs)
        self._log_timer.start(150)
        self._level_timer = QTimer(self)
        self._level_timer.timeout.connect(self._poll_input_level)
        self._level_timer.start(200)
        # Session-scoped, unlike the two above: started in _finish_start and
        # stopped in _end_session_tracking, so neither runs while idle.
        self._inactivity_timer = QTimer(self)
        self._inactivity_timer.setInterval(_INACTIVITY_CHECK_MS)
        self._inactivity_timer.timeout.connect(self._check_inactivity)
        self._cost_flush_timer = QTimer(self)
        self._cost_flush_timer.setInterval(_COST_FLUSH_MS)
        self._cost_flush_timer.timeout.connect(self._flush_cost_session)

        # One anonymous request to the GitHub releases API, off the GUI thread;
        # the banner appears only if it answers with a newer version.
        self.update_banner.start_check(
            self.settings.check_for_updates,
            self.settings.include_prereleases,
            self.settings.skipped_update_version,
        )

        # With hide mode "never" (the default) the overlay is open even while
        # stopped — that is what makes the stopped hint and a stopped-session
        # announcement possible at all.
        self._apply_subtitle_hide_mode()
        # Before the first show(), so an "always" panel comes up on top rather
        # than being recreated a frame later.
        self._apply_always_on_top()

        # Last in __init__ and on a timer, so the panel is fully built and shown
        # before a start that may block on a provider handshake (or put a
        # missing-key dialog up) begins.
        if self.settings.auto_start:
            QTimer.singleShot(_AUTO_START_DELAY_MS, self.on_start)

    def _t(self, key: str, fallback: str) -> str:
        return self.texts.get(key, fallback)

    def _clean_label(self, key: str, fallback: str) -> str:
        """A translated label with any leading symbol stripped, so prefixing
        our own glyph cannot produce "▶  ▶ Start"."""
        text = self._t(key, fallback).strip()
        while text and not (text[0].isalnum() or text[0] in "ÄÖÜ"):
            text = text[1:].lstrip()
        return text or fallback

    # ── window geometry ──────────────────────────────────────────────────
    def _default_size(self) -> QSize:
        """Opening size when nothing is stored, clamped to this screen.

        The clamp is the point. `_DEFAULT_H` is picked so the cards fit without
        a scroll bar, which makes it a figure about the CONTENT — and content
        does not shrink to suit a 768 px laptop. Without the clamp a default
        chosen on a tall monitor opens partly below the taskbar on a short one,
        where the window cannot even be dragged up to reach its own title bar.
        Scrolling on a small screen is the correct outcome; an unreachable
        window is not.

        The share is `window_size.MAX_SCREEN_SHARE`, the same one the secondary
        windows cap their height with, so the app has one idea of "too big for
        this screen".
        """
        width = _DEFAULT_W
        if not self._log_collapsed:
            # The log panel claims its own width beside the sidebar; without
            # this the cards would open narrower than one column.
            width = max(width, _SIDEBAR_W_WITH_LOG + _LOG_PANEL_MIN_W)
        height = _DEFAULT_H
        screen = self.screen()
        if screen is not None:
            room = screen.availableGeometry()
            width = min(width, int(room.width() * MAX_SCREEN_SHARE))
            height = min(height, int(room.height() * MAX_SCREEN_SHARE))
        return QSize(width, height)

    def _restore_window_geometry(self) -> bool:
        """Reopen at the size and place the panel was closed at.

        Returns False when nothing usable is stored, so the caller falls back
        to the default size. A geometry whose top-left is on no current screen
        is dropped: a window restored onto a monitor that has since been
        unplugged is unreachable.
        """
        match = _GEOMETRY_RE.fullmatch((self.settings.window_geometry or "").strip())
        if not match:
            return False
        width, height, x, y = (int(group) for group in match.groups())
        minimum = self.minimumSize()
        if width < minimum.width() or height < minimum.height():
            return False
        if QGuiApplication.screenAt(QPoint(x, y)) is None:
            return False
        self.setGeometry(x, y, width, height)
        return True

    def _restore_maximized_state(self) -> None:
        """Come back up maximized if that is how the panel was closed.

        Maximized is a window *state* the WxH+X+Y string cannot express — a
        restored geometry alone reopens a screen-sized window that is not
        actually maximized and has nothing to restore down to. Set as a state
        rather than through showMaximized(): the window has not been shown
        yet, and app.run()'s show() then brings it up maximized directly.
        """
        if self.settings.window_maximized:
            self.setWindowState(self.windowState() | Qt.WindowMaximized)

    def _persist_window_geometry(self) -> None:
        maximized = self.isMaximized()
        self.settings.window_maximized = maximized
        # While maximized the current geometry IS the screen; normalGeometry()
        # is the restored-down box, which is what un-maximizing needs to land
        # on after a restart.
        rect = self.normalGeometry() if maximized else self.geometry()
        if rect.isValid():
            self.settings.window_geometry = (
                f"{rect.width()}x{rect.height()}+{rect.x()}+{rect.y()}"
            )
        save_settings(self.settings)

    # ── window chrome ────────────────────────────────────────────────────
    def _apply_window_icon(self) -> None:
        """Taskbar + title-bar icon. Set on the window as well as on the
        QApplication so the two can never disagree — both come from the one
        cached QIcon, built from the logo's mark (see gui/icons.py)."""
        icon = app_icon()
        if icon is not None:
            self.setWindowIcon(icon)

    def _logo_pixmap(self, height: int = 30) -> QPixmap | None:
        """The header logo's mark for the current theme.

        ``logo_mark`` trims the shipped artwork's transparent padding and its
        illegible lettering (see utils/icons) — drawing the raw PNG would put a
        tiny dome in the middle of an empty box.
        """
        path = (
            ICON_PATH_PNG_ON_DARK
            if self.settings.theme_mode == "dark"
            else ICON_PATH_PNG
        )
        if not os.path.exists(path):
            return None
        try:
            import io

            from utils.icons import logo_mark

            buffer = io.BytesIO()
            logo_mark(path, height).save(buffer, format="PNG")
            pixmap = QPixmap()
            pixmap.loadFromData(buffer.getvalue(), "PNG")
            return pixmap if not pixmap.isNull() else None
        except Exception as exc:  # noqa: BLE001 - a missing logo is cosmetic
            log(f"Header logo unavailable: {exc}", level="WARNING")
            return None

    # ── build ────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QWidget()
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(0)
        side.addWidget(self._header())

        # Between the header and the cards, as in the Tk panel. Hidden unless
        # the check finds a release newer than the running version.
        from gui.review_banner import ReviewBanner
        from gui.update_banner import UpdateBanner

        self.update_banner = UpdateBanner(self._t, on_skip=self._on_update_skipped)
        side.addWidget(self.update_banner)
        # Below the update notice, and never at the same time as it — see
        # _maybe_ask_for_a_review. Both are hidden until they have something to
        # say, and a hidden banner takes no room (its spacing is a stylesheet
        # margin, not this layout's).
        self.review_banner = ReviewBanner(self._t, on_decision=self._on_review_decision)
        side.addWidget(self.review_banner)

        self.card_area = QScrollArea()
        self.card_area.setWidgetResizable(True)
        self.card_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # A rebuild (a GUI language change) replaces the cards, so the grid
        # that was arranging the old ones goes with them: a levelling pass it
        # had queued would otherwise land on destroyed cards. While the state
        # lived on the panel a rebuild replaced it and a pending pass simply
        # found the new cards; this keeps that. (Not a reproduced crash — an
        # unguarded rebuild with a pass in flight survives; it is the stale
        # work and the orphaned grid per switch that this removes.)
        previous = getattr(self, "card_grid", None)
        if previous is not None:
            previous.deleteLater()
        self.cards_host = QWidget()
        # Three column groups, exactly as the Tk panel: A = Controls +
        # Display, B = Translation flow, C = Advanced. The arranging is
        # gui/card_grid.py; this only says what goes in each column.
        self.card_grid = CardGrid(self.cards_host, parent=self)
        # Built in this order because the builders set the panel attributes
        # each other's rows read; the columns they land in are below.
        display_card = self._display_card()
        language_card = self._language_card()
        advanced_card = self._advanced_card()
        self.card_grid.add_column(0, self._control_card(), display_card)
        self.card_grid.add_column(1, language_card)
        self.card_grid.add_column(2, advanced_card)

        self.card_area.setWidget(self.cards_host)
        self.card_area.viewport().installEventFilter(self)
        side.addWidget(self.card_area, 1)
        self.sidebar = sidebar
        row.addWidget(sidebar, 1)

        self.log_panel = self._log_panel()
        # Parented first: setVisible on a parentless widget opens a top-level
        # window, which flashed on screen before the panel appeared.
        row.addWidget(self.log_panel, 1)
        self.log_panel.setVisible(not self._log_collapsed)
        self._row_layout = row

        self.setCentralWidget(root)
        self._apply_log_panel_widths()
        self._relayout_columns()
        self._sync_mode_controls()
        self._sync_running_state()

    def _header(self) -> QWidget:
        header = QWidget()
        header.setObjectName("sidebar")
        row = QHBoxLayout(header)
        row.setContentsMargins(20, 12, 14, 8)
        row.setSpacing(8)

        self.logo_label = QLabel()
        pixmap = self._logo_pixmap()
        if pixmap is not None:
            self.logo_label.setPixmap(pixmap)
        row.addWidget(self.logo_label)
        brand = QLabel("MinbarLive")
        brand.setObjectName("hero")
        row.addWidget(brand)
        row.addStretch(1)

        # Same glyphs and order as the Tk header: ⟲ ▦ ⚑ ⚙ then the log toggle.
        for glyph, tip_key, tip_default, slot in (
            ("⟲", "history_title", "Session history", self.open_history),
            ("▦", "batch_file", "File / Batch", self.open_batch),
            ("⚑", "announce_title", "Announcement", self.open_announce),
            ("⚙", "settings_title", "Settings", self.open_settings),
        ):
            button = QPushButton(glyph)
            button.setObjectName("icon")
            button.setFixedSize(40, 40)
            button.setToolTip(self._t(tip_key, tip_default))
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(slot)
            row.addWidget(button)

        self.log_toggle = QPushButton("▶" if self._log_collapsed else "◀")
        self.log_toggle.setObjectName("icon")
        self.log_toggle.setFixedSize(40, 40)
        self.log_toggle.setToolTip(self._t("logs", "Logs"))
        self.log_toggle.setCursor(Qt.PointingHandCursor)
        self.log_toggle.clicked.connect(self._toggle_log_panel)
        row.addWidget(self.log_toggle)
        return header

    # ── card: control centre ─────────────────────────────────────────────
    def _control_card(self) -> Card:
        card = Card("▶", self._t("control_center", "Control centre"))

        self.status_pill = QLabel(self._t("stopped", "Stopped"))
        self.status_pill.setObjectName("pill_stopped")
        self.status_pill.setAlignment(Qt.AlignCenter)
        card.body.addWidget(self.status_pill)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.start_btn = QPushButton("▶  " + self._clean_label("start", "Start"))
        self.start_btn.setObjectName("accent")
        self.start_btn.setProperty("class", "big")
        self.start_btn.setMinimumHeight(52)
        self.start_btn.clicked.connect(self.on_start)
        self.stop_btn = QPushButton("■  " + self._clean_label("stop", "Stop"))
        self.stop_btn.setObjectName("danger")
        self.stop_btn.setMinimumHeight(52)
        self.stop_btn.clicked.connect(self.on_stop)
        self.stop_btn.setEnabled(False)
        buttons.addWidget(self.start_btn)
        buttons.addWidget(self.stop_btn)
        card.body.addLayout(buttons)
        return card

    # ── card: display & audio ────────────────────────────────────────────
    def _display_card(self) -> Card:
        card = Card("▤", self._t("display_routing", "Display & audio"))

        self.monitor_combo = self._combo()
        for i, screen in enumerate(QGuiApplication.screens()):
            g = screen.geometry()
            self.monitor_combo.addItem(f"{i + 1}. {screen.name()} ({g.width()}x{g.height()})")
        self.monitor_combo.setCurrentIndex(
            max(0, min(self.settings.monitor_index, self.monitor_combo.count() - 1))
        )
        self.monitor_combo.currentIndexChanged.connect(self._on_monitor_changed)

        (
            self.device_names,
            self.device_base_names,
            self.device_indices,
            self.device_loopback_flags,
        ) = get_input_devices()
        self.device_combo = self._combo()
        self.device_combo.addItems(self.device_names or ["(no input devices)"])
        # Restore by NAME, not position: indices shift when hardware is plugged
        # in, and loopback entries carry synthetic negative indices. Without
        # this a loopback setup silently captures a real microphone.
        saved = self.settings.input_device_name
        if saved:
            pos = find_input_device_position(saved, self.device_base_names)
            if pos is None and saved in self.device_names:
                pos = self.device_names.index(saved)
            if pos is not None:
                self.device_combo.setCurrentIndex(pos)
        # Connected AFTER restoring, so restoring is not itself a user change.
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)

        top = QHBoxLayout()
        top.setSpacing(12)
        top.addWidget(
            field(
                self._t("subtitle_screen", "Subtitle screen"),
                self.monitor_combo,
                symbol="▣",
            ),
            1,
        )
        top.addWidget(
            field(self._t("input_device", "Input device"), self.device_combo, symbol="◉"),
            1,
        )
        # Roomier than the other cards: moving font size into the expander
        # left this one short, and the four groups (routing, level, sliders,
        # appearance) read better with air between them than packed together.
        card.body.setSpacing(12)
        card.body.addLayout(top)
        hint = self._loopback_hint()
        if hint is not None:
            card.body.addWidget(hint)
        card.body.addSpacing(6)
        card.body.addWidget(self._input_level_row())
        card.body.addSpacing(6)

        # One control per row — the two side-by-side tiles left the sliders too
        # short to aim with. Font size lives in the appearance expander below,
        # next to the colour it applies to.
        card.body.addWidget(self._height_panel())
        card.body.addWidget(self._opacity_panel())
        card.body.addSpacing(4)
        card.body.addWidget(self._typography_expander())
        return card

    def _loopback_hint(self) -> QPushButton | None:
        """Explain the missing "(Loopback)" entries where the platform has none.

        Built only where loopback capture is unavailable (macOS), so the card is
        untouched on Windows and Linux — as in the Tk panel. Without it the
        device list simply has no system-audio entry and no reason given.

        A ``#link`` button rather than the Tk version's muted label: that is the
        Qt tree's existing idiom for an external link, and it makes a clickable
        hint actually look clickable.
        """
        if loopback_supported():
            return None
        button = QPushButton(
            self._t("macos_loopback_hint", "macOS: system audio needs BlackHole ↗")
        )
        button.setObjectName("link")
        button.setFlat(True)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(BLACKHOLE_URL))
        )
        return button

    def _input_level_row(self) -> QWidget:
        """dBFS readout · segmented bar · Test button, as in the Tk card."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self.level_value = QLabel(self._t("input_level_no_signal", "No signal"))
        # Deliberately NOT objectName "muted": the readout recolours per state
        # via its own stylesheet, and an id rule in the app sheet outranks a
        # widget sheet's plain `color` — the text stayed grey either way.
        self.level_value.setStyleSheet(f"color: {current_colors()['muted']};")
        self.level_value.setMinimumWidth(78)
        self.level_bar = AudioLevelBar()
        self.level_test_btn = QPushButton(self._t("input_level_test", "Test mic"))
        self.level_test_btn.setObjectName("compact")
        # Fixed, so the meter beside it gives way instead: with the default
        # policy the row squeezed the button below its text and clipped the
        # label at both ends. Its two captions differ in length, so the width
        # is pinned to the longer one and does not jump on Start/Stop.
        self.level_test_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.level_test_btn.setFixedWidth(
            max(
                self.level_test_btn.fontMetrics().horizontalAdvance(
                    self._t(key, fallback)
                )
                for key, fallback in (
                    ("input_level_test", "Test mic"),
                    ("input_level_stop_test", "Stop"),
                )
            )
            + 28
        )
        self.level_test_btn.clicked.connect(self._toggle_input_level_test)

        row.addWidget(self.level_value)
        row.addWidget(self.level_bar, 1)
        row.addWidget(self.level_test_btn)
        self._level_text_state: tuple[str, str] | None = None
        self._level_button_state: tuple[bool, bool] | None = None
        return holder

    @staticmethod
    def _mini_row(title: str) -> tuple[QFrame, QHBoxLayout, QLabel]:
        """A soft tile holding one labelled control on a single row."""
        frame = QFrame()
        frame.setObjectName("mini")
        box = QHBoxLayout(frame)
        box.setContentsMargins(14, 11, 14, 11)
        box.setSpacing(10)
        label = QLabel(title)
        label.setObjectName("field")
        label.setMinimumWidth(112)
        box.addWidget(label)
        return frame, box, label

    def _slider_panel(
        self, title: str, minimum: int, maximum: int, value: int, on_change
    ) -> tuple[QFrame, Slider, QLabel]:
        frame, box, caption = self._mini_row(title)
        slider = Slider()
        slider.setRange(minimum, maximum)
        slider.setValue(max(minimum, min(maximum, value)))
        slider.valueChanged.connect(on_change)
        readout = QLabel(f"{slider.value()}%")
        readout.setObjectName("value")
        readout.setMinimumWidth(56)
        readout.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        box.addWidget(slider, 1)
        box.addWidget(readout)
        frame.caption = caption
        return frame, slider, readout

    def _height_panel(self) -> QFrame:
        # Range and caption are both provisional: _sync_display_sliders swaps
        # them for the lift's when the mode calls for it.
        frame, self.height_slider, self.height_value = self._slider_panel(
            self._t("height", "Window height"),
            WINDOW_HEIGHT_PERCENT_MIN,
            WINDOW_HEIGHT_PERCENT_MAX,
            self.settings.window_height_percent,
            self._on_height_changed,
        )
        self.height_caption = frame.caption
        self.height_row = frame
        return frame

    def _opacity_panel(self) -> QFrame:
        """Backdrop opacity, moved here from the Settings window.

        It belongs with the other two things that decide how much of the screen
        the overlay takes — height and font size — not two windows away.
        """
        frame, self.opacity_slider, self.opacity_value = self._slider_panel(
            self._t("backdrop_opacity", "Background opacity"),
            BACKDROP_OPACITY_MIN,
            BACKDROP_OPACITY_MAX,
            self.settings.subtitle_backdrop_opacity,
            self._on_opacity_changed,
        )
        self.opacity_caption = frame.caption
        self.opacity_row = frame
        return frame

    # ── subtitle appearance (collapsible) ────────────────────────────────
    def _typography_expander(self) -> Expander:
        """Original-text size and the two colour overrides.

        Collapsed by default, like the Tk expander: these are set-once values,
        not something an operator reaches for mid-session.
        """
        self.typography = Expander(
            self._t("subtitle_appearance", "Subtitle appearance")
        )
        self._color_pick_btns: dict[str, QPushButton] = {}
        self._color_reset_btns: dict[str, QPushButton] = {}

        # Grouped by the line each control affects: the translation's size and
        # colour, then the original's. Hunting for "the other size" three rows
        # away is what the flat order cost.
        self.font_stepper = Stepper(
            lambda: self._step_font(smaller=True),
            lambda: self._step_font(smaller=False),
            self._font_percent_text(),
        )
        self.source_font_stepper = Stepper(
            lambda: self._step_source_font(+_SOURCE_FONT_STEP),
            lambda: self._step_source_font(-_SOURCE_FONT_STEP),
            self._source_font_percent_text(),
        )
        # Opening it makes the left column ~240px taller, which is what the
        # 2-column levelling has to notice (and decline).
        self.typography.toggled.connect(
            lambda _open: self._relayout_columns(force=True)
        )
        self._typography_row(self._t("font", "Font size"), self.font_stepper)
        self._color_row("translation_text_color")
        self.typography.body.addSpacing(_SECTION_GAP)
        self._typography_row(
            self._t("source_text_size", "Original text size"),
            self.source_font_stepper,
        )
        self._color_row("source_text_color")

        self._refresh_typography()
        return self.typography

    def _typography_row(self, caption: str, control: QWidget) -> None:
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(QLabel(caption))
        row.addStretch(1)
        row.addWidget(control)
        self.typography.body.addLayout(row)

    def _color_row(self, attribute: str) -> None:
        pick = QPushButton()
        pick.setMinimumWidth(104)
        pick.setCursor(Qt.PointingHandCursor)
        pick.clicked.connect(lambda _c=False, a=attribute: self._pick_color(a))
        reset = QPushButton(self._t("color_default", "Default"))
        reset.clicked.connect(lambda _c=False, a=attribute: self._reset_color(a))
        # Same height as the steppers directly above and below them.
        for button in (pick, reset):
            button.setFixedHeight(CONTROL_H)
        buttons = QWidget()
        box = QHBoxLayout(buttons)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)
        box.addWidget(pick)
        box.addWidget(reset)
        self._color_pick_btns[attribute] = pick
        self._color_reset_btns[attribute] = reset
        self._typography_row(self._t(attribute, attribute), buttons)

    def _font_percent_text(self) -> str:
        """Subtitle size as a percentage of the shipped default.

        ``font_size_base`` is a DIVISOR — a bigger base is a SMALLER font — so
        showing it raw made the number count down when "+" was pressed. The
        percentage moves with the text, like the original-size row below it.
        """
        try:
            percent = round(
                _FONT_SIZE_BASE_DEFAULT / float(self.settings.font_size_base) * 100
            )
        except (TypeError, ValueError, ZeroDivisionError):
            percent = 100
        return f"{percent}%"

    def _source_font_percent_text(self) -> str:
        """Original size as a percentage OF the translation size.

        Both values are divisors, so the ratio is translation/source. Measuring
        against the translation stays truthful when the subtitle font is
        resized; against a fixed constant it would drift.
        """
        base = getattr(
            self.settings, "source_font_size_base", DEFAULT_SOURCE_FONT_SIZE_BASE
        )
        try:
            percent = round(float(self.settings.font_size_base) / float(base) * 100)
        except (TypeError, ValueError, ZeroDivisionError):
            percent = 70
        return f"{percent}%"

    def _refresh_typography(self) -> None:
        """Repaint both size steppers and both colour buttons from the settings.

        The translation stepper is normally kept in step by ``_step_font``,
        which owns the only other way it changes. A layout switch replaces all
        four values at once (see ``_swap_layout_appearance``), so it has to be
        repainted from the stored value like everything else here.
        """
        self.font_stepper.set_value_text(self._font_percent_text())
        self.source_font_stepper.set_value_text(self._source_font_percent_text())
        colors = current_colors()
        for attribute, pick in self._color_pick_btns.items():
            color = getattr(self.settings, attribute, "") or ""
            if color:
                pick.setText(color.upper())
                # The button carries the operator's own colour, so it is styled
                # per widget rather than by the app stylesheet.
                pick.setStyleSheet(
                    f"background-color: {color}; color: {_readable_on(color)};"
                    f" border: 1px solid {colors['border']}; font-weight: 600;"
                )
            else:
                pick.setText(self._t("color_choose", "Choose…"))
                pick.setStyleSheet("")
            reset = self._color_reset_btns.get(attribute)
            if reset is not None:
                # Nothing to reset while the theme colour is already in use.
                reset.setEnabled(bool(color))

    def _step_source_font(self, delta: float) -> None:
        """Step the divisor. Positive delta = larger divisor = smaller text."""
        current = getattr(
            self.settings, "source_font_size_base", DEFAULT_SOURCE_FONT_SIZE_BASE
        )
        new_base = max(
            SOURCE_FONT_SIZE_BASE_MIN, min(SOURCE_FONT_SIZE_BASE_MAX, current + delta)
        )
        if new_base == current:
            return
        self.settings.source_font_size_base = new_base
        if self.subtitle_window:
            self.subtitle_window.set_source_font_size_base(new_base)
        self._refresh_typography()
        save_settings(self.settings)

    def _pick_color(self, attribute: str) -> None:
        current = getattr(self.settings, attribute, "") or ""
        chosen = QColorDialog.getColor(
            QColor(current) if current else QColor("#ffffff"),
            self,
            self._t("subtitle_appearance", "Subtitle appearance"),
        )
        if not chosen.isValid():
            return
        setattr(self.settings, attribute, chosen.name().upper())
        self._apply_typography_to_window()
        self._refresh_typography()
        save_settings(self.settings)
        log(f"Subtitle {attribute} set to {chosen.name()}", level="INFO")

    def _reset_color(self, attribute: str) -> None:
        if not getattr(self.settings, attribute, ""):
            return
        setattr(self.settings, attribute, "")
        self._apply_typography_to_window()
        self._refresh_typography()
        save_settings(self.settings)
        log(f"Subtitle {attribute} reset to theme default", level="INFO")

    def _apply_typography_to_window(self) -> None:
        if not self.subtitle_window:
            return
        # The translation base too: the steppers drive it through
        # increase/decrease_font, but a layout switch replaces it outright.
        self.subtitle_window.set_font_size_base(self.settings.font_size_base)
        self.subtitle_window.set_source_font_size_base(
            self.settings.source_font_size_base
        )
        self.subtitle_window.set_translation_text_color(
            self.settings.translation_text_color
        )
        self.subtitle_window.set_source_text_color(self.settings.source_text_color)

    # ── card: translation flow ───────────────────────────────────────────
    def _language_card(self) -> Card:
        card = Card("⇄", self._t("translation_flow", "Translation flow"))

        # Canonical (English) names are stored; the dropdown shows the endonym.
        self._source_names = [name for name, _ in SOURCE_LANGUAGES]
        self.source_combo = self._combo()
        self._refresh_source_combo()
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        self.target_combo = self._combo()
        self.target_combo.addItems(TARGET_LANGUAGE_DISPLAY_NAMES)
        self.target_combo.setCurrentText(
            language_display_name(self.settings.target_language)
        )
        self.target_combo.currentIndexChanged.connect(self._on_target_changed)

        pair = QHBoxLayout()
        pair.setSpacing(6)
        pair.addWidget(
            field(self._t("source", "Spoken language"), self.source_combo, symbol="⌁"), 1
        )
        swap = QPushButton("⇄")
        swap.setObjectName("icon")
        swap.setFixedSize(_HELP_BTN_PX, _HELP_BTN_PX)
        swap.setCursor(Qt.PointingHandCursor)
        swap.clicked.connect(self._on_swap_languages)
        # Bottom-aligned rather than pushed down by a hand-measured spacer:
        # each side of the pair is a caption above a dropdown, so the row's
        # bottom edge IS the dropdown's, and a fixed offset only lines up while
        # the caption happens to be the height it was measured at.
        pair.addWidget(swap, 0, Qt.AlignBottom)
        pair.addWidget(
            field(self._t("target", "Subtitle language"), self.target_combo, symbol="→"),
            1,
        )
        card.body.addLayout(pair)

        # Processing strategy — the master switch, above the Subtitles picker
        # it feeds (Realtime is streaming-only).
        strat_head = QHBoxLayout()
        strat_label = QLabel("⇶  " + self._t("processing_strategy", "Processing"))
        strat_label.setObjectName("field")
        self.strategy_hint = QLabel(
            self._t("hint_stop_to_change", "⚠ Stop to change")
        )
        self.strategy_hint.setObjectName("warning_text")
        self.strategy_hint.setVisible(False)
        strat_head.addWidget(strat_label)
        strat_head.addStretch(1)
        strat_head.addWidget(self.strategy_hint)
        card.body.addLayout(strat_head)

        self.strategy_combo = self._combo()
        self.strategy_combo.addItems(
            [self._t(f"strategy_{s}", s.title()) for s in STRATEGY_IDS]
        )
        self.strategy_combo.setCurrentIndex(current_strategy_index(self.settings))
        self.strategy_combo.currentIndexChanged.connect(self._on_strategy_changed)
        card.body.addWidget(self._with_help(self.strategy_combo, "strategy"))

        # Subtitles mode, its "?" and the mode-specific control all on ONE row,
        # as in the Tk card — the stepper is not a second column there.
        self.mode_combo = self._combo()
        self._refresh_mode_combo()
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_caption = QLabel("≋  " + self._t("subtitles", "Subtitles"))
        mode_caption.setObjectName("field")
        card.body.addWidget(mode_caption)

        mode_row = QHBoxLayout()
        mode_row.setSpacing(8)
        mode_row.addWidget(self._with_help(self.mode_combo, "subtitle"), 1)

        self.speed_stepper = Stepper(
            lambda: self._step_speed(-0.25),
            lambda: self._step_speed(+0.25),
            f"{self.settings.scroll_speed:.1f}x",
        )
        self.mode_controls = QWidget()
        mc = QHBoxLayout(self.mode_controls)
        mc.setContentsMargins(0, 0, 0, 0)
        mc.setSpacing(8)
        mc.addWidget(self.speed_stepper)
        mode_row.addWidget(self.mode_controls)
        card.body.addLayout(mode_row)

        # Display toggles, in the Tk order: catch-up / live transcript (one at
        # a time, by mode), then "show original text", then the 3-way
        # visibility selector.
        self.catchup_check = QCheckBox(
            self._t("adaptive_subtitle_catchup", "Speed up when behind")
        )
        self.catchup_check.setChecked(self.settings.adaptive_subtitle_catchup)
        self.catchup_check.toggled.connect(self._on_catchup_changed)
        self.interim_check = QCheckBox(
            self._t("show_interim_transcript", "Show live transcript")
        )
        self.interim_check.setChecked(self.settings.show_interim_transcript)
        self.interim_check.toggled.connect(self._on_interim_changed)
        # Transparent belongs with the other display toggles, not off to the
        # right of the mode dropdown.
        self.transparent_check = QCheckBox(self._t("transparent", "Transparent"))
        self.transparent_check.setChecked(self.settings.transparent_static)
        self.transparent_check.toggled.connect(self._on_transparent_changed)
        self.bilingual_check = QCheckBox(
            self._t("bilingual_mode", "Show original text")
        )
        self.bilingual_check.setChecked(self.settings.bilingual_mode)
        self.bilingual_check.toggled.connect(self._on_bilingual_toggled)
        # A selector, not a checkbox: the off state is a real alternative
        # layout rather than "feature off", and a checkbox called "Side by
        # side" leaves the other one unnamed. Same reasoning as the two 3-way
        # selectors in the decisions table.
        #
        # Only meaningful with an original to put in the second column, so it
        # is hidden outright without one rather than sitting there dead. It
        # rides on the same row as the toggle it depends on — which also means
        # hiding it cannot change the card's height, so nothing has to be
        # re-levelled the way _sync_mode_controls has to.
        self.layout_segment = SegmentedControl(
            self._subtitle_layout_labels(),
            1 if self.settings.subtitle_side_by_side else 0,
            compact=True,
        )
        self.layout_segment.setVisible(self.settings.bilingual_mode)
        self.layout_segment.changed.connect(
            lambda index: self._on_side_by_side_toggled(index == 1)
        )
        bilingual_row = QHBoxLayout()
        bilingual_row.setContentsMargins(0, 0, 0, 0)
        bilingual_row.setSpacing(16)
        bilingual_row.addWidget(self.bilingual_check)
        bilingual_row.addWidget(self.layout_segment)
        bilingual_row.addStretch(1)
        card.body.addWidget(self.catchup_check)
        card.body.addWidget(self.interim_check)
        card.body.addWidget(self.transparent_check)
        card.body.addLayout(bilingual_row)

        hide_caption = QLabel(self._t("hide_subtitle_label", "Hide subtitle window"))
        hide_caption.setObjectName("field")
        self.hide_segment = SegmentedControl(
            [self._t(*_HIDE_MODE_KEYS[m]) for m in SUBTITLE_HIDE_MODES],
            SUBTITLE_HIDE_MODES.index(self.settings.subtitle_hide_mode)
            if self.settings.subtitle_hide_mode in SUBTITLE_HIDE_MODES
            else 0,
        )
        self.hide_segment.changed.connect(self._on_hide_mode_changed)
        card.body.addWidget(hide_caption)
        card.body.addWidget(self.hide_segment)
        return card

    def _with_help(self, widget: QWidget, kind: str) -> QWidget:
        """``widget`` with a "?" button beside it, as the Tk dropdowns have."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        row.addWidget(widget, 1)
        help_btn = QPushButton("?")
        help_btn.setObjectName("icon")
        # Same edge length as the −/+ steppers it shares a row with; at 38 px it
        # read as a different class of control.
        help_btn.setFixedSize(_HELP_BTN_PX, _HELP_BTN_PX)
        help_btn.setCursor(Qt.PointingHandCursor)
        help_btn.clicked.connect(lambda: self._show_help(kind))
        row.addWidget(help_btn)
        return holder

    def _show_help(self, kind: str) -> None:
        """Explain each entry of a dropdown, as the Tk "?" popups do."""
        if kind == "strategy":
            title = self._t("processing_strategy", "Processing")
            keys = [f"strategy_hint_{s}" for s in STRATEGY_IDS]
            names = [self._t(f"strategy_{s}", s) for s in STRATEGY_IDS]
        else:
            title = self._t("subtitles", "Subtitles")
            modes = subtitle_mode_choices(self.settings)
            keys = [f"subtitle_hint_{m}" for m in modes]
            names = [self._t(f"subtitle_mode_{m}", m) for m in modes]
        body = "\n\n".join(
            f"{name}\n{self._t(key, '')}" for name, key in zip(names, keys, strict=False)
        )
        self._info(title, body)

    def _info(self, title: str, body: str) -> None:
        """A themed, silent information dialog.

        ``QMessageBox.information`` draws the platform's own dialog with an
        English "OK" and plays the system's "asterisk" sound — the icon is what
        triggers it. Reading a dropdown's help is not an alert.
        """
        show_message(self, title, body, kind="info", translate=self._t)

    # ── card: advanced ───────────────────────────────────────────────────
    def _advanced_card(self) -> Card:
        # Collapsible while it shares a column, and collapsed there: the whole
        # card is set-up-once configuration, and open it is most of the scroll
        # length. In three columns it is pinned open and the "Other settings"
        # group inside it becomes the collapsible one instead — see
        # _sync_advanced_for_columns.
        card = Card(
            "⚙",
            self._t("advanced_settings", "Advanced"),
            collapsible=True,
            expanded=False,
        )
        self.advanced_card = card
        card.toggled.connect(lambda _open: self._relayout_columns(force=True))

        # Transcription first, then translation — the Tk order.
        card.body.addWidget(self._section(self._t("section_transcription", "Transcription")))
        self.transcription_provider_combo = self._combo()
        self.transcription_provider_combo.currentIndexChanged.connect(
            self._on_transcription_provider_changed
        )
        self.transcription_model_combo = self._combo()
        self.transcription_model_combo.currentIndexChanged.connect(
            self._on_transcription_model_changed
        )
        # "Automatic", not "Default": ticking it overrides a manual pick, and a
        # box labelled "Default" reads as describing the dropdown instead.
        self.use_default_transcription = QCheckBox(
            self._t("use_recommended", "Automatic")
        )
        self.use_default_transcription.setChecked(
            self.settings.use_default_transcription_model
        )
        self.use_default_transcription.toggled.connect(
            self._on_use_default_transcription
        )
        card.body.addLayout(
            self._engine_block(
                self.transcription_provider_combo,
                self.transcription_model_combo,
                self.use_default_transcription,
            )
        )

        card.body.addSpacing(_SECTION_GAP)
        card.body.addWidget(self._section(self._t("section_translation", "Translation")))
        self.provider_combo = self._combo()
        self.provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self.model_combo = self._combo()
        self.model_combo.currentIndexChanged.connect(self._on_model_changed)
        self.use_default_translation = QCheckBox(
            self._t("use_recommended", "Automatic")
        )
        self.use_default_translation.setChecked(
            self.settings.use_default_translation_model
        )
        self.use_default_translation.toggled.connect(self._on_use_default_translation)
        card.body.addLayout(
            self._engine_block(
                self.provider_combo, self.model_combo, self.use_default_translation
            )
        )

        card.body.addSpacing(_SECTION_GAP)
        # Built as an expander but plain (a section heading, controls flush)
        # until the panel is wide enough for the card to stop collapsing.
        self.other_settings = Expander(
            self._t("other_settings", "Other settings"),
            expanded=True,
            collapsible=False,
        )
        body = self.other_settings.body
        card.body.addWidget(self.other_settings)

        # Directly under the heading, above the checkboxes: it is a window
        # behaviour, not one of the pipeline toggles below it.
        aot_caption = QLabel(self._t("window_on_top_label", "Window always on top"))
        aot_caption.setObjectName("field")
        self.aot_segment = SegmentedControl(
            [self._t(*_AOT_MODE_KEYS[m]) for m in ALWAYS_ON_TOP_MODES],
            ALWAYS_ON_TOP_MODES.index(self.settings.always_on_top_mode)
            if self.settings.always_on_top_mode in ALWAYS_ON_TOP_MODES
            else 0,
        )
        self.aot_segment.changed.connect(self._on_aot_changed)
        body.addWidget(aot_caption)
        body.addWidget(self.aot_segment)

        self._other_checks: dict[str, QCheckBox] = {}
        for attribute, key, fallback in (
            ("show_footer", "show_footer", "Show disclaimer"),
            ("auto_stop_inactivity", "auto_stop_inactivity", "Stop when idle"),
            ("noise_filter", "noise_filter", "Noise filter"),
            ("auto_cleanup_logs", "auto_cleanup_logs", "Clean up logs"),
            ("auto_cleanup_content", "auto_cleanup_content", "Clean up recordings"),
            ("auto_start", "auto_start_on_launch", "Start on launch"),
        ):
            box = QCheckBox(self._t(key, fallback))
            box.setChecked(bool(getattr(self.settings, attribute)))
            box.toggled.connect(
                lambda checked, a=attribute: self._on_simple_setting(a, checked)
            )
            body.addWidget(box)
            self._other_checks[attribute] = box

        self._refresh_provider_combos()
        return card

    def _section(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    @staticmethod
    def _engine_block(
        provider: QComboBox, model: QComboBox, check: QCheckBox
    ) -> QGridLayout:
        """Provider above model, with "Automatic" beside the model.

        A grid rather than two rows so both combos end at the same edge: laid
        out as rows the provider ran the card's full width and passed behind the
        checkbox, which reads as a misaligned overhang.
        """
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)
        grid.addWidget(provider, 0, 0)
        grid.addWidget(model, 1, 0)
        grid.addWidget(check, 1, 1)
        grid.setColumnStretch(0, 1)
        return grid

    # ── log panel ────────────────────────────────────────────────────────
    def _log_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(260)
        box = QVBoxLayout(panel)
        # Left margin matches the right: the row layout has no spacing, so at 0
        # the log box sat directly against the card area's scroll bar with
        # nothing between them.
        box.setContentsMargins(18, 16, 18, 18)
        box.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("▤  " + self._t("logs", "Logs"))
        title.setObjectName("hero")
        self.log_status = QLabel(self._t("stopped", "Stopped"))
        self.log_status.setObjectName("pill_stopped")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.log_status)
        box.addLayout(head)

        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("log")
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumBlockCount(2000)
        box.addWidget(self.log_text, 1)
        return panel

    def _apply_log_panel_widths(self) -> None:
        """Let the log share the window instead of being bolted onto it.

        Open, the sidebar is pinned to ``_SIDEBAR_W_WITH_LOG`` and the log takes
        whatever is left — the Tk arrangement, and the reason the card grid
        drops to a single column while the log is up. Closed, the sidebar takes
        the whole width back.
        """
        if self._log_collapsed:
            self.sidebar.setMinimumWidth(0)
            self.sidebar.setMaximumWidth(16777215)  # QWIDGETSIZE_MAX
            self._row_layout.setStretch(0, 1)
        else:
            self.sidebar.setFixedWidth(_SIDEBAR_W_WITH_LOG)
            self._row_layout.setStretch(0, 0)
        self._apply_minimum_size()

    def _cards_minimum_width(self) -> int:
        """Narrowest the card area may get without clipping a card.

        The area scrolls vertically only, so anything narrower than the cards'
        own minimum is simply cut off — there is no horizontal bar to reach it,
        and the vertical one then sits ON the clipped edge. That is the whole
        of the reported bug: at the old 420 px floor the cards needed 449, so
        Stop and both dropdowns in the Display card ran under the scroll bar
        and off the window.

        Measured, not a constant: the widest row is a translated label beside a
        segmented control ("Show original text" + Combined|Side by side), and
        that width differs per GUI language.
        """
        return (
            self.card_grid.minimum_width()
            + self.card_area.verticalScrollBar().sizeHint().width()
            + 2 * self.card_area.frameWidth()
        )

    def _apply_minimum_size(self) -> None:
        """Pin the window's floor to what the current arrangement needs.

        Re-applied whenever the log panel opens or closes, because that decides
        which of the two arrangements has to fit: the cards alone, or the
        pinned-width sidebar beside a log panel that has a minimum of its own.

        Nothing is measured before the first show. A card's padding, font and
        border all come from the application stylesheet, which Qt applies when
        the widget is polished — so a pre-show ``minimumSizeHint`` describes
        unstyled widgets and came out at 50 px against the real 449. Until then
        the floor is the height alone, and ``showEvent`` re-runs this.
        """
        if not self.isVisible():
            self.setMinimumSize(QSize(0, _MIN_WINDOW_H))
            return
        if self._log_collapsed:
            width = self._cards_minimum_width()
        else:
            width = _SIDEBAR_W_WITH_LOG + _LOG_PANEL_MIN_W
        self.setMinimumSize(QSize(width, _MIN_WINDOW_H))

    def showEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # First point at which the cards have been styled, and so the first
        # point at which their minimum width is a real number.
        self._apply_minimum_size()
        # …and the first point at which there is an HWND to theme. The panel is
        # built AFTER apply_theme ran, so the sweep there never saw it.
        set_titlebar_dark(self, self.settings.theme_mode != "light")

    def _toggle_log_panel(self) -> None:
        self._log_collapsed = not self._log_collapsed
        self.log_panel.setVisible(not self._log_collapsed)
        self.log_toggle.setText("▶" if self._log_collapsed else "◀")
        self.settings.log_panel_collapsed = self._log_collapsed
        save_settings(self.settings)
        # Read BEFORE _apply_log_panel_widths(). That call raises the window's
        # minimum width to fit the log, and setMinimumSize GROWS a window that
        # is under the new floor there and then — so by the time it returns,
        # the width the log has to give back is already gone. (Measured: a
        # 702 px window is 840 px on the next line, without any resize() here.)
        width_before = self.width()
        self._apply_log_panel_widths()
        if not self._log_collapsed:
            # The log opens INSIDE the current window; it only widens one that
            # cannot hold both, which would otherwise give the log a column a
            # character wide. Never past the screen it is on.
            if not self.isMaximized():
                screen = self.screen() or QGuiApplication.primaryScreen()
                available = screen.availableGeometry().width() if screen else 0
                wanted = _SIDEBAR_W_WITH_LOG + _LOG_PANEL_MIN_W
                if available:
                    wanted = min(wanted, available)
                if self.width() < wanted:
                    self.resize(wanted, self.height())
            # What it cost, and what it grew to — measured here rather than
            # assumed, since either the raised minimum or the resize above may
            # have been the one that moved it. A window that was wide enough
            # already records (w, w), which makes the restore below a no-op
            # without needing a branch of its own.
            self._log_widen = (width_before, self.width())
        elif self._log_widen is not None:
            # Closing hands back exactly what opening took, or every peek at
            # the log leaves the window wider than the user left it — for good,
            # since the geometry is stored on exit. _apply_log_panel_widths()
            # has already dropped the minimum back to the cards' own, so the
            # log's floor cannot clamp this.
            before, widened_to = self._log_widen
            if self.width() == widened_to:
                # Exactly where opening left it, so the only thing that has
                # touched the width since is us. ANY other value means the user
                # resized the window while the log was up — wider OR narrower —
                # and that size is a deliberate choice to leave alone. A "no
                # wider than we made it" test gets the narrower case backwards
                # and snaps a window the user shrank back up to its old size.
                self.resize(before, self.height())
        self._relayout_columns()

    def _drain_logs(self) -> None:
        appended = False
        while not log_queue.empty():
            try:
                self.log_text.appendPlainText(log_queue.get_nowait())
                appended = True
            except Exception:  # noqa: BLE001 - a log line must never break the UI
                break
        if appended:
            bar = self.log_text.verticalScrollBar()
            bar.setValue(bar.maximum())

    # ── responsive card grid ─────────────────────────────────────────────
    def _available_width(self) -> int:
        """Width the card grid has to work with, as if the scroll bar were
        always showing.

        The viewport is authoritative once the window is on screen. Before
        that it still reports its default, so fall back to the window's own
        width minus whatever the log panel will claim — otherwise the first
        layout is computed against a placeholder size.

        The bar is reserved whether or not it is up, and that is not a fudge —
        it breaks a feedback loop. The viewport's width depends on whether the
        vertical scroll bar is showing; the column count decides how tall the
        content is; and the content height decides whether that bar shows. Feed
        the live viewport width back into the column decision and the loop
        closes on itself at any window width within the bar's own width of a
        threshold: three columns pin the Advanced card open, the taller content
        summons the bar, the bar takes the viewport back under _COL3_MIN_W, two
        columns let the content shrink, the bar goes, and it starts again.
        Measured before this: every width from 1030 to 1039 oscillated 3/2/3/2
        without ever settling.

        Reserving it unconditionally costs the layout the bar's width on the
        rare screenful that would not have needed one, and matches
        _cards_minimum_width(), which already assumes the bar is there.
        """
        viewport = self.card_area.viewport().width()
        if self.isVisible() and viewport > 1:
            bar = self.card_area.verticalScrollBar()
            if not bar.isVisible():
                viewport -= bar.sizeHint().width()
            return max(0, viewport)
        return max(
            0, self.width() - (0 if self._log_collapsed else _SIDEBAR_W_WITH_LOG)
        )

    def eventFilter(self, watched, event):  # noqa: N802 - Qt API
        """Reflow when the SCROLL VIEWPORT resizes, not just the window.

        The window's own resizeEvent fires before the layout has handed the
        viewport its new width, so a window-only trigger leaves the grid one
        step behind — it stuck at one column on a 1180 px window.
        """
        if watched is self.card_area.viewport() and event.type() == QEvent.Resize:
            self._relayout_columns()
        return super().eventFilter(watched, event)

    def _relayout_columns(self, force: bool = False) -> None:
        """Re-arrange the cards for the width now available."""
        cols = self.card_grid.relayout(
            self._available_width(), not self._log_collapsed, force
        )
        # Only when the count actually changed, and last, so the re-entrant
        # _relayout_columns its toggle triggers finds a consistent grid.
        if cols is not None:
            self._sync_advanced_for_columns(cols)

    def _level_two_column_bottoms(self) -> None:
        self.card_grid.level_two_column_bottoms()

    def _sync_advanced_for_columns(self, cols: int) -> None:
        """Move the collapse one level in or out with the column count.

        In the 3-column layout column C holds nothing else, so a collapsed
        Advanced is an empty column: the card is pinned open and the "Other
        settings" group inside it becomes the collapsible one — closed to
        start with, since it is the longest and least-touched group. Narrower,
        the card itself collapses (open, it is most of the scroll length) and
        the group is a plain section again. A manual toggle of either stands
        until the column count changes.
        """
        card = getattr(self, "advanced_card", None)
        if card is None:
            return
        wide = cols >= 3
        card.set_collapsible(not wide)
        if not wide:
            card.set_expanded(False)
        self.other_settings.set_collapsible(wide)
        if wide:
            self.other_settings.set_expanded(False)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._relayout_columns()

    # ── combo helper ─────────────────────────────────────────────────────
    @staticmethod
    def _combo() -> Dropdown:
        combo = Dropdown()
        # Ignored horizontally so a card can be squeezed below the combo's
        # natural width instead of pinning the window open (report 8).
        combo.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        return combo

    # ── dropdown refreshers ──────────────────────────────────────────────
    def _refresh_source_combo(self) -> None:
        """Real-time transcription cannot auto-detect the source language, so
        "Automatic" is removed from the picker while streaming is selected."""
        streaming = self.settings.pipeline_mode == PIPELINE_MODE_STREAMING
        names = [
            name
            for name, code in SOURCE_LANGUAGES
            if not (streaming and code is None)
        ]
        current = self.settings.source_language
        if current not in names:
            current = names[0]
            self.settings.source_language = current
        # Signals stay connected: blocking them across the repopulate is enough,
        # and disconnect/reconnect warns loudly the first time round (nothing is
        # connected yet when the card builds).
        blocked = self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItems([language_display_name(n) for n in names])
        self.source_combo.setCurrentText(language_display_name(current))
        self.source_combo.blockSignals(blocked)

    def _refresh_mode_combo(self) -> None:
        modes = subtitle_mode_choices(self.settings)
        blocked = self.mode_combo.blockSignals(True)
        self.mode_combo.clear()
        for mode in modes:
            self.mode_combo.addItem(self._t(f"subtitle_mode_{mode}", mode), mode)
        index = self.mode_combo.findData(effective_subtitle_mode(self.settings))
        self.mode_combo.setCurrentIndex(max(0, index))
        self.mode_combo.blockSignals(blocked)

    def _refresh_provider_combos(self) -> None:
        """Repopulate both provider dropdowns for the current strategy.

        The transcription list follows the Processing Strategy: real-time
        offers the streaming engines, chunk/semantic the segmented ones.
        """
        streaming = self.settings.pipeline_mode == PIPELINE_MODE_STREAMING
        if streaming:
            choices = [
                (name, pid)
                for name, pid in TRANSCRIPTION_PROVIDER_CHOICES
                if pid in STREAMING_TRANSCRIPTION_PROVIDERS
            ]
        else:
            choices = [
                (name, pid)
                for name, pid in TRANSCRIPTION_PROVIDER_CHOICES
                if pid not in STREAMING_TRANSCRIPTION_PROVIDERS
            ]
        self._fill(
            self.transcription_provider_combo,
            visible_provider_choices(choices, self._running),
            self.settings.transcription_provider,
        )
        self._fill(
            self.provider_combo,
            visible_provider_choices(list(PROVIDER_CHOICES), self._running),
            self.settings.ai_provider,
        )
        self._refresh_model_combos()
        self._sync_default_model_states()

    def _refresh_model_combos(self) -> None:
        self._fill(
            self.transcription_model_combo,
            get_model_choices(self.settings.transcription_provider, "transcription"),
            self.settings.transcription_model,
            get_default_model(self.settings.transcription_provider, "transcription"),
        )
        self._fill(
            self.model_combo,
            get_model_choices(self.settings.ai_provider, "translation"),
            self.settings.translation_model,
            get_default_model(self.settings.ai_provider, "translation"),
        )

    @staticmethod
    def _fill(
        combo: QComboBox,
        choices: list[tuple[str, str]],
        current: str,
        fallback: str | None = None,
    ) -> None:
        blocked = combo.blockSignals(True)
        combo.clear()
        for name, value in choices:
            combo.addItem(name, value)
        index = combo.findData(current)
        if index < 0 and fallback is not None:
            index = combo.findData(fallback)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(blocked)

    def _sync_default_model_states(self) -> None:
        """A model dropdown is disabled while its "Automatic" box is ticked —
        that is what the tick means."""
        # Provider choice is locked with the model: "Default" means the shipped
        # engine AND its shipped model, so leaving the engine changeable let a
        # ticked box sit above a non-default provider.
        transcription_free = not self.use_default_transcription.isChecked()
        translation_free = not self.use_default_translation.isChecked()
        self.transcription_model_combo.setEnabled(transcription_free)
        self.transcription_provider_combo.setEnabled(transcription_free)
        self.model_combo.setEnabled(translation_free)
        self.provider_combo.setEnabled(translation_free)

    def _sync_mode_controls(self) -> None:
        """Show only the control the current subtitle mode actually uses."""
        mode = self._current_mode()
        self.speed_stepper.setVisible(mode == "continuous")
        self.transparent_check.setVisible(mode == "static")
        self.mode_controls.setVisible(mode == "continuous")
        self.catchup_check.setVisible(mode == "continuous")
        self.interim_check.setVisible(mode == "realtime")
        self._sync_display_sliders()
        # Showing or hiding a row changes the translation card's height, and
        # with it what the two columns need to end level.
        self._level_two_column_bottoms()

    def _lift_mode(self) -> bool:
        """Whether the height slider is currently a LIFT rather than a height.

        Transparent static only: there the overlay has no backdrop of its own,
        so it takes the whole monitor and the slider moves the subtitles and
        the footer up the screen together instead of resizing a band.
        """
        return self._current_mode() == "static" and self.transparent_check.isChecked()

    def _sync_display_sliders(self) -> None:
        """Point the two Display sliders at what the current mode gives them.

        **Height** never greys out — it means something in every mode — but it
        means two different things, so its range, its value, its readout and its
        caption all swap with ``_lift_mode``. The two meanings have a stored
        field each (``window_height_percent`` and ``static_lift_percent``), so
        toggling Transparent hands the slider the value that meaning was last
        left at rather than the other one's — see WINDOW_HEIGHT_PERCENT_* in
        utils/settings for what one shared field cost.

        The swap is made with the slider's signals blocked, and that is
        load-bearing rather than tidy: ``setRange`` clamps the current value and
        ``setValue`` moves it, and both emit ``valueChanged`` — so merely
        switching modes would write the mode you just LEFT the value belonging
        to the one you arrived in.

        **Backdrop opacity** stays live in every mode. It used to grey out
        under Transparent, on the grounds that the toggle sets the window
        backdrop to fully transparent and leaves nothing to apply it to — but
        the mode still paints a card behind each line (``_ribbon_rects``), and
        that card is the only thing between the text and the video. So the
        slider keeps its job and sets the card's opacity instead; only the
        tooltip says which backdrop it is reaching.
        """
        lift = self._lift_mode()
        slider = self.height_slider
        blocked = slider.blockSignals(True)
        if lift:
            slider.setRange(STATIC_LIFT_PERCENT_MIN, STATIC_LIFT_PERCENT_MAX)
            slider.setValue(self.settings.static_lift_percent)
        else:
            slider.setRange(WINDOW_HEIGHT_PERCENT_MIN, WINDOW_HEIGHT_PERCENT_MAX)
            slider.setValue(self.settings.window_height_percent)
        slider.blockSignals(blocked)
        self.height_value.setText(f"{slider.value()}%")
        self.height_caption.setText(
            self._t("height_offset", "Distance from bottom:")
            if lift
            else self._t("height", "Height:")
        )
        self.height_row.setToolTip(
            self._t(
                "height_offset_hint",
                "Transparent mode uses the whole screen, so this moves the "
                "subtitles and the footer up together.",
            )
            if lift
            else ""
        )

        self.opacity_row.setToolTip(
            self._t(
                "opacity_transparent_hint",
                "Transparent mode has no background of its own, so this sets "
                "how dark the box behind each line is.",
            )
            if lift
            else ""
        )

    def _sync_running_state(self) -> None:
        # Captured BEFORE the buttons change: Qt moves focus out of a widget
        # during setEnabled(False), so by the time this returns the focus
        # widget is already the successor.
        focused = QApplication.focusWidget()
        # Both buttons are inert while a Start is in flight: Start would queue a
        # second session and there is nothing to stop until the pipeline is up.
        self.start_btn.setEnabled(not self._running and not self._starting)
        # ``not self._starting`` matters only for a live restart, which is the
        # one case where a start is in flight while the session counts as
        # running — stopping halfway through reopening it races the restart.
        self.stop_btn.setEnabled(
            self._running and not self._stopping and not self._starting
        )
        self._rehome_button_focus(focused)
        if self._starting:
            text = self._clean_label("connecting", "Connecting…")
            pill = "pill_connecting"
        elif self._running:
            text, pill = self._t("running", "Running"), "pill_running"
        else:
            text, pill = self._t("stopped", "Stopped"), "pill_stopped"
        for label in (self.status_pill, self.log_status):
            label.setText(text)
            label.setObjectName(pill)
            label.style().unpolish(label)
            label.style().polish(label)
        # Strategy and engine cannot change mid-run: the pipeline reads them
        # once at start.
        self.strategy_hint.setVisible(self._running)
        self.strategy_combo.setEnabled(not self._running)

    def _rehome_button_focus(self, focused) -> None:
        """Keep the focus ring off unrelated controls when Start/Stop is
        disabled under it.

        Disabling the button that was just clicked hands focus to the NEXT
        widget in the tab chain — the subtitle-screen dropdown, which then wore
        the accent ring after every Start, as if the operator had selected it.
        Focus goes to the button that is now the live action, or nowhere at all
        while a start is in flight and neither is.
        """
        if focused not in (self.start_btn, self.stop_btn) or focused.isEnabled():
            return
        heir = self.stop_btn if focused is self.start_btn else self.start_btn
        if heir.isEnabled():
            heir.setFocus(Qt.OtherFocusReason)
            return
        current = QApplication.focusWidget()
        if current is not None:
            current.clearFocus()

    # ── handlers: display ────────────────────────────────────────────────
    def _on_monitor_changed(self, index: int) -> None:
        self.settings.monitor_index = index
        save_settings(self.settings)
        if self.subtitle_window:
            self.subtitle_window.set_monitor(index)

    def _on_height_changed(self, value: int) -> None:
        """One slider, two settings — whichever meaning is in force right now.

        The mode decides where the number goes, so the other meaning keeps the
        value the operator last left it at and Transparent can be toggled back
        and forth without either drifting.
        """
        if self._lift_mode():
            self.settings.static_lift_percent = value
        else:
            self.settings.window_height_percent = value
        self.height_value.setText(f"{value}%")
        save_settings(self.settings)
        if self.subtitle_window:
            if self._lift_mode():
                self.subtitle_window.set_static_lift_percent(value)
            else:
                self.subtitle_window.set_window_height_percent(value)

    def _on_opacity_changed(self, value: int) -> None:
        self.settings.subtitle_backdrop_opacity = value
        self.opacity_value.setText(f"{value}%")
        save_settings(self.settings)
        if self.subtitle_window:
            self.subtitle_window.set_backdrop_opacity(value)

    def _step_font(self, *, smaller: bool) -> None:
        # font_size_base is a DIVISOR, so a bigger base is a smaller font.
        base = self.settings.font_size_base
        self.settings.font_size_base = min(80, base + 5) if smaller else max(20, base - 5)
        if self.subtitle_window:
            if smaller:
                self.subtitle_window.decrease_font()
            else:
                self.subtitle_window.increase_font()
            self.settings.font_size_base = self.subtitle_window.get_font_size_base()
        self._scale_source_font_with_translation(base, self.settings.font_size_base)
        self.font_stepper.set_value_text(self._font_percent_text())
        save_settings(self.settings)

    def _scale_source_font_with_translation(
        self, old_base: float, new_base: float
    ) -> None:
        """Keep the original-text size proportional when the translation size
        changes, so the ratio set in the appearance expander survives a −/+."""
        try:
            old_base, new_base = float(old_base), float(new_base)
        except (TypeError, ValueError):
            return
        if not old_base or old_base == new_base:
            return
        source_base = getattr(
            self.settings, "source_font_size_base", DEFAULT_SOURCE_FONT_SIZE_BASE
        )
        scaled = max(
            SOURCE_FONT_SIZE_BASE_MIN,
            min(SOURCE_FONT_SIZE_BASE_MAX, float(source_base) * (new_base / old_base)),
        )
        if scaled == source_base:
            return
        self.settings.source_font_size_base = scaled
        if self.subtitle_window:
            self.subtitle_window.set_source_font_size_base(scaled)
        self._refresh_typography()

    def _step_speed(self, delta: float) -> None:
        speed = round(max(0.25, min(5.0, self.settings.scroll_speed + delta)), 2)
        self.settings.scroll_speed = speed
        self.speed_stepper.set_value_text(f"{speed:.1f}x")
        save_settings(self.settings)
        log(f"Scroll speed changed to: {speed:.1f}x")
        if self.subtitle_window:
            self.subtitle_window.set_scroll_speed(speed)

    # ── handlers: languages / mode ───────────────────────────────────────
    def _on_source_changed(self, _index: int) -> None:
        self.settings.source_language = language_canonical_name(
            self.source_combo.currentText()
        )
        save_settings(self.settings)
        log(f"Source language changed to: {self.settings.source_language}")
        # Segmented mode re-reads the source per audio segment; a streaming
        # socket fixed it at connect and has to be reopened.
        self._restart_pipeline_for_live_change()

    def _on_target_changed(self, _index: int) -> None:
        self.settings.target_language = language_canonical_name(
            self.target_combo.currentText()
        )
        save_settings(self.settings)
        log(f"Target language changed to: {self.settings.target_language}")
        if self.subtitle_window:
            self.subtitle_window.set_language(self.settings.target_language)

    def _on_swap_languages(self) -> None:
        source = self.settings.source_language
        target = self.settings.target_language
        if source not in TARGET_LANGUAGE_NAMES:
            return  # "Automatic" has no target counterpart
        self.settings.source_language, self.settings.target_language = target, source
        self._refresh_source_combo()
        self.target_combo.setCurrentText(language_display_name(source))
        save_settings(self.settings)
        # Swapping changes the SOURCE, and a live streaming socket fixed that
        # at connect. _on_source_changed cannot carry this: _refresh_source_combo
        # above blocks the combo's signals across its repopulate, so the handler
        # never runs. Without this call the button silently left the engine
        # transcribing the previous language — German speech came back written
        # in Arabic script — while the target language, read per translation
        # call, changed immediately.
        self._restart_pipeline_for_live_change()

    def _current_mode(self) -> str:
        return self.mode_combo.currentData() or "continuous"

    def _on_mode_changed(self, _index: int) -> None:
        self.settings.subtitle_mode = self._current_mode()
        save_settings(self.settings)
        log(f"Subtitle mode changed to: {self.settings.subtitle_mode}")
        self._sync_mode_controls()
        if self.subtitle_window:
            self.subtitle_window.set_subtitle_mode(
                effective_subtitle_mode(self.settings)
            )

    def _on_strategy_changed(self, index: int) -> None:
        if apply_strategy(self.settings, index) is None:
            return
        save_settings(self.settings)
        self._refresh_source_combo()
        self._refresh_mode_combo()
        self._refresh_provider_combos()
        self._sync_mode_controls()
        # A strategy change can select an engine whose key is missing; ask now
        # rather than at Start.
        ensure_keys(required_key_providers(self.settings), self.texts, self)

    def _on_transparent_changed(self, checked: bool) -> None:
        self.settings.transparent_static = checked
        save_settings(self.settings)
        log(f"Transparent mode: {'enabled' if checked else 'disabled'}")
        # It takes the window backdrop away, so the opacity slider below it
        # has nothing left to apply to.
        self._sync_display_sliders()
        if self.subtitle_window:
            self.subtitle_window.set_transparent_static(checked)

    def _on_catchup_changed(self, checked: bool) -> None:
        self.settings.adaptive_subtitle_catchup = checked
        save_settings(self.settings)
        if self.subtitle_window:
            self.subtitle_window.set_adaptive_catchup(checked)

    def _on_interim_changed(self, checked: bool) -> None:
        self.settings.show_interim_transcript = checked
        save_settings(self.settings)
        # Mid-session too: the bridge starts or stops sampling and clears the
        # line it leaves behind. Without this the switch only took effect on
        # the next Start.
        self.bridge.set_show_interim(checked)

    def _on_bilingual_toggled(self, checked: bool) -> None:
        self.settings.bilingual_mode = checked
        save_settings(self.settings)
        # The side-by-side layout has nothing to put in its second column
        # without an original. The stored preference is deliberately left
        # alone, so turning the original back on restores the chosen layout.
        self.layout_segment.setVisible(checked)
        if self.subtitle_window:
            self.subtitle_window.set_bilingual_mode(checked)

    def _subtitle_layout_labels(self) -> list[str]:
        return [
            self._t("subtitle_layout_combined", "Combined"),
            self._t("subtitle_side_by_side", "Side by side"),
        ]

    _LAYOUT_APPEARANCE = (
        ("font_size_base", "alt_font_size_base"),
        ("source_font_size_base", "alt_source_font_size_base"),
        ("translation_text_color", "alt_translation_text_color"),
        ("source_text_color", "alt_source_text_color"),
    )

    def _swap_layout_appearance(self) -> None:
        """Exchange the live font sizes and colours with the other layout's.

        Each layout remembers what was chosen for it, so switching back
        restores that rather than carrying one set across both. Swapping in
        place, rather than making every reader pick a set, means the subtitle
        window, the batch window and the steppers all keep reading the same
        fields and never have to know which layout is on.

        ``alt_font_size_base`` doubles as the "never switched yet" marker: it
        is the only one of the four that can never legitimately be None. On the
        first switch both sides are seeded from the live values, so nothing
        moves until something is actually changed in one of them.
        """
        s = self.settings
        live = [getattr(s, name) for name, _alt in self._LAYOUT_APPEARANCE]
        if s.alt_font_size_base is None:
            stashed = live
        else:
            stashed = [getattr(s, alt) for _name, alt in self._LAYOUT_APPEARANCE]
        for (name, alt), was_live, now_live in zip(
            self._LAYOUT_APPEARANCE, live, stashed, strict=True
        ):
            setattr(s, name, now_live)
            setattr(s, alt, was_live)

    def _on_side_by_side_toggled(self, checked: bool) -> None:
        self.settings.subtitle_side_by_side = checked
        self._swap_layout_appearance()
        save_settings(self.settings)
        if self.subtitle_window:
            self.subtitle_window.set_side_by_side(checked)
        self._apply_typography_to_window()
        self._refresh_typography()

    def _on_hide_mode_changed(self, index: int) -> None:
        self.settings.subtitle_hide_mode = SUBTITLE_HIDE_MODES[index]
        save_settings(self.settings)
        self._apply_subtitle_hide_mode()

    def _on_aot_changed(self, index: int) -> None:
        self.settings.always_on_top_mode = ALWAYS_ON_TOP_MODES[index]
        save_settings(self.settings)
        self._apply_always_on_top()

    def _apply_always_on_top(self) -> None:
        """Apply the always-on-top mode to EVERY window the app owns.

        The overlay is the obvious one, but the control panel is what an
        operator alt-tabs away from and needs back — and a secondary window is
        only reliably above other applications if it carries the flag itself,
        being a child of a topmost parent is not enough.
        """
        on_top = self._effective_always_on_top()
        if self.subtitle_window:
            self.subtitle_window.set_always_on_top(on_top)
        for window in (self, *self._open_secondary_windows()):
            # Never through setWindowFlag: that recreates the native window,
            # which repaints it from an empty surface — the white flash the
            # panel showed on every change of this setting.
            set_window_on_top(window, on_top)

    def bring_to_front(self, window: QWidget) -> None:
        """Put a secondary window back in front of the panel and the overlay.

        Raising alone is not enough when an always-on-top window is in play —
        the overlay, which an announcement can create on the spot — so the
        window is first put on the same footing, as the Tk tree does before it
        lifts the announcement window.
        """
        if window.isWindow():
            set_window_on_top(window, self._effective_always_on_top())
        window.raise_()
        window.activateWindow()

    # ── window style ─────────────────────────────────────────────────────
    def uses_integrated_windows(self) -> bool:
        """Whether secondary windows open as in-app panels.

        Unlike the Tk tree there is no platform gate: the Qt host presents
        panels as child widgets and paints the dim itself, so it needs nothing
        from the window manager (see gui/modal_host.py).
        """
        return self.settings.window_style == "integrated"

    @property
    def modal_host(self) -> ModalHost:
        """The in-app panel host, created on first use — a windowed-style
        session never builds one."""
        host = getattr(self, "_modal_host", None)
        if host is None:
            host = self._modal_host = ModalHost(self)
        return host

    def reopen_secondary_windows(self) -> None:
        """Close every secondary window and bring the settings window back.

        What a window-style change needs: a window is built either as a real
        window or as a panel and cannot be converted in place. Deferred by a
        turn because the caller is a widget inside the window being closed.
        """
        QTimer.singleShot(0, self, self._reopen_secondary_windows)

    def _reopen_secondary_windows(self) -> None:
        self.close_secondary_windows()
        self.open_settings()

    def close_secondary_windows(self) -> None:
        """Destroy every secondary window — a language or window-style change
        rebuilds them all rather than converting them in place."""
        for name in _SECONDARY_WINDOWS:
            window = getattr(self, name, None)
            if window is None:
                continue
            # These are destroyed, not merely closed, so anything a window owns
            # on the app's behalf has to be handed back first — the
            # announcement window's auto-clear timer is the only one.
            release = getattr(window, "release", None)
            if callable(release):
                release()
            window.close()
            window.deleteLater()
            setattr(self, name, None)

    def _show_secondary(self, window: QWidget) -> None:
        """Show a secondary window, in the style the settings ask for.

        As a real window it carries the current always-on-top mode, set before
        the first show() while it has no native surface yet and the flag costs
        nothing. As a panel it is inside the control panel and inherits
        everything about its stacking.
        """
        if self.uses_integrated_windows():
            self.modal_host.present(window)
            return
        if self._effective_always_on_top():
            window.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        window.show()

    def _replace_secondary(self, name: str) -> None:
        """Drop a closed secondary window before its replacement is built.

        Without this each open/close cycle left a hidden dialog parented to the
        panel — invisible, but alive, and in integrated mode an accumulating
        stack of dead child widgets.
        """
        window = getattr(self, name, None)
        if window is not None:
            window.deleteLater()
            setattr(self, name, None)

    def _open_secondary_windows(self) -> list[QWidget]:
        """Open secondary windows that carry their own window flags.

        In-app panels are excluded: they are child widgets, and always-on-top
        is a property of the top-level window they live in.
        """
        return [
            window
            for name in _SECONDARY_WINDOWS
            if (window := getattr(self, name, None)) is not None
            and window.isVisible()
            and window.isWindow()
        ]

    def _on_simple_setting(self, attribute: str, checked: bool) -> None:
        setattr(self.settings, attribute, checked)
        save_settings(self.settings)
        if attribute == "show_footer" and self.subtitle_window:
            self.subtitle_window.set_show_footer(checked)

    # ── handlers: providers ──────────────────────────────────────────────
    def _on_provider_changed(self, _index: int) -> None:
        provider = self.provider_combo.currentData()
        if not provider or provider == self.settings.ai_provider:
            return
        self.settings.ai_provider = provider
        self.settings.translation_model = get_default_model(provider, "translation")
        save_settings(self.settings)
        self._refresh_model_combos()
        ensure_keys([provider], self.texts, self)

    def _on_model_changed(self, _index: int) -> None:
        model = self.model_combo.currentData()
        if model:
            self.settings.translation_model = model
            save_settings(self.settings)

    def _on_transcription_provider_changed(self, _index: int) -> None:
        provider = self.transcription_provider_combo.currentData()
        if not provider or provider == self.settings.transcription_provider:
            return
        self.settings.transcription_provider = provider
        self.settings.transcription_model = get_default_model(provider, "transcription")
        save_settings(self.settings)
        self._refresh_model_combos()
        ensure_keys(required_key_providers(self.settings), self.texts, self)

    def _on_transcription_model_changed(self, _index: int) -> None:
        model = self.transcription_model_combo.currentData()
        if model:
            self.settings.transcription_model = model
            save_settings(self.settings)
            # A streaming socket is opened with one fixed model.
            self._restart_pipeline_for_live_change()

    def _on_use_default_translation(self, checked: bool) -> None:
        self.settings.use_default_translation_model = checked
        if checked:
            from utils.settings import DEFAULT_AI_PROVIDER

            self._manual_translation = (
                self.settings.ai_provider,
                self.settings.translation_model,
            )
            self.settings.ai_provider = DEFAULT_AI_PROVIDER
            self.settings.translation_model = get_default_model(
                DEFAULT_AI_PROVIDER, "translation"
            )
            self._refresh_provider_combos()
        elif self._manual_translation is not None:
            # Unticking restores the pick the tick overrode — otherwise trying
            # the box out once costs the choice permanently.
            (
                self.settings.ai_provider,
                self.settings.translation_model,
            ) = self._manual_translation
            self._manual_translation = None
            self._refresh_provider_combos()
        save_settings(self.settings)
        self._sync_default_model_states()

    def _on_use_default_transcription(self, checked: bool) -> None:
        self.settings.use_default_transcription_model = checked
        if checked:
            from utils.settings import (
                DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER,
                DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER,
            )

            provider = (
                DEFAULT_STREAMING_TRANSCRIPTION_PROVIDER
                if self.settings.pipeline_mode == PIPELINE_MODE_STREAMING
                else DEFAULT_SEGMENTED_TRANSCRIPTION_PROVIDER
            )
            self._manual_transcription = (
                self.settings.transcription_provider,
                self.settings.transcription_model,
            )
            self.settings.transcription_provider = provider
            self.settings.transcription_model = get_default_model(
                provider, "transcription"
            )
            self._refresh_provider_combos()
        elif self._manual_transcription is not None:
            (
                self.settings.transcription_provider,
                self.settings.transcription_model,
            ) = self._manual_transcription
            self._manual_transcription = None
            self._refresh_provider_combos()
        save_settings(self.settings)
        self._sync_default_model_states()

    # ── input level meter ────────────────────────────────────────────────
    def _poll_input_level(self) -> None:
        try:
            snapshot = self.controller.get_input_level()
        except Exception:  # noqa: BLE001 - the meter is never worth a crash
            snapshot = None
        # The APPLIED theme, not this window's copy of the setting: the two can
        # diverge, and dark-theme text on a light card is invisible.
        colors = current_colors()
        if snapshot is not None:
            from gui.levels import level_fill

            value = level_fill(snapshot.rms_dbfs)
            self.level_bar.set_value(value)
            if snapshot.clipping_ratio > 0.02:
                text, colour = (
                    self._t("input_level_clipping", "Clipping!"),
                    colors["danger"],
                )
            elif value <= 0.001:
                text, colour = (
                    self._t("input_level_no_signal", "No signal"),
                    colors["muted"],
                )
            else:
                text, colour = f"{snapshot.rms_dbfs:.0f} dBFS", colors["text"]
            # Only touch the label when the readout changed: this runs 5x a
            # second and a restyle is not free.
            if (text, colour) != self._level_text_state:
                self._level_text_state = (text, colour)
                self.level_value.setText(text)
                self.level_value.setStyleSheet(f"color: {colour};")
        self._sync_level_button()

    def _sync_level_button(self) -> None:
        testing = False
        checker = getattr(self.controller, "is_input_level_test_running", None)
        if checker is not None:
            try:
                testing = bool(checker())
            except Exception:  # noqa: BLE001
                testing = False
        state = (self._running, testing)
        if state == self._level_button_state:
            return
        self._level_button_state = state
        # A live session already feeds the meter, and a preview cannot own the
        # same device, so testing is not offered while running.
        self.level_test_btn.setEnabled(not self._running)
        self.level_test_btn.setText(
            self._t("input_level_stop_test", "Stop")
            if testing and not self._running
            else self._t("input_level_test", "Test mic")
        )

    def _toggle_input_level_test(self) -> None:
        checker = getattr(self.controller, "is_input_level_test_running", None)
        if checker is not None and checker():
            try:
                self.controller.stop_input_level_test()
            except Exception:  # noqa: BLE001
                pass
            self._level_button_state = None
            return
        if self._running:
            return
        try:
            self.controller.start_input_level_test(self._selected_device())
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            show_message(
                self,
                self._t("input_level", "Input level"),
                str(exc),
                kind="warn",
                translate=self._t,
            )
        self._level_button_state = None

    # ── secondary windows ────────────────────────────────────────────────
    def open_settings(self) -> None:
        existing = getattr(self, "_settings_window", None)
        if existing is not None and existing.isVisible():
            self.bring_to_front(existing)
            return
        self._replace_secondary("_settings_window")
        from gui.settings_window import SettingsWindow

        self._settings_window = SettingsWindow(self)
        self._show_secondary(self._settings_window)

    def open_history(self, initial_tab: str = "history") -> None:
        """Rebuilt on each open rather than refreshed, so a session recorded
        while it was closed always appears."""
        existing = getattr(self, "_history_window", None)
        if existing is not None and existing.isVisible():
            existing.show_tab(initial_tab)
            self.bring_to_front(existing)
            return
        self._replace_secondary("_history_window")
        from gui.history_window import HistoryWindow

        self._history_window = HistoryWindow(self._t, self, initial_tab=initial_tab)
        self._show_secondary(self._history_window)

    def open_batch(self) -> None:
        """Kept alive rather than rebuilt so an in-flight run survives the
        window losing focus; closing it cancels the run."""
        existing = getattr(self, "_batch_window", None)
        if existing is not None and existing.isVisible():
            self.bring_to_front(existing)
            return
        self._replace_secondary("_batch_window")
        from gui.batch_window import BatchWindow

        self._batch_window = BatchWindow(self._t, self.settings, self)
        self._show_secondary(self._batch_window)

    def open_announce(self) -> None:
        """Kept alive rather than rebuilt: it owns the auto-clear timer for a
        timed announcement, which must keep running while the window is shut."""
        existing = getattr(self, "_announce_window", None)
        if existing is not None:
            if existing.isVisible():
                self.bring_to_front(existing)
            else:
                # Back through _show_secondary rather than a bare show(): as a
                # panel it has to be presented again, or it comes up with no
                # backdrop, no ✕ and wherever it last sat.
                self._show_secondary(existing)
            return
        from gui.announce_window import AnnounceWindow

        self._announce_window = AnnounceWindow(self._t, self.settings, self)
        self._show_secondary(self._announce_window)

    def apply_theme_mode(self, theme_mode: str) -> None:
        """Re-theme the whole application — one stylesheet call, where the Tk
        tree walks per-widget registries to recolour every widget by hand."""
        from gui.theme import apply_theme

        apply_theme(QApplication.instance(), theme_mode)
        self.level_bar.update()
        self._level_text_state = None
        pixmap = self._logo_pixmap()
        if pixmap is not None:
            self.logo_label.setPixmap(pixmap)

    def apply_gui_language(self, code: str) -> None:
        """Reload the GUI texts. Rebuilding every label in place is a lot of
        bookkeeping for a rare action, so the panel is rebuilt instead — the
        same thing the Tk tree does to its secondary windows."""
        self.settings.gui_language = code
        save_settings(self.settings)
        self.texts = load_gui_translations(code)
        self.close_secondary_windows()
        geometry = self.saveGeometry()
        self._build()
        self.restoreGeometry(geometry)
        log(f"GUI language changed to: {code}", level="INFO")

    # ── session ──────────────────────────────────────────────────────────
    def on_start(self) -> None:
        """Start the pipeline off the GUI thread.

        Opening a streaming session waits for the provider's session
        confirmation, measured at 30+ seconds on the first connect after an API
        key changes. Run inline and the window is frozen ("Not responding") for
        that whole time with no sign that Start did anything — hence the worker
        thread and the "Connecting…" pill while it runs.
        """
        if self._running or self._starting:
            return
        self._persist()
        # Prompt for anything missing before touching the pipeline, so a
        # missing key is a dialog rather than a failure from inside start().
        if not ensure_keys(required_key_providers(self.settings), self.texts, self):
            return
        device = self._started_device = self._selected_device()
        self._pending_device = None
        self._ensure_subtitle_window()
        # Opened before the pipeline so provider threads have somewhere to
        # record usage from their first call; dropped again if the start fails.
        begin_cost_session()

        self._start_error: Exception | None = None
        done = threading.Event()

        def _work() -> None:
            try:
                self.controller.start(input_device=device)
            except Exception as exc:  # noqa: BLE001 - applied on the GUI thread
                self._start_error = exc
            finally:
                done.set()

        self._starting = True
        self._sync_running_state()
        threading.Thread(target=_work, daemon=True, name="pipeline-start").start()
        self._await(done, self._finish_start)

    def _finish_start(self) -> None:
        """Apply the outcome of a Start once its worker thread is done."""
        self._starting = False
        # A device chosen WHILE this start was connecting was parked rather
        # than applied (see _on_device_changed); the pipeline is up now, so it
        # can be honoured. Taken before the error branch so a failed start
        # cannot leave it to fire against the next session.
        pending, self._pending_device = self._pending_device, None
        error, self._start_error = self._start_error, None
        if error is not None:
            # No usage was billed for a start that never ran — drop the session
            # so it never shows up in the cost history.
            cancel_cost_session()
            log(f"Start failed: {error}", level="ERROR")
            self._sync_running_state()
            show_message(
                self,
                self._t("error_start_failed", "Start failed"),
                str(error),
                kind="error",
                translate=self._t,
            )
            self._apply_subtitle_hide_mode()
            return

        self._running = True
        # Buttons and pill first: the pipeline IS up, and anything below that
        # raises must not leave the panel reading "Connecting…" forever.
        self._sync_running_state()
        if self.subtitle_window:
            self.subtitle_window.set_stopped_hint(False)
        self._apply_always_on_top()
        self.bridge.start(
            streaming=streaming_enabled(self.settings),
            show_interim=self.settings.show_interim_transcript,
        )
        self._refresh_provider_combos()
        self._inactivity_timer.start()
        self._cost_flush_timer.start()
        log("Started.", level="INFO")
        # Last, so the session is fully up before the capture thread is
        # replaced — and only when it really differs from what started.
        if pending is not None and pending != self._started_device:
            log("Applying the device chosen while connecting.", level="INFO")
            self._apply_device_change(pending)

    def _await(self, done: threading.Event, then, interval_ms: int = 100) -> None:
        """Call ``then`` on the GUI thread once ``done`` is set.

        A timer rather than a queued signal so the whole start/stop flow stays
        in plain Python objects — nothing Qt-owned crosses the thread boundary.
        """

        def _poll() -> None:
            if done.is_set():
                then()
            else:
                QTimer.singleShot(interval_ms, _poll)

        QTimer.singleShot(interval_ms, _poll)

    def on_stop(self) -> None:
        """Stop the pipeline off the GUI thread — closing a streaming session
        takes the connection lock, which a reconnect blocked in a slow
        ``open_stream()`` can hold for tens of seconds."""
        if not self._running or self._stopping:
            return
        self._stopping = True
        self._sync_running_state()
        done = threading.Event()

        def _work() -> None:
            try:
                self.controller.stop()
            except Exception as exc:  # noqa: BLE001 - logged on the GUI thread
                self._stop_error = exc
            finally:
                done.set()

        self._stop_error: Exception | None = None
        self.bridge.stop()
        threading.Thread(target=_work, daemon=True, name="pipeline-stop").start()
        self._await(done, self._finish_stop)

    def _finish_stop(self) -> None:
        self._stopping = False
        error, self._stop_error = self._stop_error, None
        if error is not None:
            # The session is still up: leave it marked running so Stop can be
            # retried, and do NOT close the cost record — a stop that failed
            # has not completed anything.
            log(f"Stop failed: {error}", level="ERROR")
            self._sync_running_state()
            show_message(
                self,
                self._t("error_stop_failed", "Stop failed"),
                str(error),
                kind="error",
                translate=self._t,
            )
            return
        self._running = False
        self._end_session_tracking("completed")
        if self.subtitle_window:
            self.subtitle_window.set_live_text(None)
            self.subtitle_window.set_stopped_hint(True)
        self._apply_always_on_top()
        # An announcement left on screen after the session ends is usually
        # stale ("starts in 10 minutes"), so clear it unless the operator
        # asked for it to persist.
        if self.settings.stop_announcement_on_live_stop:
            announce = getattr(self, "_announce_window", None)
            if announce is not None:
                announce.stop_announcement()
            else:
                self.clear_announcement()
        self._sync_running_state()
        self._refresh_provider_combos()
        self._apply_subtitle_hide_mode()
        log("Stopped.", level="INFO")
        # Last, and only on this path: a session the operator ran to the end.
        self._maybe_ask_for_a_review()

    # ── session tracking (cost record + inactivity guard) ────────────────
    def _end_session_tracking(self, status: str) -> None:
        """Close the cost record and stop both session-scoped timers.

        Every path out of a running session goes through here — a normal stop,
        a failure that takes the pipeline down with it, and closing the window
        — so a session can never leave its usage unwritten or a timer running
        against a dead pipeline.
        """
        self._inactivity_timer.stop()
        self._cost_flush_timer.stop()
        try:
            end_cost_session(status)
        except Exception as exc:  # noqa: BLE001 - never block a stop or a close
            log(f"Could not finalise the cost session: {exc}", level="ERROR")

    def _check_inactivity(self) -> None:
        """Stop a session that has transcribed nothing for a long while.

        The cost guard for a forgotten session: the pipeline holds its provider
        connection open and keeps billing until someone notices. The controller
        only reports the elapsed time — the policy is the panel's, as in Tk.
        """
        if not self._running or not self.settings.auto_stop_inactivity:
            return
        try:
            idle = self.controller.seconds_since_last_activity()
        except Exception as exc:  # noqa: BLE001 - never worth killing a session
            log(f"Inactivity check failed: {exc}", level="DEBUG")
            return
        if idle >= AUTO_STOP_INACTIVITY_SECONDS:
            log(
                "Auto-stop: no transcription for "
                f"{AUTO_STOP_INACTIVITY_SECONDS // 60} minutes — stopping.",
                level="INFO",
            )
            self.on_stop()

    def _flush_cost_session(self) -> None:
        """Persist the in-progress cost record.

        Provider threads update the tracker in memory only and the record is
        written on Stop, so without this a crash mid-session loses all of its
        usage. Low frequency: it exists to bound the loss, not to be current.
        """
        if not self._running:
            return
        try:
            flush_cost_history()
        except Exception as exc:  # noqa: BLE001 - the flush is never worth a crash
            log(f"Cost flush failed: {exc}", level="DEBUG")

    # ── live changes that need the stream reopened ───────────────────────
    def _restart_pipeline_for_live_change(self) -> None:
        """Reconnect a live stream so a change it cannot apply in place lands.

        The streaming socket fixes the source language and the transcription
        model at connect, so changing either mid-session does nothing until the
        stream is reopened — the control confirms a change that is not in
        effect, which is worse than refusing it. Segmented mode re-reads both
        per audio segment and needs none of this.

        Off the GUI thread for the same reason Start is: reopening waits on the
        provider handshake. Expect the same brief audio gap as a manual
        Stop -> Start.
        """
        if not self._running or self._starting or self._stopping:
            return
        if self.settings.pipeline_mode != PIPELINE_MODE_STREAMING:
            return
        device = self._selected_device()
        log("Restarting live stream to apply change…", level="INFO")
        self._start_error = None
        done = threading.Event()

        def _work() -> None:
            try:
                self.controller.restart(input_device=device)
            except Exception as exc:  # noqa: BLE001 - applied on the GUI thread
                self._start_error = exc
            finally:
                done.set()

        self._starting = True
        self._sync_running_state()
        threading.Thread(target=_work, daemon=True, name="pipeline-restart").start()
        self._await(done, self._finish_restart)

    def _finish_restart(self) -> None:
        self._starting = False
        error, self._start_error = self._start_error, None
        if error is not None:
            # restart() can fail after its own stop() already ran, so the
            # session is gone — say so rather than leaving the panel "running".
            self._running = False
            self.bridge.stop()
            self._end_session_tracking("error")
            log(f"Live restart failed: {error}", level="ERROR")
            self._sync_running_state()
            self._refresh_provider_combos()
            self._apply_subtitle_hide_mode()
            show_message(
                self,
                self._t("error_start_failed", "Start failed"),
                str(error),
                kind="error",
                translate=self._t,
            )
            return
        self._sync_running_state()
        log("Live stream restarted", level="INFO")

    def _on_device_lost(self) -> None:
        # Say so. Stopping silently mid-session leaves the operator watching a
        # dead overlay with no idea the microphone went away.
        if self._running:
            self.on_stop()
        title = self._t("audio_device_lost", "Audio device lost")
        log(title, level="ERROR")
        self._info(title, title)

    def _selected_device(self) -> int | None:
        pos = self.device_combo.currentIndex()
        if not self.device_indices or not (0 <= pos < len(self.device_indices)):
            return None
        return self.device_indices[pos]

    def _on_device_changed(self, _index: int) -> None:
        """Swap the capture device without stopping the session.

        Both pipeline modes only need the capture thread replaced, so this is a
        hot-swap rather than a restart. Persisted either way so the choice
        survives a restart even when nothing is running.
        """
        self._persist()
        device = self._selected_device()
        if device is None:
            return
        log(f"Input device changed to: {self.device_combo.currentText()}")
        if self._starting:
            # Start captured the device it began with before spawning its
            # worker, and it is connecting on that one right now. Swapping
            # underneath it races the connect; dropping the change silently —
            # which is what used to happen, because _running is still False
            # here — leaves the pipeline on a device the panel no longer
            # shows. The operator sees the device they picked and hears
            # nothing from it, and only a switch away and back fixes it.
            self._pending_device = device
            return
        if not self._running:
            return
        self._apply_device_change(device)

    def _apply_device_change(self, device: int) -> None:
        try:
            if not self.controller.change_input_device(device):
                # Refused (e.g. the stream would not release the old device):
                # fall back to a full restart, which always applies.
                self.controller.restart(input_device=device)
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            log(f"Device switch failed: {exc}", level="ERROR")
            self._running = False
            self._end_session_tracking("error")
            self._sync_running_state()
            show_message(
                self, "MinbarLive", str(exc), kind="error", translate=self._t
            )

    # ── subtitle window ──────────────────────────────────────────────────
    def _effective_always_on_top(self) -> bool:
        mode = self.settings.always_on_top_mode
        if mode == "always":
            return True
        if mode == "running":
            return self._running
        return False

    def _subtitle_window_should_exist(self) -> bool:
        mode = self.settings.subtitle_hide_mode
        if mode == "always":
            return False
        if mode == "stopped":
            return self._running
        return True  # "never"

    def _apply_subtitle_hide_mode(self) -> None:
        """Create or destroy the overlay so it matches the hide mode and the
        running state, without disturbing an already-correct window. An active
        announcement keeps it alive."""
        should = self._subtitle_window_should_exist()
        if should and self.subtitle_window is None:
            self._ensure_subtitle_window()
            if self.subtitle_window and not self._running:
                self.subtitle_window.set_stopped_hint(True)
        elif not should and self.subtitle_window is not None:
            if not self._announcement_active:
                self._teardown_subtitle_window()

    def _ensure_subtitle_window(self) -> None:
        if self.subtitle_window is not None:
            return
        s = self.settings
        self.subtitle_window = SubtitleWindow(
            on_stop=self.on_stop,
            monitor_index=s.monitor_index,
            font_size_base=s.font_size_base,
            source_font_size_base=s.source_font_size_base,
            translation_text_color=s.translation_text_color,
            source_text_color=s.source_text_color,
            target_language=s.target_language,
            subtitle_mode=effective_subtitle_mode(s),
            scroll_speed=s.scroll_speed,
            transparent_static=s.transparent_static,
            window_height_percent=s.window_height_percent,
            static_lift_percent=s.static_lift_percent,
            backdrop_opacity=s.subtitle_backdrop_opacity,
            show_footer=s.show_footer,
            theme_mode=s.subtitle_theme_mode,
            bilingual_mode=s.bilingual_mode,
            side_by_side=s.subtitle_side_by_side,
            adaptive_catchup=s.adaptive_subtitle_catchup,
        )
        self.subtitle_window.set_always_on_top(self._effective_always_on_top())
        self._apply_active_announcement()
        self.subtitle_window.show()

    def _teardown_subtitle_window(self) -> None:
        if self.subtitle_window is not None:
            self.subtitle_window.destroy()
            self.subtitle_window = None

    # ── announcements (driven by the announcement window) ────────────────
    def show_announcement(self, text: str) -> None:
        """Put ``text`` on the overlay, creating one if the hide policy left
        none open — otherwise an announcement is impossible while stopped,
        which is exactly when it is most useful."""
        self._announcement_active = True
        # Kept, not just flagged: an "until stopped" message has to survive the
        # overlay being torn down and built again (Tk keeps the same state on
        # the AppGUI for the same reason) — see _ensure_subtitle_window.
        self._announcement_text = text
        self._ensure_subtitle_window()
        if self.subtitle_window:
            self.subtitle_window.set_announcement(text)

    def clear_announcement(self) -> None:
        self._announcement_active = False
        self._announcement_text = ""
        if self.subtitle_window:
            self.subtitle_window.clear_announcement()
        # If the overlay was kept open only for the announcement, close it.
        self._apply_subtitle_hide_mode()

    def has_active_announcement(self) -> bool:
        return self._announcement_active

    def _apply_active_announcement(self) -> None:
        """Put a still-running announcement back onto a freshly built overlay.

        An "until stopped" message has to survive the overlay being torn down
        and built again — a stop/start under the "when stopped" hide policy, a
        monitor change — instead of vanishing with the window it happened to
        have been drawn on. The Tk tree keeps the same state on the AppGUI for
        exactly this.
        """
        if (
            self._announcement_active
            and self._announcement_text
            and self.subtitle_window is not None
        ):
            self.subtitle_window.set_announcement(self._announcement_text)

    # ── pipeline signals (already on the GUI thread) ─────────────────────
    def _on_translation(self, text: str, source_text) -> None:
        if self.subtitle_window:
            self.subtitle_window.add_subtitle(text, source_text=source_text)

    def _on_live_text(self, text: str, settled: bool) -> None:
        if self.subtitle_window:
            self.subtitle_window.set_live_text(text, settled)

    # ── persistence / shutdown ───────────────────────────────────────────
    def _maybe_ask_for_a_review(self) -> None:
        """Count a completed session and put the review question if it is due.

        Called from ``_finish_stop`` only, so what is counted is a session the
        operator ran to the end — not a launch, and not a start that failed.

        **Never while the update notice is up.** Two accent-soft strips stacked
        above the cards read as a wall of nagging, and the update offer is the
        one with something the user may act on today. The counter is left where
        it is rather than reset, so the question simply lands after the next
        session instead — which is why the due test is ``>=`` and not ``==``.
        """
        if self.settings.review_prompt_disabled:
            return
        self.settings.sessions_since_review_prompt += 1
        save_settings(self.settings)
        if self.update_banner.isVisible():
            return
        self.review_banner.maybe_show(
            self.settings.sessions_since_review_prompt,
            self.settings.review_prompt_disabled,
        )

    def _on_review_decision(self, sessions: int, disabled: bool) -> None:
        """Persist the answer to the review question, whatever it was."""
        self.settings.sessions_since_review_prompt = sessions
        self.settings.review_prompt_disabled = disabled
        save_settings(self.settings)

    def _on_update_skipped(self, version: str) -> None:
        """Remember a release the user chose to pass over.

        Written through at once rather than at the next ``_persist``: the point
        of skipping is that the notice is gone for good, and a panel that never
        gets round to persisting (a crash, a kill) would ask again on the next
        launch.
        """
        self.settings.skipped_update_version = version
        save_settings(self.settings)

    def _persist(self) -> None:
        self.settings.source_language = language_canonical_name(
            self.source_combo.currentText()
        )
        self.settings.target_language = language_canonical_name(
            self.target_combo.currentText()
        )
        self.settings.subtitle_mode = self._current_mode()
        self.settings.monitor_index = self.monitor_combo.currentIndex()
        self.settings.bilingual_mode = self.bilingual_check.isChecked()
        self.settings.subtitle_side_by_side = self.layout_segment.current_index() == 1
        pos = self.device_combo.currentIndex()
        if 0 <= pos < len(self.device_base_names):
            self.settings.input_device_name = self.device_base_names[pos]
        save_settings(self.settings)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        self._persist_window_geometry()
        # Stopped inline, not through on_stop(): its worker thread reports back
        # through a timer that will never fire once the window is gone, so the
        # pipeline would outlive the app.
        if self._running or self._starting:
            self._running = self._starting = False
            self.bridge.stop()
            try:
                self.controller.stop()
            except Exception as exc:  # noqa: BLE001 - shutting down anyway
                log(f"Stop failed: {exc}", level="ERROR")
            # Whatever the stop did, the usage is real and the process is about
            # to go away — write the record before it does.
            self._end_session_tracking("closed")
        self._log_timer.stop()
        self._level_timer.stop()
        self._teardown_subtitle_window()
        super().closeEvent(event)

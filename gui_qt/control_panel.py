"""Qt control panel.

Layout parity with the CustomTkinter panel is deliberate: the same four cards,
in the same order, in the same reflowing 1/2/3-column grid, with the same
header button row and the same collapsible log panel on the right. Operators
know where things are, and "it moved" is a real cost.

What is NOT ported is the measurement machinery the Tk panel needs to achieve
that layout — ``_responsive_scale``, ``_align_advanced_card``'s idle-requeueing
correction loop (the one that divided by the wrong scale factor and froze the
panel in PR #43), ``_collapsed_margin``, ``window_work_area``. Qt layouts do
that arithmetic; only the column count is ours to decide.
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
from gui_qt.api_keys import activate_stored_keys, ensure_keys
from gui_qt.dialogs import show_message
from gui_qt.i18n import load_gui_translations
from gui_qt.icons import app_icon
from gui_qt.modal_host import ModalHost
from gui_qt.pipeline_bridge import PipelineBridge, streaming_enabled
from gui_qt.subtitle_window import SubtitleWindow
from gui_qt.theme import current_colors
from gui_qt.widgets import (
    CONTROL_H,
    AudioLevelBar,
    Card,
    Dropdown,
    Expander,
    SegmentedControl,
    Slider,
    Stepper,
    field,
    set_window_on_top,
)
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
    STREAMING_TRANSCRIPTION_PROVIDERS,
    SUBTITLE_HIDE_MODES,
    TARGET_LANGUAGE_DISPLAY_NAMES,
    TARGET_LANGUAGE_NAMES,
    language_canonical_name,
    language_display_name,
    load_settings,
    save_settings,
)

# Logical widths at which the card grid gains a column, measured from what the
# columns actually need (352 / 365 / 204 plus the grid's margins and spacing)
# with a little headroom for a wordier GUI language. The horizontal scrollbar
# is off, so a threshold that lets a column below its minimum does not scroll —
# it clips.
_COL2_MIN_W = 800
_COL3_MIN_W = 1030
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
# Largest height difference the 2-column grid will absorb to make both columns
# end on one line (see _level_two_column_bottoms). Comfortably above the tens
# of pixels their content happens to differ by, and well below the ~240 an
# opened subtitle-appearance section adds.
_LEVEL_FILL_MAX_PX = 140

# Step applied to the original-text divisor per −/+ click, as in gui/typography.
_SOURCE_FONT_STEP = 5.0
# The shipped font_size_base, i.e. the 100% the size stepper counts from.
_FONT_SIZE_BASE_DEFAULT = 40

# Opening size when nothing is stored. Wide enough for the two-column grid
# without the sidebar scrollbar; the log adds its own width on top.
_DEFAULT_W = 880
_DEFAULT_H = 640
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
        self._announcement_active = False
        self._announcement_text = ""
        self._log_collapsed = self.settings.log_panel_collapsed
        self._columns: int | None = None
        self._level_queued = False
        activate_stored_keys()

        self.bridge = PipelineBridge(controller, self)
        self.bridge.translation.connect(self._on_translation)
        self.bridge.live_text.connect(self._on_live_text)
        self.bridge.audio_device_lost.connect(self._on_device_lost)

        self.setWindowTitle(self._t("window_title", "MinbarLive"))
        self._apply_window_icon()
        self._build()
        # Small enough that the panel can be dragged down to a corner of the
        # screen; everything above it scrolls. Before any geometry is applied,
        # so a stored size is never clamped against a stale minimum.
        self.setMinimumSize(QSize(420, 420))
        if not self._restore_window_geometry():
            self.resize(
                max(_DEFAULT_W, _SIDEBAR_W_WITH_LOG + _LOG_PANEL_MIN_W)
                if not self._log_collapsed
                else _DEFAULT_W,
                _DEFAULT_H,
            )
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
        self.update_banner.start_check(self.settings.check_for_updates)

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
        cached QIcon, built from the logo's mark (see gui_qt/icons.py)."""
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
        from gui_qt.update_banner import UpdateBanner

        self.update_banner = UpdateBanner(self._t)
        side.addWidget(self.update_banner)

        self.card_area = QScrollArea()
        self.card_area.setWidgetResizable(True)
        self.card_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.cards_host = QWidget()
        self.grid = QGridLayout(self.cards_host)
        self.grid.setContentsMargins(18, 14, 18, 18)
        self.grid.setHorizontalSpacing(18)
        self.grid.setVerticalSpacing(18)

        # Three column groups, exactly as the Tk panel: A = Controls +
        # Display, B = Translation flow, C = Advanced. A group is the reflow's
        # atom, so a tall card never pads a short one out with dead space.
        self.col_a, box_a = self._column()
        self.col_b, box_b = self._column()
        self.col_c, box_c = self._column()
        display_card = self._display_card()
        language_card = self._language_card()
        advanced_card = self._advanced_card()
        # A spacer above and below each column's cards: side by side the
        # columns should end level, and which of the two spacers (or the card
        # itself) absorbs the slack is what decides how. See
        # _set_equal_column_heights.
        for box in (box_a, box_b, box_c):
            box.addStretch(0)
        box_a.addWidget(self._control_card())
        box_a.addWidget(display_card)
        box_b.addWidget(language_card)
        box_c.addWidget(advanced_card)
        for box in (box_a, box_b, box_c):
            box.addStretch(1)
        self._column_tails = [
            (box_a, display_card),
            (box_b, language_card),
            (box_c, advanced_card),
        ]
        for _box, card in self._column_tails:
            card.add_stretch()

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

    @staticmethod
    def _column() -> tuple[QWidget, QVBoxLayout]:
        widget = QWidget()
        box = QVBoxLayout(widget)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(18)
        return widget, box

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
        frame, self.height_slider, self.height_value = self._slider_panel(
            self._t("height", "Window height"),
            5,
            100,
            self.settings.window_height_percent,
            self._on_height_changed,
        )
        self.height_caption = frame.caption
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
        """Repaint the colour buttons from the stored values."""
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
        card.body.addWidget(self.catchup_check)
        card.body.addWidget(self.interim_check)
        card.body.addWidget(self.transparent_check)
        card.body.addWidget(self.bilingual_check)

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
        self.use_default_transcription = QCheckBox(self._t("use_default", "Default"))
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
        self.use_default_translation = QCheckBox(self._t("use_default", "Default"))
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
        """Provider above model, with "Default" beside the model.

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
        box.setContentsMargins(0, 16, 18, 18)
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

    def _toggle_log_panel(self) -> None:
        self._log_collapsed = not self._log_collapsed
        self.log_panel.setVisible(not self._log_collapsed)
        self.log_toggle.setText("▶" if self._log_collapsed else "◀")
        self.settings.log_panel_collapsed = self._log_collapsed
        save_settings(self.settings)
        self._apply_log_panel_widths()
        # The log opens INSIDE the current window; it only widens one that
        # cannot hold both, which would otherwise give the log a column a
        # character wide. Never past the screen it is on.
        if not self._log_collapsed and not self.isMaximized():
            screen = self.screen() or QGuiApplication.primaryScreen()
            available = screen.availableGeometry().width() if screen else 0
            wanted = _SIDEBAR_W_WITH_LOG + _LOG_PANEL_MIN_W
            if available:
                wanted = min(wanted, available)
            if self.width() < wanted:
                self.resize(wanted, self.height())
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
        """Width the card grid has to work with.

        The viewport is authoritative once the window is on screen. Before
        that it still reports its default, so fall back to the window's own
        width minus whatever the log panel will claim — otherwise the first
        layout is computed against a placeholder size.
        """
        viewport = self.card_area.viewport().width()
        if self.isVisible() and viewport > 1:
            return viewport
        return max(
            0, self.width() - (0 if self._log_collapsed else _SIDEBAR_W_WITH_LOG)
        )

    def _column_count(self) -> int:
        # With the log open the sidebar is pinned narrow, so the cards get one
        # column — the same trade the Tk panel makes.
        if not self._log_collapsed:
            return 1
        width = self._available_width()
        if width <= 1:
            return 2  # nothing to measure yet; the default window shows two
        if width >= _COL3_MIN_W:
            return 3
        if width >= _COL2_MIN_W:
            return 2
        return 1

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
        cols = self._column_count()
        if cols == self._columns and not force:
            # Cheap, and the only thing a plain resize can change: a card's
            # content may have grown since the columns were last arranged.
            self._level_two_column_bottoms()
            return
        changed = cols != self._columns
        self._columns = cols
        for widget in (self.col_a, self.col_b, self.col_c):
            self.grid.removeWidget(widget)
        for i in range(3):
            self.grid.setColumnStretch(i, 0)

        if cols == 1:
            for i, widget in enumerate((self.col_a, self.col_b, self.col_c)):
                self.grid.addWidget(widget, i, 0)
            self.grid.setColumnStretch(0, 1)
        elif cols == 2:
            # A spans both rows so its height does not push C away from B.
            self.grid.addWidget(self.col_a, 0, 0, 2, 1)
            self.grid.addWidget(self.col_b, 0, 1)
            self.grid.addWidget(self.col_c, 1, 1)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 1)
        else:
            for i, widget in enumerate((self.col_a, self.col_b, self.col_c)):
                self.grid.addWidget(widget, 0, i)
                self.grid.setColumnStretch(i, 1)
        for widget in (self.col_a, self.col_b, self.col_c):
            widget.setVisible(True)
        # The scroll area makes cards_host at least viewport-tall, and without
        # somewhere for that surplus to go the card row swallowed it — the
        # cards grew to the bottom of the window instead of stopping at the
        # tallest one. One stretched row absorbs it.
        #
        # In the 2-column layout that row is row 1 — the one holding Advanced —
        # and it does a second job there: column A spans both rows, so its
        # height (which changes whenever the subtitle-appearance expander
        # opens) has to go somewhere. Stretched, row 1 takes all of it and
        # row 0 stays exactly as tall as the card in it, which is what keeps
        # Advanced sitting still under Translation flow instead of sliding
        # down the window.
        used_rows = 3 if cols == 1 else 1
        for row in range(4):
            self.grid.setRowStretch(row, 1 if row == used_rows else 0)
        # Only side by side in three columns: in the L-shaped 2-column layout
        # levelling this way means padding Advanced away from the card above
        # it, and that gap grows every time column A does. Two columns end
        # level a different way — see _level_two_column_bottoms.
        self._set_equal_column_heights(cols >= 3)
        self._level_two_column_bottoms()
        # Last, so the re-entrant _relayout_columns its toggle triggers finds a
        # grid that is already consistent.
        if changed:
            self._sync_advanced_for_columns(cols)

    def _set_equal_column_heights(self, equal: bool) -> None:
        """Side by side in three columns, the cards should end level.

        Three ragged bottom edges read as unfinished. In the 1- and 2-column
        layouts each card keeps its natural height instead: stacked, levelling
        has nothing to level, and in the L-shaped 2-column layout it only pads
        Advanced away from the card above it.

        An OPEN card takes the slack itself (its own trailing stretch keeps its
        content top-aligned). A COLLAPSED one must not — stretching a header
        strip into a tall empty box is worse than a ragged edge — so the spacer
        ABOVE it takes the slack instead and the strip sits on the baseline.

        Every stretch factor has to be set explicitly: a spacer from
        ``addStretch`` is expansive whatever its factor, so zeroing one alone
        still let it swallow the height and the card grew by nothing.
        """
        for box, card in self._column_tails:
            fill = equal and card.is_expanded()
            lead = 1 if (equal and not fill) else 0
            box.setStretch(0, lead)  # leading spacer: bottom-aligns the card
            box.setStretch(box.indexOf(card), 1 if fill else 0)
            box.setStretch(box.count() - 1, 0 if equal else 1)
            card.setSizePolicy(
                QSizePolicy.Preferred,
                QSizePolicy.Expanding if fill else QSizePolicy.Preferred,
            )

    def _level_two_column_bottoms(self) -> None:
        """Queue the two-column levelling for once the layout has settled.

        Hiding or showing a widget invalidates size hints through a posted
        event, so anything reading them in the same call gets the PREVIOUS
        layout's numbers — the padding would then be one toggle behind.
        Coalesced, and the handler schedules nothing, so this cannot become
        the idle-requeueing correction loop that froze the Tk panel (PR #43).
        """
        if self._level_queued:
            return
        self._level_queued = True
        QTimer.singleShot(0, self._apply_two_column_levelling)

    def _apply_two_column_levelling(self) -> None:
        """Make the two columns end on one line.

        Their natural heights are whatever their cards happen to add up to, so
        the bottom borders land a handful of pixels apart — which reads as a
        mistake rather than as a layout. The SHORTER column's last card takes
        the difference; a Card keeps its content top-aligned, so the padding
        lands below it, invisible.

        Unless that card is COLLAPSED, and then it must not grow at all: a
        header strip inflated into a tall empty box is exactly what a collapsed
        card exists to avoid. The spacer ABOVE it takes the slack instead, so
        the strip keeps its own height and the two columns still end on one
        line — the rule _set_equal_column_heights already applies at three
        columns.

        Only a small difference, though: when one column is a whole opened
        subtitle-appearance section taller, inflating a card by that much is
        worse than a ragged edge — and pushing Advanced down to meet it is
        exactly the dead gap this layout exists to avoid.

        Each card's own ``sizeHint`` ignores the minimum set here, so the
        measurement never sees the previous correction: running this twice
        gives the same answer.
        """
        self._level_queued = False
        tails = getattr(self, "_column_tails", None)
        if not tails:
            return
        # Deliver the invalidations a hidden/shown widget posted, so the hints
        # below describe the layout as it is now and not as it was one toggle
        # ago. A 0-timer alone does not guarantee they have been processed.
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        (display_box, display), (advanced_box, advanced) = tails[0], tails[2]
        if self._columns != 2:
            for box, card in ((display_box, display), (advanced_box, advanced)):
                card.setMinimumHeight(0)
                self._pad_above(box, 0)
            return
        gap = self.grid.verticalSpacing()
        left = self._natural_height(display_box)
        right = self._natural_height(tails[1][0]) + gap + self._natural_height(
            advanced_box
        )
        if left > right:
            (shorter, shorter_box), (taller, taller_box) = (
                (advanced, advanced_box),
                (display, display_box),
            )
        else:
            (shorter, shorter_box), (taller, taller_box) = (
                (display, display_box),
                (advanced, advanced_box),
            )
        extra = abs(left - right)
        if extra > _LEVEL_FILL_MAX_PX:
            extra = 0
        taller.setMinimumHeight(0)
        self._pad_above(taller_box, 0)
        collapsed = extra and not shorter.is_expanded()
        self._pad_above(shorter_box, extra if collapsed else 0)
        shorter.setMinimumHeight(
            0 if collapsed or not extra else shorter.sizeHint().height() + extra
        )

    @staticmethod
    def _pad_above(box, pixels: int) -> None:
        """Set the height of a column's leading spacer.

        Excluded from _natural_height (which counts widgets only), so a second
        pass measures the same columns and lands on the same number — no
        feedback loop.
        """
        spacer = box.itemAt(0).spacerItem() if box.count() else None
        if spacer is None:
            return
        spacer.changeSize(0, pixels, QSizePolicy.Minimum, QSizePolicy.Fixed)
        box.invalidate()

    @staticmethod
    def _natural_height(box) -> int:
        """A column's height from its cards' own hints — spacers excluded, so
        the stretch that fills a tall window does not count as content."""
        heights = [
            box.itemAt(i).widget().sizeHint().height()
            for i in range(box.count())
            if box.itemAt(i).widget() is not None
        ]
        return sum(heights) + max(0, len(heights) - 1) * box.spacing()

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
        """A model dropdown is disabled while its "Default" box is ticked —
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
        # Showing or hiding a row changes the translation card's height, and
        # with it what the two columns need to end level.
        self._level_two_column_bottoms()

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
        self.settings.window_height_percent = value
        self.height_value.setText(f"{value}%")
        save_settings(self.settings)
        if self.subtitle_window:
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
        if self.subtitle_window:
            self.subtitle_window.set_scroll_speed(speed)

    # ── handlers: languages / mode ───────────────────────────────────────
    def _on_source_changed(self, _index: int) -> None:
        self.settings.source_language = language_canonical_name(
            self.source_combo.currentText()
        )
        save_settings(self.settings)
        # Segmented mode re-reads the source per audio segment; a streaming
        # socket fixed it at connect and has to be reopened.
        self._restart_pipeline_for_live_change()

    def _on_target_changed(self, _index: int) -> None:
        self.settings.target_language = language_canonical_name(
            self.target_combo.currentText()
        )
        save_settings(self.settings)
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

    def _current_mode(self) -> str:
        return self.mode_combo.currentData() or "continuous"

    def _on_mode_changed(self, _index: int) -> None:
        self.settings.subtitle_mode = self._current_mode()
        save_settings(self.settings)
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

    def _on_bilingual_toggled(self, checked: bool) -> None:
        self.settings.bilingual_mode = checked
        save_settings(self.settings)
        if self.subtitle_window:
            self.subtitle_window.set_bilingual_mode(checked)

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
        from the window manager (see gui_qt/modal_host.py).
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

            self.settings.ai_provider = DEFAULT_AI_PROVIDER
            self.settings.translation_model = get_default_model(
                DEFAULT_AI_PROVIDER, "translation"
            )
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
            self.settings.transcription_provider = provider
            self.settings.transcription_model = get_default_model(
                provider, "transcription"
            )
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
            from gui_qt.levels import level_fill

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
        from gui_qt.settings_window import SettingsWindow

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
        from gui_qt.history_window import HistoryWindow

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
        from gui_qt.batch_window import BatchWindow

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
        from gui_qt.announce_window import AnnounceWindow

        self._announce_window = AnnounceWindow(self._t, self.settings, self)
        self._show_secondary(self._announce_window)

    def apply_theme_mode(self, theme_mode: str) -> None:
        """Re-theme the whole application — one stylesheet call, where the Tk
        tree walks per-widget registries to recolour every widget by hand."""
        from gui_qt.theme import apply_theme

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
        self._columns = None
        self._build()
        self.restoreGeometry(geometry)
        log(
            self._t("log_gui_language_changed", "GUI language changed to: {language}")
            .format(language=code),
            level="INFO",
        )

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
        device = self._selected_device()
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
        log(self._t("log_started", "Started."), level="INFO")

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
        log(self._t("log_stopped", "Stopped."), level="INFO")

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
        if not self._running or device is None:
            return
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
            backdrop_opacity=s.subtitle_backdrop_opacity,
            show_footer=s.show_footer,
            theme_mode=s.subtitle_theme_mode,
            bilingual_mode=s.bilingual_mode,
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

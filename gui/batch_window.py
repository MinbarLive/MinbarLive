"""Batch / file window: turn a recording into subtitles.

The control ORDER is deliberate: settings first (languages, output format,
subtitle content, then the collapsed "More settings" expander with the four
engine dropdowns), THEN the file picker, then progress, status and the action
buttons. Picking a file is the last thing you do before pressing Start, so it
sits next to Start — not at the top.

Laid out as a hero plus three cards over a fixed action bar: every other window
here (settings, history, announcement) groups its controls into cards, and one
box around everything read as the odd one out. The cards scroll; Start never
does.

The batch job is configured independently of the live app: nothing here writes
to the main settings. The pipeline itself is ``batch/processor.py``, reused
unchanged.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading

from PySide6.QtCore import QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gui.api_keys import ensure_keys
from gui.dialogs import ask_yes_no, ask_yes_no_or_dismiss
from gui.widgets import Dropdown, Expander, SegmentedControl, field
from gui.window_size import SECONDARY_WINDOW_W, apply_content_size
from providers import (
    PROVIDER_CHOICES,
    TRANSCRIPTION_PROVIDER_CHOICES,
    get_default_model,
    get_model_choices,
    ranked_keyed_provider,
)
from utils.frozen_env import external_process_env
from utils.logging import log
from utils.settings import (
    SOURCE_LANGUAGES,
    STREAMING_TRANSCRIPTION_PROVIDERS,
    TARGET_LANGUAGE_NAMES,
    language_canonical_name,
    language_display_name,
)

# Segmented sibling for each streaming engine. Duplicated from
# gui/batch_view.py rather than imported: importing that module would pull
# customtkinter into the Qt process, and the two toolkits never share one.
_BATCH_STT_FALLBACKS = {"openai_realtime": "openai", "gemini_realtime": "gemini"}


def _batch_stt_fallback(provider_id: str) -> str:
    """Segmented sibling for a streaming engine. Deepgram has no segmented
    mode and no shared key family — pick the highest-ranked segmented STT
    provider the user actually holds a key for."""
    if provider_id == "deepgram":
        return ranked_keyed_provider(["openai", "gemini"])
    return _BATCH_STT_FALLBACKS.get(provider_id, provider_id)


# (settings value, translation key, English fallback) per output format, in the
# order the segmented control shows them.
_OUTPUT_FORMATS = (
    ("srt", "batch_output_srt", "Subtitles (.srt)"),
    ("txt", "batch_output_text", "Transcript (.txt)"),
    ("both", "batch_output_both", "Both (.srt + .txt)"),
)
_DEFAULT_OUTPUT_INDEX = 2  # both — the extra file costs nothing

# Bilingual subtitles: off / on, as a two-way segmented choice rather than a
# check box, so it reads like the other either/or selectors in the app.
_BILINGUAL_LABELS = (
    ("batch_bilingual_off", "Translation only"),
    ("batch_bilingual_on", "Original + translation"),
)
_DEFAULT_BILINGUAL_INDEX = 1  # the original above the translation, as in Tk

# Width the window is laid out for, fixed: the content is a single column of
# dropdowns and buttons that stretch badly. Shared with the settings and
# announcement windows, which are the same shape — see gui/window_size.py.
# The height follows the content, which changes with the GUI language and the
# More-settings expander — see _resize_to_content.
BATCH_WINDOW_W = SECONDARY_WINDOW_W

# Padding around the card column, the gap between cards, and a card's own
# inner padding. Tighter than the card padding inside the settings window:
# this one is sized to its content, and every pixel of padding is one the
# whole window grows by.
_PAD = 16
_CARD_GAP = 12
_CARD_PAD = 16

# Longest picker-button label before the filename is truncated in the middle,
# so the start AND the extension stay readable.
_FILE_NAME_LIMIT = 48

# What the picker's "Audio/Video" filter offers. batch/processor.py has no
# allowlist of its own — anything ffmpeg can read is converted to 16 kHz WAV —
# so this list exists only to spare the operator the "All files" dropdown, and
# a format missing from it looks unsupported when it is not. Covers what a
# mosque recording actually arrives as: phones (.m4a .3gp .amr), cameras
# (.mov .mts .m2ts), OBS (.mkv .flv), Windows (.wma .wmv .avi) and broadcast
# captures (.ts .mpg .mpeg), on top of the plain audio formats.
_MEDIA_EXTENSIONS = (
    "3gp aac aiff amr avi caf flac flv m2ts m4a m4v mkv mov mp3 mp4 mpeg mpg "
    "mts ogg ogv opus ts wav webm wma wmv"
).split()

# Progress bar height. Pinned rather than left to the stylesheet, which states
# it as a hint the layout is free to exceed when it has height to spare.
_PROGRESS_H = 10


class _Worker(QObject):
    """Runs process_file on a plain thread and reports back as Qt signals."""

    progress = Signal(int, int)
    finished = Signal(str)  # output path, "" when cancelled
    failed = Signal(str)
    ffmpeg_missing = Signal()  # the input needs ffmpeg and none is installed
    dl_progress = Signal(int)  # ffmpeg download, percent
    dl_finished = Signal()  # ffmpeg is in place

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, **kwargs) -> None:
        self.cancel_event.clear()

        def _run() -> None:
            from batch.processor import (  # lazy: heavy import
                FfmpegNotFoundError,
                process_file,
            )

            try:
                path = process_file(
                    progress_callback=lambda done, total: self.progress.emit(
                        done, total
                    ),
                    cancel_event=self.cancel_event,
                    **kwargs,
                )
                self.finished.emit(path or "")
            except FfmpegNotFoundError:
                # Its own signal: the window can offer to fetch ffmpeg rather
                # than showing the raw exception as a dead end.
                log("Batch run needs ffmpeg", level="WARNING")
                self.ffmpeg_missing.emit()
            except Exception as exc:  # noqa: BLE001 - reported to the operator
                log(f"Batch run failed: {exc}", level="ERROR")
                self.failed.emit(str(exc))

        self._thread = threading.Thread(target=_run, daemon=True, name="qt-batch")
        self._thread.start()

    def start_ffmpeg_download(self) -> None:
        """Fetch ffmpeg into the app data directory, on its own thread.

        Reports through signals like the run does, so every update lands on
        the GUI thread rather than being painted from this one.
        """
        self.cancel_event.clear()

        def _run() -> None:
            from utils.ffmpeg_download import (
                FfmpegDownloadCancelled,
                download_ffmpeg,
            )

            try:
                download_ffmpeg(
                    progress_cb=self.dl_progress.emit,
                    cancel_event=self.cancel_event,
                )
                self.dl_finished.emit()
            except FfmpegDownloadCancelled:
                self.finished.emit("")  # reuses the cancelled-run path
            except Exception as exc:  # noqa: BLE001 - reported to the operator
                log(f"ffmpeg download failed: {exc}", level="ERROR")
                self.failed.emit(str(exc))

        self._thread = threading.Thread(
            target=_run, daemon=True, name="qt-ffmpeg-download"
        )
        self._thread.start()

    def cancel(self) -> None:
        self.cancel_event.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


class BatchWindow(QDialog):
    def __init__(self, translate, settings, parent=None):
        super().__init__(parent)
        self._t = translate
        self._panel = parent
        self.settings = settings
        self._input_path = ""
        self._output_path = ""
        self.setWindowTitle(self._t("batch_file", "Batch / File"))
        if parent is not None:
            self.setWindowIcon(parent.windowIcon())

        self.worker = _Worker(self)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.ffmpeg_missing.connect(self._on_ffmpeg_missing)
        self.worker.dl_progress.connect(self._on_download_progress)
        self.worker.dl_finished.connect(self._on_download_finished)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # The cards scroll; the action bar below does not. On a short screen
        # that is the difference between reaching Start and not.
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
        column.addWidget(self._language_card())
        column.addWidget(self._output_card())
        column.addWidget(self._file_card())
        # Keeps the cards top-aligned when the window is taller than they are.
        column.addStretch(1)
        self.scroll.setWidget(self.body)
        outer.addWidget(self.scroll, 1)

        self.action_bar = self._action_bar()
        outer.addWidget(self.action_bar)

        self._sync_file_row()  # also applies the bilingual segment's own rule
        self._resize_to_content()

    # ── build ────────────────────────────────────────────────────────────
    def _card(self, symbol: str, title: str) -> tuple[QFrame, QVBoxLayout]:
        """A titled card, laid out like the settings window's.

        The heading is the smaller ``#heading`` style rather than the settings
        window's 20px ``#card_title``: three of those in a window sized to its
        content cost about 45px of height for no extra information, and the
        hero above already carries the big title.
        """
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(_CARD_PAD, _CARD_PAD - 2, _CARD_PAD, _CARD_PAD - 2)
        box.setSpacing(10)
        heading = QLabel(f"{symbol}  {title}")
        heading.setObjectName("heading")
        box.addWidget(heading)
        return card, box

    def _hero(self) -> QHBoxLayout:
        """Glyph tile, title and sub-line — the window's identity block."""
        row = QHBoxLayout()
        row.setSpacing(12)
        # Same grid glyph as the panel button that opens this window.
        symbol = QLabel("▦")
        symbol.setObjectName("card_symbol")
        symbol.setFixedSize(44, 44)
        symbol.setAlignment(Qt.AlignCenter)
        row.addWidget(symbol, 0, Qt.AlignTop)

        column = QVBoxLayout()
        column.setSpacing(2)
        title = QLabel(self._t("batch_file", "Batch / File"))
        title.setObjectName("card_title")
        subtitle = QLabel(
            self._t("batch_file_sub", "Create translated subtitles from a recording")
        )
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        column.addWidget(title)
        column.addWidget(subtitle)
        row.addLayout(column, 1)
        return row

    # ── card: translation ────────────────────────────────────────────────
    def _language_card(self) -> QFrame:
        card, box = self._card("⇄", self._t("section_translation", "Translation"))

        source_names = [name for name, _ in SOURCE_LANGUAGES]
        self.source_combo = Dropdown([language_display_name(n) for n in source_names])
        self.source_combo.setCurrentText(
            language_display_name(
                self.settings.source_language
                if self.settings.source_language in source_names
                else source_names[0]
            )
        )
        self.target_combo = Dropdown(
            [language_display_name(n) for n in TARGET_LANGUAGE_NAMES]
        )
        self.target_combo.setCurrentText(
            language_display_name(self.settings.target_language)
        )

        pair = QHBoxLayout()
        pair.setSpacing(10)
        pair.addWidget(
            field(
                self._t("batch_source_language", "Spoken language"),
                self.source_combo,
                symbol="⌁",
            ),
            1,
        )
        pair.addWidget(
            field(
                self._t("batch_target_language", "Subtitle language"),
                self.target_combo,
                symbol="→",
            ),
            1,
        )
        box.addLayout(pair)
        return card

    # ── card: output ─────────────────────────────────────────────────────
    def _output_card(self) -> QFrame:
        card, box = self._card("▤", self._t("batch_output_format", "Output"))

        # Segmented rather than a dropdown: three fixed alternatives, all worth
        # seeing at once — the same shape as the panel's 3-way selectors.
        self.output_segment = SegmentedControl(
            [self._t(key, fallback) for _v, key, fallback in _OUTPUT_FORMATS],
            _DEFAULT_OUTPUT_INDEX,
        )
        self.output_segment.changed.connect(lambda _i: self._sync_bilingual_state())
        # Captioned like every other control in the window. It ran bare under
        # the card's "Output" heading, which left the card's two rows visibly
        # unbalanced — one titled, one not — and made the heading do double
        # duty for the whole card AND this one control.
        box.addWidget(
            field(
                self._t("batch_output_files", "Files"),
                self.output_segment,
                symbol="▤",
            )
        )

        # Was a check box; a two-way segment says what each choice PRODUCES
        # instead of leaving "off" unnamed.
        self.bilingual_segment = SegmentedControl(
            [self._t(key, fallback) for key, fallback in _BILINGUAL_LABELS],
            _DEFAULT_BILINGUAL_INDEX,
        )
        box.addWidget(
            field(
                self._t("subtitles", "Subtitles").rstrip(":"),
                self.bilingual_segment,
                symbol="≋",
            )
        )

        self.more = Expander(self._t("batch_more_settings", "More settings"))
        self.more.body.addWidget(self._more_settings())
        self.more.toggled.connect(self._on_more_toggled)
        box.addWidget(self.more)
        return card

    def _more_settings(self) -> QWidget:
        """Engine + model pickers. Batch always runs the SEGMENTED engine, so
        the real-time-only engines are not offered here — the models shown are
        what the run actually uses."""
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        stt_choices = [
            (name, pid)
            for name, pid in TRANSCRIPTION_PROVIDER_CHOICES
            if pid not in STREAMING_TRANSCRIPTION_PROVIDERS
        ]
        self._stt_ids = [pid for _n, pid in stt_choices]
        effective = _batch_stt_fallback(self.settings.transcription_provider)
        if effective not in self._stt_ids:
            effective = self._stt_ids[0]
        self.stt_provider_combo = Dropdown([n for n, _p in stt_choices])
        self.stt_provider_combo.setCurrentIndex(self._stt_ids.index(effective))
        self.stt_provider_combo.currentIndexChanged.connect(self._on_stt_provider)
        self.stt_model_combo = Dropdown()
        grid.addWidget(
            self._caption(self._t("batch_transcription_model", "Transcription")),
            0, 0, 1, 2,
        )
        grid.addWidget(self.stt_provider_combo, 1, 0)
        grid.addWidget(self.stt_model_combo, 1, 1)

        self._translation_ids = [pid for _n, pid in PROVIDER_CHOICES]
        provider = (
            self.settings.ai_provider
            if self.settings.ai_provider in self._translation_ids
            else self._translation_ids[0]
        )
        self.translation_provider_combo = Dropdown([n for n, _p in PROVIDER_CHOICES])
        self.translation_provider_combo.setCurrentIndex(
            self._translation_ids.index(provider)
        )
        self.translation_provider_combo.currentIndexChanged.connect(
            self._on_translation_provider
        )
        self.translation_model_combo = Dropdown()
        grid.addWidget(
            self._caption(self._t("batch_translation_model", "Translation")),
            2, 0, 1, 2,
        )
        grid.addWidget(self.translation_provider_combo, 3, 0)
        grid.addWidget(self.translation_model_combo, 3, 1)

        self.defaults_btn = QPushButton(self._t("batch_defaults", "Use default"))
        self.defaults_btn.clicked.connect(self._on_defaults)
        grid.addWidget(self.defaults_btn, 4, 1)

        self._fill_models(
            self.stt_model_combo, self._selected_stt_provider(), "transcription"
        )
        self._fill_models(
            self.translation_model_combo,
            self._selected_translation_provider(),
            "translation",
        )
        return holder

    # ── card: the recording ──────────────────────────────────────────────
    def _file_card(self) -> QFrame:
        card, box = self._card("♪", self._t("batch_media_files", "Audio/Video"))
        # Tighter than the other cards: picker, progress and status are one
        # group, and the status line is blank until a run says something.
        box.setSpacing(8)

        # The picker button carries the chosen file's name itself, with a ✕
        # beside it to clear the choice — so no separate "no file selected"
        # line repeating what the button already says.
        row = QHBoxLayout()
        row.setSpacing(6)
        self.pick_btn = QPushButton()
        self.pick_btn.setMinimumHeight(40)
        self.pick_btn.clicked.connect(self._on_pick)
        self.clear_btn = QPushButton("✕")
        self.clear_btn.setFixedSize(40, 40)
        self.clear_btn.clicked.connect(self._on_clear)
        row.addWidget(self.pick_btn, 1)
        row.addWidget(self.clear_btn)
        box.addLayout(row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        # Always on screen, empty when idle — a bar that only appears once a
        # run starts leaves the window jumping a row taller at the moment the
        # user is watching it, and gives no hint that progress is reported.
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(_PROGRESS_H)
        box.addWidget(self.progress)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        self._set_status("", "muted")
        box.addWidget(self.status)
        return card

    # ── action bar ───────────────────────────────────────────────────────
    def _action_bar(self) -> QWidget:
        """Start / Stop and the two follow-ups, pinned below the scroll area."""
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(_PAD, 4, _PAD, _PAD - 2)
        box.setSpacing(8)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.start_btn = QPushButton(self._t("batch_start", "Start"))
        self.start_btn.setObjectName("accent")
        self.start_btn.setMinimumHeight(44)
        self.start_btn.clicked.connect(self._on_start)
        self.start_btn.setEnabled(False)
        self.cancel_btn = QPushButton(self._t("batch_cancel", "Stop processing"))
        self.cancel_btn.setMinimumHeight(44)
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setEnabled(False)
        top.addWidget(self.start_btn, 1)
        top.addWidget(self.cancel_btn, 1)
        box.addLayout(top)

        bottom = QHBoxLayout()
        bottom.setSpacing(8)
        # Always clickable: past runs are in the history viewer whether or not
        # one finished in THIS window, so disabling it hid a working feature.
        # A finished run turns it accent (see _on_finished) to point at it.
        self.history_btn = QPushButton(self._t("batch_open_history", "Show in history"))
        self.history_btn.setMinimumHeight(36)
        self.history_btn.clicked.connect(self._on_open_history)
        self.folder_btn = QPushButton(self._t("batch_open_folder", "Open folder"))
        self.folder_btn.setMinimumHeight(36)
        self.folder_btn.clicked.connect(self._on_open_folder)
        # Nothing to open until a run has written something this session.
        self.folder_btn.setEnabled(False)
        bottom.addWidget(self.history_btn, 1)
        bottom.addWidget(self.folder_btn, 1)
        box.addLayout(bottom)
        return holder

    def _caption(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("field")
        return label

    def _file_button_text(self) -> str:
        """The chosen file's name, or the prompt. A long name is truncated in
        the middle so the start AND the extension stay visible."""
        if not self._input_path:
            return "▤  " + self._t("batch_pick_file", "Choose file…")
        name = os.path.basename(self._input_path)
        if len(name) <= _FILE_NAME_LIMIT:
            return name
        stem, extension = os.path.splitext(name)
        head = max(10, _FILE_NAME_LIMIT - len(extension) - 1)
        return stem[:head].rstrip() + "…" + extension

    def _config_controls(self) -> list[QWidget]:
        """Every control that decides what a run produces.

        Locked together while one is in progress, as in the Tk window
        (``gui/batch_view.py`` ``_batch_option_combos``): the worker was handed
        its arguments at Start, so a change made now cannot reach the running
        job — it would only leave the window describing a job it is not running.
        """
        return [
            self.source_combo,
            self.target_combo,
            self.stt_provider_combo,
            self.stt_model_combo,
            self.translation_provider_combo,
            self.translation_model_combo,
            self.output_segment,
            self.bilingual_segment,
            # Included although Tk leaves it out: it rewrites the six controls
            # above, so leaving it live would move locked ones.
            self.defaults_btn,
        ]

    def _sync_file_row(self) -> None:
        """Picker label, ✕, Start and the run lock, from the current state."""
        running = self.worker.is_running()
        chosen = bool(self._input_path)
        self.pick_btn.setText(self._file_button_text())
        self.pick_btn.setEnabled(not running)
        self.clear_btn.setVisible(chosen)
        self.clear_btn.setEnabled(not running)
        self.start_btn.setEnabled(chosen and not running)
        self.cancel_btn.setEnabled(running)
        for control in self._config_controls():
            control.setEnabled(not running)
        # Re-apply the transcript-only rule the blanket enable above clobbers.
        self._sync_bilingual_state()

    def _natural_height(self) -> int:
        """Height the content wants at ``BATCH_WINDOW_W``.

        Measured from the card column's own layout plus the action bar, not
        from ``adjustSize()`` or the dialog's sizeHint: a word-wrapped label's
        sizeHint reserves a second line it does not use at this width, and the
        surplus was handed to whatever in the column could stretch.
        """
        body_layout = self.body.layout()
        body_layout.activate()
        if body_layout.hasHeightForWidth():
            body = body_layout.totalHeightForWidth(BATCH_WINDOW_W)
        else:
            body = body_layout.totalSizeHint().height()
        return body + self.action_bar.sizeHint().height()

    def _resize_to_content(self) -> None:
        """Fixed width, height from the content — the shape of the Tk window,
        which is not resizable either. Called again whenever the More-settings
        expander changes how tall the cards are.

        The cap comes from the shared rule in gui/window_size.py: past it
        the cards scroll instead of the action bar being pushed off the bottom.
        """
        # Deliver the invalidation the expander posted when it hid or showed
        # its panel: layouts cache heightForWidth, so measuring in the same
        # call would describe the layout as it was one toggle ago.
        QApplication.sendPostedEvents(None, QEvent.LayoutRequest)
        apply_content_size(self, self._natural_height())

    def _set_status(self, text: str, kind: str = "muted") -> None:
        """Status line + its colour. The colour comes from an object name, not
        a widget stylesheet: an id rule in the app sheet would outrank one, and
        this way a theme switch recolours it with everything else."""
        self.status.setText(text)
        self.status.setObjectName(kind)
        self.status.style().unpolish(self.status)
        self.status.style().polish(self.status)

    # ── option handlers ──────────────────────────────────────────────────
    def _on_more_toggled(self, _open: bool) -> None:
        # Queued: showing or hiding a widget invalidates the layout through a
        # POSTED event, and measuring in this same call returns the previous
        # state's numbers — the window then grew when the panel opened but did
        # not shrink again when it closed. Bound to self so a closed window
        # never gets the late call.
        QTimer.singleShot(0, self, self._resize_to_content)

    def _sync_bilingual_state(self) -> None:
        # Transcript-only writes no SRT, so a bilingual SRT is meaningless.
        # Disabled on the widget rather than through set_enabled(), so
        # isEnabled() reports it and Qt greys the segments by propagation.
        self.bilingual_segment.setEnabled(
            self._output_format() != "txt" and not self.worker.is_running()
        )

    def _bilingual_srt(self) -> bool:
        # From the chosen format, not from the segment's enabled state: a run
        # in progress disables it along with every other config control, and
        # reading isEnabled() here would then report "translation only" for a
        # job that is producing exactly the opposite.
        return (
            self._output_format() != "txt"
            and self.bilingual_segment.current_index() == _DEFAULT_BILINGUAL_INDEX
        )

    def _selected_stt_provider(self) -> str:
        return self._stt_ids[self.stt_provider_combo.currentIndex()]

    def _selected_translation_provider(self) -> str:
        return self._translation_ids[self.translation_provider_combo.currentIndex()]

    @staticmethod
    def _fill_models(combo: QComboBox, provider: str, kind: str) -> None:
        blocked = combo.blockSignals(True)
        combo.clear()
        for name, model_id in get_model_choices(provider, kind):
            combo.addItem(name, model_id)
        index = combo.findData(get_default_model(provider, kind))
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(blocked)

    def _on_stt_provider(self, _index: int) -> None:
        self._fill_models(
            self.stt_model_combo, self._selected_stt_provider(), "transcription"
        )

    def _on_translation_provider(self, _index: int) -> None:
        self._fill_models(
            self.translation_model_combo,
            self._selected_translation_provider(),
            "translation",
        )

    def _on_defaults(self) -> None:
        """Reset engines and output to what the app is configured to use."""
        effective = _batch_stt_fallback(self.settings.transcription_provider)
        if effective in self._stt_ids:
            self.stt_provider_combo.setCurrentIndex(self._stt_ids.index(effective))
        if self.settings.ai_provider in self._translation_ids:
            self.translation_provider_combo.setCurrentIndex(
                self._translation_ids.index(self.settings.ai_provider)
            )
        self._on_stt_provider(0)
        self._on_translation_provider(0)
        self.output_segment.set_current_index(_DEFAULT_OUTPUT_INDEX)
        self.bilingual_segment.set_current_index(_DEFAULT_BILINGUAL_INDEX)
        self._sync_bilingual_state()

    def _output_format(self) -> str:
        index = self.output_segment.current_index()
        if not 0 <= index < len(_OUTPUT_FORMATS):
            index = _DEFAULT_OUTPUT_INDEX
        return _OUTPUT_FORMATS[index][0]

    # ── file + run ───────────────────────────────────────────────────────
    def _on_pick(self) -> None:
        media = self._t("batch_media_files", "Audio/Video")
        all_files = self._t("batch_all_files", "All files")
        patterns = " ".join(f"*.{extension}" for extension in _MEDIA_EXTENSIONS)
        path, _ = QFileDialog.getOpenFileName(
            self,
            self._t("batch_pick_file", "Choose file…"),
            "",
            f"{media} ({patterns});;{all_files} (*)",
        )
        if not path:
            return
        self._input_path = path
        self._set_status("")
        self.progress.setValue(0)
        self._sync_file_row()

    def _on_clear(self) -> None:
        self._input_path = ""
        self._set_status("")
        self.progress.setValue(0)
        self._sync_file_row()

    def _on_start(self) -> None:
        if not self._input_path or self.worker.is_running():
            return
        # Both chosen engines need a key; ask now rather than failing inside
        # the worker thread half a file in.
        providers = [self._selected_stt_provider(), self._selected_translation_provider()]
        if not ensure_keys(list(dict.fromkeys(providers)), {}, self):
            return
        self.progress.setValue(0)
        self.worker.start(
            input_path=self._input_path,
            source_language=language_canonical_name(self.source_combo.currentText()),
            target_language=language_canonical_name(self.target_combo.currentText()),
            transcription_provider=self._selected_stt_provider(),
            transcription_model=self.stt_model_combo.currentData(),
            translation_provider=self._selected_translation_provider(),
            translation_model=self.translation_model_combo.currentData(),
            output_format=self._output_format(),
            bilingual_srt=self._bilingual_srt(),
        )
        # After start(), so is_running() already reports the new state.
        self._sync_file_row()

    def _on_cancel(self) -> None:
        self.worker.cancel()
        self.cancel_btn.setEnabled(False)
        self._set_status(self._t("batch_cancelled", "Cancelled"), "status_warn")

    def _on_open_history(self) -> None:
        # Straight to the Batch tab: the run just finished is there, not in the
        # session list the viewer opens on by default.
        if self._panel is not None:
            self._panel.open_history("batch")

    def _on_open_folder(self) -> None:
        if not self._output_path:
            return
        folder = os.path.dirname(self._output_path)
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606 - a folder the user just wrote to
            elif sys.platform == "darwin":
                # env: from a frozen build the bundle's *_LIBRARY_PATH would
                # otherwise leak into the file manager and crash it on a libstdc++
                # mismatch — the same trap the ffmpeg call sidesteps.
                subprocess.Popen(["open", folder], env=external_process_env())  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", folder], env=external_process_env())  # noqa: S603,S607
        except OSError as exc:
            log(f"Opening the output folder failed: {exc}", level="WARNING")

    # ── worker signals (delivered on the GUI thread) ─────────────────────
    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setValue(round(done * 100 / total))
        # No resize: the text is a constant-length counter, and a window that
        # resizes on every segment would twitch for the whole run.
        self._set_status(
            self._t("batch_progress", "Segment {current}/{total}").format(
                current=done, total=total
            )
        )

    def _on_finished(self, path: str) -> None:
        self._sync_file_row()
        if not path:
            # Cancelled: process_file writes nothing in that case.
            self.progress.setValue(0)
            self._set_status(self._t("batch_cancelled", "Cancelled"), "status_warn")
            return
        self._output_path = path
        self.progress.setValue(100)
        self.folder_btn.setEnabled(True)
        # Point at where the finished run can be reopened.
        self.history_btn.setObjectName("accent")
        self.history_btn.style().unpolish(self.history_btn)
        self.history_btn.style().polish(self.history_btn)
        self._set_status(
            self._t("batch_done", "Saved next to your file: {name}").format(
                name=os.path.basename(path)
            ),
            "status_ok",
        )
        # The outcome can wrap to a second line; the run is over, so growing
        # for it costs nothing.
        self._resize_to_content()

    def _on_failed(self, message: str) -> None:
        self._sync_file_row()
        self.progress.setValue(0)
        self._set_status(
            self._t("batch_error", "Failed: {error}").format(error=message),
            "status_error",
        )
        self._resize_to_content()

    # ── ffmpeg ───────────────────────────────────────────────────────────
    def _on_ffmpeg_missing(self) -> None:
        """Anything that is not already a 16 kHz WAV goes through ffmpeg.

        Offer to fetch it once, with consent, instead of leaving the operator
        at a dead end. Windows only — every other platform has a package
        manager, and pulling an unsigned binary there would be worse than the
        instruction to install it.
        """
        self._sync_file_row()
        self.progress.setValue(0)
        if sys.platform == "win32" and self._offer_ffmpeg_download():
            return  # the run restarts itself once ffmpeg is in place
        self._set_status(self._ffmpeg_missing_text(), "status_error")
        self._resize_to_content()

    def _ffmpeg_missing_text(self) -> str:
        """What to say when there is no ffmpeg and no download to offer.

        Naming the file format is only half an answer — on macOS and Linux
        the operator was told what was wrong and nothing about what to do
        next. Where we know the command, it goes in the message.
        """
        from utils.ffmpeg_download import ffmpeg_install_command

        command = ffmpeg_install_command()
        if not command:
            return self._t(
                "batch_ffmpeg_missing",
                "ffmpeg not found — install ffmpeg to process this file format.",
            )
        return self._t(
            "batch_ffmpeg_missing_hint",
            "ffmpeg not found — install it and try again:  {command}",
        ).format(command=command)

    def _offer_ffmpeg_download(self) -> bool:
        """True when a download was started (and the run will resume)."""
        from utils.ffmpeg_download import FFMPEG_DOWNLOAD_MB

        prompt = self._t(
            "batch_ffmpeg_download_prompt",
            "ffmpeg is required to convert this file format. Download it now? "
            "(one time, ~{mb} MB)",
        ).format(mb=FFMPEG_DOWNLOAD_MB)
        if not ask_yes_no(self, "ffmpeg", prompt, translate=self._t):
            return False
        log("BATCH ffmpeg download started")
        self.worker.start_ffmpeg_download()
        self._sync_file_row()
        return True

    def _on_download_progress(self, percent: int) -> None:
        self.progress.setValue(percent)
        self._set_status(
            self._t("batch_ffmpeg_downloading", "Downloading ffmpeg… {percent}%").format(
                percent=percent
            )
        )

    def _on_download_finished(self) -> None:
        self.progress.setValue(0)
        # The download thread emitted this as its last statement and is about
        # to exit; without the join it can still be alive, and _on_start's
        # "already running" guard would silently skip the restart.
        self.worker.join()
        # ffmpeg is in place — run what the user actually asked for.
        self._on_start()

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Closing a window is not a statement about the job (AGENTS.md,
        # 2026-08-05), so ask instead of deciding. Cancelling silently threw
        # away a run that may be twenty minutes in; finishing silently left
        # a thread writing files with no window to say so.
        #
        # Letting it run is safe: the .srt and the history entry are written
        # by batch/processor.py on the worker thread, so the finished run shows
        # up in the history viewer's Batch tab whether or not this window is
        # still open. Only the progress display is lost.
        if self.worker.is_running():
            cancel = ask_yes_no_or_dismiss(
                self,
                self._t("batch_close_title", "A run is still going"),
                self._t(
                    "batch_close_question",
                    "Cancel this run, or leave it working in the background? "
                    "A background run still saves its file and appears in the "
                    "history.",
                ),
                default_yes=False,  # never let Return throw the run away
                yes_text=self._t("batch_close_cancel_run", "Cancel run"),
                no_text=self._t("batch_close_keep_running", "Keep running"),
                translate=self._t,
            )
            # Dismissing the prompt with its own ✕ or Escape is "never mind",
            # not an answer: the window stays open on the running job it was
            # showing. Closing it anyway would make the ✕ a third, unlabelled
            # answer to a question about someone's twenty-minute run.
            if cancel is None:
                event.ignore()
                return
            if cancel:
                self.worker.cancel()
        super().closeEvent(event)

"""The "MinbarLive is already running" dialog.

``main.py`` shows this from its single-instance guard, before any other GUI
exists — which is why it builds its own QApplication and why
``gui/platform_setup.py`` has to run before it rather than before the panel.

It must stay Qt, and not fall back to anything simpler that happens to be in
the standard library: building a Tk window sets the process DPI awareness to
per-monitor **v1** (measured: 0 → 2 through shcore) before Qt gets to ask for
the v2 context it wants, and Qt cannot take it back.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from gui.i18n import load_gui_translations
from gui.icons import app_icon
from gui.theme import apply_theme
from utils.settings import load_settings

DIALOG_W = 500


class AlreadyRunningDialog(QDialog):
    """Accepted == "launch anyway"; rejected == abort, including the ✕."""

    def __init__(self, texts: dict, parent=None):
        super().__init__(parent)
        self._t = texts.get
        self.setWindowTitle(
            self._t("already_running_title", "MinbarLive is already running")
        )
        self.setMinimumWidth(DIALOG_W)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 22, 24, 18)
        outer.setSpacing(18)

        body = QHBoxLayout()
        body.setSpacing(16)
        badge = QLabel("?")
        badge.setObjectName("card_symbol")
        badge.setFixedSize(52, 52)
        badge.setAlignment(Qt.AlignCenter)
        body.addWidget(badge, 0, Qt.AlignTop)
        message = QLabel(
            self._t(
                "already_running_body",
                "MinbarLive is already running or is currently starting up!\n\n"
                "Unless you meant to do this, please shut down the existing "
                "instance before starting a new one.",
            )
        )
        message.setWordWrap(True)
        body.addWidget(message, 1)
        outer.addLayout(body)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        self.cancel_btn = QPushButton(self._t("dlg_cancel", "Cancel"))
        self.cancel_btn.setMinimumHeight(44)
        self.cancel_btn.clicked.connect(self.reject)
        # The safe option answers Enter and Esc: starting a second instance is
        # the thing this dialog exists to talk the user out of.
        self.cancel_btn.setDefault(True)
        self.launch_btn = QPushButton(
            self._t("already_running_launch_anyway", "Launch Anyway")
        )
        self.launch_btn.setObjectName("accent")
        self.launch_btn.setMinimumHeight(44)
        self.launch_btn.clicked.connect(self.accept)
        buttons.addWidget(self.cancel_btn)
        buttons.addWidget(self.launch_btn)
        outer.addLayout(buttons)


def show_already_running_dialog() -> bool:
    """True if the user chose "Launch anyway", False to abort.

    Creates the QApplication that the rest of the launch then reuses —
    ``gui.app.run`` picks up the existing instance, since Qt allows only one
    per process.
    """
    app = QApplication.instance() or QApplication(sys.argv)
    settings = load_settings()
    apply_theme(app, settings.theme_mode)
    icon = app_icon()
    if icon is not None:
        app.setWindowIcon(icon)

    dialog = AlreadyRunningDialog(load_gui_translations(settings.gui_language))
    if icon is not None:
        dialog.setWindowIcon(icon)
    # Above the instance that is already running: without this the warning
    # opens behind that window and reads as the launch having done nothing.
    dialog.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    return dialog.exec() == QDialog.Accepted

"""Themed message and confirm dialogs for the Qt tree.

Qt's ``QMessageBox`` statics draw the platform's own dialog: a blue system
icon, system-coloured chrome that ignores the app theme, and English "OK" /
"Yes" / "No" whatever the GUI language is — and the icon is what plays the
Windows alert sound on every appearance. The Tk tree replaced them long ago
(``WidgetFactoryMixin._alert`` / ``._confirm`` over ``show_message``); this is
that dialog, in Qt.

Same shape as the Tk one: a card with a glyph tile coloured by severity, the
title beside it, the message below, and localized buttons. Return accepts,
Escape cancels.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

# glyph + the object name that colours it, per severity.
_KINDS = {
    "info": ("ℹ", "dialog_icon_info"),
    "warn": ("⚠", "dialog_icon_warn"),
    "error": ("✕", "dialog_icon_error"),
}

_WIDTH = 430


def _t_default(_key: str, fallback: str) -> str:
    return fallback


class MessageDialog(QDialog):
    """OK-only, or Yes/No when ``confirm``. Use the helpers below."""

    def __init__(
        self,
        parent,
        title: str,
        message: str,
        *,
        kind: str = "warn",
        confirm: bool = False,
        default_yes: bool = True,
        translate: Callable[[str, str], str] | None = None,
    ):
        super().__init__(parent)
        t = translate or _t_default
        self.setWindowTitle(title)
        if parent is not None and not parent.windowIcon().isNull():
            self.setWindowIcon(parent.windowIcon())
        # No help button in the title bar, and no resizing a fixed-size card.
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)

        glyph, icon_name = _KINDS.get(kind, _KINDS["warn"])

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)
        card = QFrame()
        card.setObjectName("card")
        box = QVBoxLayout(card)
        box.setContentsMargins(18, 16, 18, 16)
        box.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel(glyph)
        icon.setObjectName(icon_name)
        icon.setFixedSize(44, 44)
        icon.setAlignment(Qt.AlignCenter)
        head.addWidget(icon, 0, Qt.AlignTop)
        heading = QLabel(title)
        heading.setObjectName("heading")
        heading.setWordWrap(True)
        head.addWidget(heading, 1)
        box.addLayout(head)

        self.body = QLabel(message)
        self.body.setWordWrap(True)
        # Selectable: an error dialog often carries the string a user needs to
        # paste into a bug report.
        self.body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        box.addWidget(self.body)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        if confirm:
            self.no_btn = QPushButton(t("dlg_no", "No"))
            self.no_btn.setMinimumHeight(38)
            self.no_btn.clicked.connect(self.reject)
            self.yes_btn = QPushButton(t("dlg_yes", "Yes"))
            self.yes_btn.setObjectName("accent")
            self.yes_btn.setMinimumHeight(38)
            self.yes_btn.clicked.connect(self.accept)
            buttons.addWidget(self.no_btn)
            buttons.addWidget(self.yes_btn)
            default = self.yes_btn if default_yes else self.no_btn
        else:
            self.ok_btn = QPushButton(t("dlg_ok", "OK"))
            self.ok_btn.setObjectName("accent")
            self.ok_btn.setMinimumHeight(38)
            self.ok_btn.clicked.connect(self.accept)
            buttons.addWidget(self.ok_btn)
            default = self.ok_btn
        for name in ("no_btn", "yes_btn", "ok_btn"):
            button = getattr(self, name, None)
            if button is not None:
                button.setMinimumWidth(96)
                # Cleared first, so only the intended default carries it: on a
                # destructive confirm, Return must not press Yes.
                button.setAutoDefault(False)
        default.setAutoDefault(True)
        default.setDefault(True)
        default.setFocus()
        box.addLayout(buttons)

        outer.addWidget(card)
        # Fixed width, height from the message: a long error must not be
        # clipped, and a short one must not sit in an empty box.
        self.setFixedWidth(_WIDTH)
        layout = self.layout()
        layout.activate()
        self.setFixedHeight(layout.totalHeightForWidth(_WIDTH))


def show_message(
    parent,
    title: str,
    message: str,
    *,
    kind: str = "warn",
    translate: Callable[[str, str], str] | None = None,
) -> None:
    MessageDialog(parent, title, message, kind=kind, translate=translate).exec()


def ask_yes_no(
    parent,
    title: str,
    message: str,
    *,
    kind: str = "warn",
    default_yes: bool = True,
    translate: Callable[[str, str], str] | None = None,
) -> bool:
    dialog = MessageDialog(
        parent,
        title,
        message,
        kind=kind,
        confirm=True,
        default_yes=default_yes,
        translate=translate,
    )
    return dialog.exec() == QDialog.Accepted

"""Qt theming built from the shared palette.

Qt styles widgets with stylesheets, so the Tk tree's per-widget registries
(``_cards``, ``_labels``, ``_buttons``, ``_combos``, ... each re-coloured by
hand on every theme switch) collapse into one stylesheet string applied to the
application. Changing theme is then a single ``apply_theme`` call.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from gui_qt.palette import palette


def qcolor(hex_color: str, alpha: int | None = None) -> QColor:
    """QColor from a palette hex string, optionally with an alpha 0-255."""
    c = QColor(hex_color)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def stylesheet(theme_mode: str) -> str:
    """Application-wide stylesheet for ``theme_mode``."""
    c = palette(theme_mode)
    return f"""
QWidget {{
    background-color: {c["app_bg"]};
    color: {c["text"]};
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
}}
QFrame#card, QFrame#panel {{
    background-color: {c["card"]};
    border: 1px solid {c["border"]};
    border-radius: 14px;
}}
/* Labels must not paint their own background: the QWidget rule above would
   otherwise stamp an app_bg rectangle around every label sitting on a card. */
QLabel {{ background: transparent; }}
QLabel#muted {{ color: {c["muted"]}; }}
QLabel#heading {{ font-size: 15px; font-weight: 600; }}

QPushButton {{
    background-color: {c["button"]};
    color: {c["text"]};
    border: none;
    border-radius: 14px;
    padding: 10px 16px;
}}
QPushButton:hover {{ background-color: {c["button_hover"]}; }}
QPushButton#accent {{ background-color: {c["accent"]}; color: #ffffff; font-weight: 600; }}
QPushButton#accent:hover {{ background-color: {c["accent_hover"]}; }}
QPushButton#danger {{ background-color: {c["danger"]}; color: #ffffff; font-weight: 600; }}
QPushButton#danger:hover {{ background-color: {c["danger_hover"]}; }}
/* Disabled must win over the #accent / #danger colours, so it comes last and
   names the ids explicitly — otherwise a disabled Stop still reads as live. */
QPushButton:disabled,
QPushButton#accent:disabled,
QPushButton#danger:disabled {{
    background-color: {c["button"]};
    color: {c["muted"]};
}}

QComboBox, QLineEdit, QSpinBox {{
    background-color: {c["entry"]};
    border: 1px solid {c["entry_border"]};
    border-radius: 12px;
    padding: 8px 12px;
    min-height: 20px;
}}
QComboBox:focus, QLineEdit:focus {{ border-color: {c["accent"]}; }}
QComboBox QAbstractItemView {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    selection-background-color: {c["accent"]};
    selection-color: #ffffff;
    outline: none;
}}

QPlainTextEdit, QTextEdit {{
    background-color: {c["log_bg"]};
    color: {c["log_text"]};
    border: 1px solid {c["border"]};
    border-radius: 12px;
}}

QCheckBox::indicator, QRadioButton::indicator {{ width: 18px; height: 18px; }}
QCheckBox::indicator:checked {{ background-color: {c["accent"]}; border-radius: 4px; }}
QCheckBox::indicator:unchecked {{
    background-color: {c["entry"]};
    border: 1px solid {c["entry_border"]};
    border-radius: 4px;
}}

QProgressBar {{
    background-color: {c["panel_soft"]};
    border: none;
    border-radius: 6px;
    height: 10px;
    text-align: center;
}}
QProgressBar::chunk {{ background-color: {c["accent"]}; border-radius: 6px; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {c["border"]};
    border-radius: 5px;
    min-height: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
"""


def apply_theme(app, theme_mode: str) -> None:
    """Apply ``theme_mode`` to the whole application."""
    app.setStyleSheet(stylesheet(theme_mode))

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
/* Widgets that draw no surface of their own must not paint a background: the
   QWidget rule above would otherwise stamp an app_bg rectangle around every
   label, checkbox and plain layout container sitting on a card. */
QLabel, QCheckBox, QRadioButton {{ background: transparent; }}
QFrame#card QWidget {{ background: transparent; }}
QFrame#card QComboBox, QFrame#card QLineEdit, QFrame#card QSpinBox,
QFrame#card QPushButton {{ background-color: {c["entry"]}; }}
QFrame#card QPushButton {{ background-color: {c["button"]}; }}
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

/* Segmented control (CTkSegmentedButton equivalent). Corner rounding comes
   from the `seg` property so the row reads as one pill, and the rules are
   placed after the generic QPushButton block so they win. */
QPushButton#segment {{
    background-color: {c["button"]};
    color: {c["text"]};
    border: none;
    border-radius: 0;
    padding: 9px 14px;
    font-weight: 600;
}}
QPushButton#segment:hover {{ background-color: {c["button_hover"]}; }}
QPushButton#segment:checked {{ background-color: {c["accent"]}; color: #ffffff; }}
QPushButton#segment[seg="first"] {{ border-top-left-radius: 12px; border-bottom-left-radius: 12px; }}
QPushButton#segment[seg="last"] {{ border-top-right-radius: 12px; border-bottom-right-radius: 12px; }}
QPushButton#segment[seg="only"] {{ border-radius: 12px; }}
QFrame#card QPushButton#segment {{ background-color: {c["button"]}; }}
QFrame#card QPushButton#segment:checked {{ background-color: {c["accent"]}; }}

/* Stepper (-/+) pairs, as used for font size and scroll speed. */
QPushButton#stepper {{
    background-color: {c["button"]};
    border-radius: 14px;
    font-size: 18px;
    font-weight: 600;
    padding: 0;
}}
QPushButton#stepper:hover {{ background-color: {c["button_hover"]}; }}
QFrame#card QPushButton#stepper {{ background-color: {c["button"]}; }}
QLabel#stepper_value, QLabel#slider_value {{
    font-size: 15px;
    font-weight: 600;
    background: transparent;
}}

QSlider::groove:horizontal {{
    background: {c["button"]};
    height: 6px;
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{ background: {c["accent"]}; border-radius: 3px; }}
QSlider::handle:horizontal {{
    background: {c["accent"]};
    width: 16px;
    margin: -6px 0;
    border-radius: 8px;
}}
QSlider::handle:horizontal:hover {{ background: {c["accent_hover"]}; }}

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

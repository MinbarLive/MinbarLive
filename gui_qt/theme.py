"""Qt theming built from the shared palette.

Qt styles widgets with stylesheets, so the Tk tree's per-widget registries
(``_cards``, ``_labels``, ``_buttons``, ``_combos``, ... each re-coloured by
hand on every theme switch) collapse into one stylesheet string applied to the
application. Changing theme is then a single ``apply_theme`` call.

Two things a stylesheet alone cannot do, so they live in a ``QProxyStyle``
here (``_ControlStyle``):

* **Check marks.** A stylesheet can only put a *picture* inside a checkbox
  indicator (``image: url(...)``), and Qt resolves that against the filesystem
  or a compiled ``.qrc`` — neither of which we want to ship for two 20 px
  glyphs that must also recolour per theme. Without a picture the indicator is
  a flat filled square, which is what the accent-coloured boxes were.
* **Indicator geometry**, so the box is 20 px in both themes regardless of the
  platform style's own metric.

The stylesheet deliberately declares no ``QCheckBox::indicator`` rule: as soon
as one exists, ``QStyleSheetStyle`` paints the indicator itself and never
reaches the proxy below it.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QProxyStyle, QStyle

from gui_qt.palette import palette

# Indicator edge length in logical px, for both check boxes and radio buttons.
_INDICATOR_PX = 20


def qcolor(hex_color: str, alpha: int | None = None) -> QColor:
    """QColor from a palette hex string, optionally with an alpha 0-255."""
    c = QColor(hex_color)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


class _ControlStyle(QProxyStyle):
    """Paints check boxes and radio buttons with a real check mark / dot."""

    def __init__(self) -> None:
        super().__init__()
        self._c = palette("light")

    def set_colors(self, colors: dict[str, str]) -> None:
        self._c = colors

    def pixelMetric(self, metric, option=None, widget=None):  # noqa: N802 - Qt API
        if metric in (
            QStyle.PM_IndicatorWidth,
            QStyle.PM_IndicatorHeight,
            QStyle.PM_ExclusiveIndicatorWidth,
            QStyle.PM_ExclusiveIndicatorHeight,
        ):
            return _INDICATOR_PX
        return super().pixelMetric(metric, option, widget)

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        if element == QStyle.PE_IndicatorCheckBox:
            self._draw_check(option, painter)
            return
        if element == QStyle.PE_IndicatorRadioButton:
            self._draw_radio(option, painter)
            return
        super().drawPrimitive(element, option, painter, widget)

    # ── indicator painting ───────────────────────────────────────────────
    def _fill_pen(self, option) -> tuple[QColor, QColor, QColor]:
        """(fill, border, mark) for the indicator's current state."""
        on = bool(option.state & QStyle.State_On)
        enabled = bool(option.state & QStyle.State_Enabled)
        hover = bool(option.state & QStyle.State_MouseOver)
        if not enabled:
            return qcolor(self._c["button"]), qcolor(self._c["border"]), qcolor(
                self._c["muted"]
            )
        if on:
            key = "accent_hover" if hover else "accent"
            return qcolor(self._c[key]), qcolor(self._c[key]), QColor("#ffffff")
        return (
            qcolor(self._c["entry"]),
            qcolor(self._c["accent"] if hover else self._c["entry_border"]),
            QColor("#ffffff"),
        )

    def _draw_check(self, option, painter: QPainter) -> None:
        fill, border, mark = self._fill_pen(option)
        rect = option.rect.adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(border, 1.4))
        painter.drawRoundedRect(rect, 5, 5)
        if option.state & QStyle.State_On:
            w, h = rect.width(), rect.height()
            # Proportional so the tick keeps its shape if the metric changes.
            points = QPolygonF(
                [
                    QPointF(rect.left() + w * 0.24, rect.top() + h * 0.52),
                    QPointF(rect.left() + w * 0.43, rect.top() + h * 0.72),
                    QPointF(rect.left() + w * 0.78, rect.top() + h * 0.29),
                ]
            )
            pen = QPen(mark, max(2.0, w * 0.16))
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawPolyline(points)
        elif option.state & QStyle.State_NoChange:
            painter.setPen(QPen(qcolor(self._c["muted"]), 2.0))
            y = rect.center().y()
            painter.drawLine(
                rect.left() + rect.width() // 4, y, rect.right() - rect.width() // 4, y
            )
        painter.restore()

    def _draw_radio(self, option, painter: QPainter) -> None:
        fill, border, mark = self._fill_pen(option)
        rect = option.rect.adjusted(1, 1, -1, -1)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(QBrush(fill))
        painter.setPen(QPen(border, 1.4))
        painter.drawEllipse(rect)
        if option.state & QStyle.State_On:
            painter.setBrush(QBrush(mark))
            painter.setPen(Qt.NoPen)
            inset = max(3, rect.width() // 3)
            painter.drawEllipse(rect.adjusted(inset, inset, -inset, -inset))
        painter.restore()


_STYLE: _ControlStyle | None = None
# The palette the app is currently themed with. Widgets that paint themselves
# (the dropdown chevron, the input meter) read it instead of each keeping its
# own copy in sync.
_CURRENT: dict[str, str] = palette("light")


def current_colors() -> dict[str, str]:
    """The palette of the theme currently applied to the application."""
    return _CURRENT


def stylesheet(theme_mode: str) -> str:
    """Application-wide stylesheet for ``theme_mode``."""
    c = palette(theme_mode)
    return f"""
/* Deliberately NO background on the bare QWidget rule. Qt gives every widget
   matching a background rule WA_StyledBackground, so `QWidget {{ background }}`
   stamps a rectangle behind every label and every plain layout container — and
   the descendant overrides needed to undo that (`QFrame#card QWidget`) then
   outrank `QPushButton#accent`, quietly greying out Start and the status pill.
   Only real surfaces get a background; everything else shows its parent. */
QWidget {{
    color: {c["text"]};
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
}}
QMainWindow, QDialog, QMessageBox {{ background-color: {c["app_bg"]}; }}
QWidget#sidebar {{ background-color: {c["sidebar"]}; }}
QFrame#card, QFrame#panel {{
    background-color: {c["card"]};
    border: 1px solid {c["border"]};
    border-radius: 14px;
}}
QFrame#mini {{
    background-color: {c["panel_soft"]};
    border: none;
    border-radius: 12px;
}}
QLabel#muted {{ color: {c["muted"]}; }}
QLabel#heading {{ font-size: 15px; font-weight: 600; }}
QLabel#card_title {{ font-size: 15px; font-weight: 700; }}
QLabel#field {{ font-size: 13px; font-weight: 600; }}
QLabel#section {{ font-size: 14px; font-weight: 700; }}
QLabel#value {{ font-size: 17px; font-weight: 700; }}
QLabel#warning_text {{ color: {c["warning"]}; font-weight: 600; }}
QLabel#hero {{ font-size: 17px; font-weight: 700; }}

/* Warning callout: bordered, warning-coloured — the wizard's provider caveats
   and the AI-accuracy disclaimer, which must not read as another grey note. */
QFrame#warning {{
    background: transparent;
    border: 2px solid {c["warning"]};
    border-radius: 12px;
}}

/* Status pills. */
QLabel#pill_running {{
    background-color: {c["accent_soft"]};
    color: {c["accent"]};
    border-radius: 14px;
    padding: 7px 14px;
    font-size: 14px;
    font-weight: 700;
}}
QLabel#pill_stopped {{
    background-color: {c["danger_soft"]};
    color: {c["danger"]};
    border-radius: 14px;
    padding: 7px 14px;
    font-size: 14px;
    font-weight: 700;
}}

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
QPushButton#big {{ font-size: 16px; font-weight: 700; border-radius: 18px; padding: 14px 16px; }}
QPushButton#icon {{ border-radius: 14px; padding: 0; font-size: 17px; }}
QPushButton#link {{
    background: transparent;
    color: {c["accent"]};
    text-align: left;
    padding: 4px 0;
}}
QPushButton#link:hover {{ background: transparent; color: {c["accent_hover"]}; }}
QPushButton#row {{ text-align: left; padding: 8px 12px; border-radius: 12px; }}
/* Disabled must win over the #accent / #danger colours, so it comes last and
   names the ids explicitly — otherwise a disabled Stop still reads as live. */
QPushButton:disabled,
QPushButton#accent:disabled,
QPushButton#danger:disabled,
QPushButton#big:disabled {{
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
QComboBox:disabled, QLineEdit:disabled {{ color: {c["muted"]}; }}
/* The native drop-down button is a sunken bevelled box with a raised arrow —
   the "1998" frame. Flattening it removes the arrow along with the bevel, and
   the usual CSS replacement (a zero-sized box with transparent side borders)
   degenerates to a filled rectangle under a QProxyStyle. So the chevron is
   painted by ``gui_qt.widgets.Dropdown`` instead; here it is only suppressed. */
QComboBox::drop-down {{
    subcontrol-origin: padding;
    subcontrol-position: center right;
    width: 26px;
    border: none;
    background: transparent;
}}
QComboBox QAbstractItemView {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 12px;
    selection-background-color: {c["accent"]};
    selection-color: #ffffff;
    padding: 4px;
    outline: none;
}}
QComboBox QAbstractItemView::item {{ min-height: 26px; padding: 4px 8px; }}

QPlainTextEdit, QTextEdit {{
    background-color: {c["log_bg"]};
    color: {c["log_text"]};
    border: 1px solid {c["border"]};
    border-radius: 12px;
    padding: 6px;
}}
QPlainTextEdit#log {{ font-family: "Consolas", "Menlo", monospace; font-size: 12px; }}

QListWidget {{
    background-color: {c["panel"]};
    border: 1px solid {c["border"]};
    border-radius: 12px;
    padding: 4px;
    outline: none;
}}
QListWidget::item {{
    background-color: {c["card"]};
    border: 1px solid {c["border"]};
    border-radius: 10px;
    padding: 8px 10px;
    margin: 3px 2px;
    color: {c["text"]};
}}
QListWidget::item:hover {{ background-color: {c["panel_soft"]}; }}
QListWidget::item:selected {{
    background-color: {c["accent_soft"]};
    border: 1px solid {c["accent"]};
    color: {c["text"]};
}}

QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QSplitter::handle {{ background: transparent; width: 10px; }}

/* Segmented control (CTkSegmentedButton equivalent). Corner rounding comes
   from the `seg` property so the row reads as one pill, and the rules are
   placed after the generic QPushButton block so they win. */
QPushButton#segment {{
    background-color: {c["button"]};
    color: {c["text"]};
    border: none;
    border-radius: 0;
    padding: 9px 12px;
    font-weight: 600;
}}
QPushButton#segment:hover {{ background-color: {c["button_hover"]}; }}
QPushButton#segment:checked {{ background-color: {c["accent"]}; color: #ffffff; }}
QPushButton#segment:disabled {{ color: {c["muted"]}; }}
QPushButton#segment[seg="first"] {{ border-top-left-radius: 12px; border-bottom-left-radius: 12px; }}
QPushButton#segment[seg="last"] {{ border-top-right-radius: 12px; border-bottom-right-radius: 12px; }}
QPushButton#segment[seg="only"] {{ border-radius: 12px; }}

/* Tab strip (history viewer). */
QPushButton#tab {{
    background: transparent;
    border: none;
    border-radius: 12px;
    padding: 9px 18px;
    font-weight: 600;
    color: {c["muted"]};
}}
QPushButton#tab:hover {{ background-color: {c["panel_soft"]}; color: {c["text"]}; }}
QPushButton#tab:checked {{ background-color: {c["accent"]}; color: #ffffff; }}

/* Stepper (-/+) pairs, as used for font size and scroll speed. */
QPushButton#stepper {{
    background-color: {c["button"]};
    border-radius: 14px;
    font-size: 18px;
    font-weight: 600;
    padding: 0;
}}
QPushButton#stepper:hover {{ background-color: {c["button_hover"]}; }}
QLabel#stepper_value, QLabel#slider_value {{
    font-size: 15px;
    font-weight: 600;
    background: transparent;
}}

QSlider {{ background: transparent; }}
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
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {c["border"]};
    border-radius: 5px;
    min-width: 30px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QToolTip {{
    background-color: {c["panel"]};
    color: {c["text"]};
    border: 1px solid {c["border"]};
    padding: 6px;
}}
"""


def apply_theme(app, theme_mode: str) -> None:
    """Apply ``theme_mode`` to the whole application."""
    global _STYLE, _CURRENT
    colors = palette(theme_mode)
    _CURRENT = colors
    if _STYLE is None:
        _STYLE = _ControlStyle()
        _STYLE.set_colors(colors)
        # Installed once: re-setting the style rebuilds every widget's palette
        # and would undo the stylesheet below on a theme switch.
        app.setStyle(_STYLE)
    else:
        _STYLE.set_colors(colors)
    app.setStyleSheet(stylesheet(theme_mode))
    for widget in app.allWidgets():
        widget.update()

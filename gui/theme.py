"""Theming: one stylesheet string, built from ``gui/palette.py`` and applied to
the whole application, so changing theme is a single ``apply_theme`` call.

Don't reintroduce per-widget colour registries — lists of every card, label,
button and combo, each re-coloured by hand on every theme switch. That is what
this replaced, and every widget added without being registered was a bug that
only appeared after a theme switch.

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

from gui.fonts import arabic_families, mono_families, ui_families
from gui.palette import palette

# Indicator edge length in logical px, for both check boxes and radio buttons.
_INDICATOR_PX = 20

# Base UI text size. 9.75pt == 13px at Qt's 96 logical DPI — the size the
# stylesheet used to carry. See _apply_app_font for why it is points.
_BASE_POINT_SIZE = 9.75


def _soft(hex_color: str, alpha: float = 0.18) -> str:
    """A translucent wash of ``hex_color`` for pill fills.

    The palette ships ``accent_soft``/``danger_soft`` but no warning twin, and
    a fill that composites over whatever card is behind it works in both themes
    without adding a second constant to keep in step.
    """
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha:.2f})"


def app_families() -> list[str]:
    """Every family the interface needs: this platform's UI stack, then
    whatever else it takes to cover Arabic.

    The panel itself speaks Arabic (``data/translations/gui/ar.json``) and
    every language selector lists native names, so an Arabic-capable family
    belongs in the base stack rather than being left to Qt's per-character
    substitution. On Windows the two stacks are the same list and this is a
    no-op — Segoe UI covers both.
    """
    families = ui_families()
    return families + [f for f in arabic_families() if f not in families]


def _css_families() -> str:
    """``app_families`` as a CSS ``font-family`` value.

    No generic ``sans-serif`` tail. Qt's stylesheet parser does not implement
    the CSS generic families — it hands the name to the font matcher like any
    other — so on macOS it was a request for a family called "Sans-serif",
    which made Qt build its whole alias table hunting for it and then say so:
    "Populating font family aliases took 134 ms. Replace uses of missing font
    family "Sans-serif" with one that exists to avoid this cost." The stack in
    front of it is already filtered to families this machine has (gui/fonts.py).
    """
    return ", ".join(f'"{f}"' for f in app_families())


def _css_mono_families() -> str:
    """The log panel's monospace stack as a CSS ``font-family`` value.

    Same rule, same reason: no ``monospace`` tail, and the families come from
    gui/fonts.py so a Windows name is not asked for on a Mac.
    """
    return ", ".join(f'"{f}"' for f in mono_families())


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

    def styleHint(self, hint, option=None, widget=None, data=None):  # noqa: N802
        """Force the plain, styled drop-down popup on every platform.

        The macOS style answers both of these with 1, and Qt then opens a
        combo as a NATIVE NSMenu: unthemed (a white list over the dark panel),
        positioned so the current item sits under the pointer rather than
        below the box, carrying a check mark we never asked for — and, because
        a menu-style popup ignores ``maxVisibleItems``, as long as the list
        happens to be. None of that is styleable from here; it stops being
        native instead. A KDE/Adwaita desktop style can answer the same way,
        so the override is not guarded by platform.
        """
        if hint in (QStyle.SH_ComboBox_Popup, QStyle.SH_ComboBox_UseNativePopup):
            return 0
        return super().styleHint(hint, option, widget, data)

    def subControlRect(self, control, option, sub_control, widget=None):  # noqa: N802
        """Open a drop-down list BELOW its box, not over it.

        Refusing the native popup (``styleHint`` above) is only half of it: the
        list's rectangle still comes from the platform style, and the macOS one
        returns a rect deliberately placed OVER the box, so the current item
        sits under the pointer — a popup button's behaviour, which is what put
        the first item on top of the closed box's own text. Every other style
        answers ``option.rect`` here (QCommonStyle), and Qt then maps its
        bottom-left to open the list underneath. So this is the Windows and
        Linux answer unchanged, applied everywhere.
        """
        if control == QStyle.CC_ComboBox and sub_control == (
            QStyle.SC_ComboBoxListBoxPopup
        ):
            return option.rect
        return super().subControlRect(control, option, sub_control, widget)

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
/* No font-size here either — it is set as the application font in
   apply_theme(). A stylesheet "font-size: Npx" leaves QFont.pointSize() at -1,
   and QComboBox::showPopup feeds the combo's point size straight back into
   QFont::setPointSize, which warns on every dropdown open. */
/* The family list is this platform's, from gui/fonts.py: naming families
   that do not exist here makes Qt build its alias table on the first layout
   and print "Replace uses of missing font family ..." for the privilege. */
QWidget {{
    color: {c["text"]};
    font-family: {_css_families()};
}}
QMainWindow, QDialog, QMessageBox {{ background-color: {c["app_bg"]}; }}
/* A secondary window presented as an in-app panel (gui/modal_host.py). It
   has no titlebar there, so it needs an edge of its own to read as a panel
   sitting ON the dimmed app rather than a hole cut into it.
   The edge is drawn by the surface BEHIND the panel, not by the panel: a
   QDialog takes its background through the palette, which fills the whole
   rect and ignores border-radius, so these two rules on the dialog itself
   never rendered. The panel goes transparent and ModalHost._add_surface puts
   a QFrame under it, which rounds and antialiases the ordinary way. */
QDialog#modal_panel {{ background: transparent; }}
QFrame#panel_surface {{
    background-color: {c["app_bg"]};
    border: 1px solid {c["border"]};
    border-radius: 14px;
}}
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
/* Status lines that report an outcome (the batch run). An object name rather
   than a per-widget stylesheet, which an id rule here would outrank anyway,
   and which would keep its old colour through a theme switch. */
QLabel#status_ok {{ color: {c["accent"]}; }}
QLabel#status_warn {{ color: {c["warning"]}; }}
QLabel#status_error {{ color: {c["danger"]}; }}
QLabel#heading {{ font-size: 15px; font-weight: 600; }}
/* Card headers: a big title beside a rounded accent tile holding the section's
   glyph — the Tk look. The tile is a QLabel with a background, which only
   paints because an id rule gives it WA_StyledBackground. */
QLabel#card_title {{ font-size: 20px; font-weight: 700; }}
QLabel#card_symbol {{
    background-color: {c["panel_soft"]};
    color: {c["accent"]};
    border-radius: 15px;
    font-size: 19px;
    font-weight: 700;
}}
QLabel#card_arrow {{ color: {c["muted"]}; font-size: 17px; }}
/* Severity glyph of a themed message dialog (gui/dialogs.py) — the same
   tile as a card's symbol, tinted by what the dialog is saying. The colours
   come after the shared block so they win on equal specificity. */
QLabel#dialog_icon_info, QLabel#dialog_icon_warn, QLabel#dialog_icon_error {{
    background-color: {c["panel_soft"]};
    border-radius: 15px;
    font-size: 20px;
    font-weight: 700;
}}
QLabel#dialog_icon_info {{ color: {c["accent"]}; }}
QLabel#dialog_icon_warn {{ color: {c["warning"]}; }}
QLabel#dialog_icon_error {{ color: {c["danger"]}; }}
QLabel#field {{ font-size: 13px; font-weight: 600; }}
QLabel#section {{ font-size: 14px; font-weight: 700; }}
QLabel#value {{ font-size: 17px; font-weight: 700; }}
QLabel#warning_text {{ color: {c["warning"]}; font-weight: 600; }}
QLabel#hero {{ font-size: 17px; font-weight: 700; }}
/* Onboarding chrome: the centred welcome title, and the sub-lines/field
   captions inside each step card (a touch larger than the generic muted rule,
   matching the wizard the users already know). */
QLabel#wizard_title {{ font-size: 22px; font-weight: 700; }}
QLabel#wizard_sub {{ color: {c["muted"]}; font-size: 14px; }}

/* "A newer release exists" strip, between the panel header and the cards.
   Accent-soft rather than a warning colour: it is an offer, not a problem. */
/* The outer spacing is a margin rather than a layout's, so that while the
   banner is hidden it takes no room at all. */
QFrame#update_banner {{
    background-color: {c["accent_soft"]};
    border: none;
    border-radius: 14px;
    margin: 0px 18px 4px 18px;
}}
QLabel#update_text {{ color: {c["accent"]}; font-size: 13px; font-weight: 700; }}
QPushButton#banner_close {{
    background: transparent;
    color: {c["accent"]};
    border-radius: 14px;
    padding: 0;
}}
QPushButton#banner_close:hover {{ background-color: {c["accent"]}; color: #ffffff; }}

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
/* Shown while a Start is in flight. The provider handshake can run for tens of
   seconds, and "Stopped" for that whole time reads as "the button did
   nothing". Amber, not the accent: green for "connecting" and green again for
   "running" is one glance too many to tell apart. */
QLabel#pill_connecting {{
    background-color: {_soft(c["warning"])};
    color: {c["warning"]};
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
/* The ✕ an in-app panel floats in its top-right corner, standing in for the
   titlebar close button it does not have. */
QPushButton#panel_close {{
    background-color: {c["panel_soft"]};
    border-radius: 15px;
    padding: 0;
    font-size: 14px;
    font-weight: 700;
}}
QPushButton#panel_close:hover {{ background-color: {c["danger"]}; color: #ffffff; }}
/* Small inline button (the input meter's Test/Stop), sized to its label. */
QPushButton#compact {{ padding: 6px 10px; border-radius: 12px; font-size: 12px; }}
QPushButton#link {{
    background: transparent;
    color: {c["accent"]};
    text-align: left;
    padding: 4px 0;
}}
QPushButton#link:hover {{ background: transparent; color: {c["accent_hover"]}; }}
QPushButton#row {{ text-align: left; padding: 8px 12px; border-radius: 12px; }}
/* Expander header (subtitle appearance): a full-width, left-aligned toggle. */
QPushButton#expander {{
    text-align: left;
    padding: 9px 14px;
    border-radius: 12px;
    font-weight: 700;
}}
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
/* Hover feedback: a dropdown should look clickable before it is clicked.
   ":enabled" so a greyed-out combo stays inert. */
QComboBox:enabled:hover {{
    background-color: {c["panel_soft"]};
    border-color: {c["muted"]};
}}
/* Focus must outrank hover, or pointing at the focused combo erases its ring.
   Qt follows CSS specificity, so the two-pseudo-state hover rule above beats a
   plain ":focus" — hence the ":enabled:focus" twin, which ties on specificity
   and wins by coming later. */
QComboBox:focus, QComboBox:enabled:focus, QLineEdit:focus {{
    border-color: {c["accent"]};
}}
QComboBox:disabled, QLineEdit:disabled {{ color: {c["muted"]}; }}
/* The native drop-down button is a sunken bevelled box with a raised arrow —
   the "1998" frame. Flattening it removes the arrow along with the bevel, and
   the usual CSS replacement (a zero-sized box with transparent side borders)
   degenerates to a filled rectangle under a QProxyStyle. So the chevron is
   painted by ``gui.widgets.Dropdown`` instead; here it is only suppressed. */
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
/* The popup needs its own hover/selection rules. Declaring ::item at all makes
   QStyleSheetStyle paint the rows, and it then ignores the view's
   selection-background-color — which is why the open list had no highlight of
   any kind under the pointer. */
QComboBox QAbstractItemView::item {{
    min-height: 26px;
    padding: 4px 8px;
    border-radius: 8px;
}}
QComboBox QAbstractItemView::item:hover {{
    background-color: {c["accent_soft"]};
    color: {c["text"]};
}}
QComboBox QAbstractItemView::item:selected {{
    background-color: {c["accent"]};
    color: #ffffff;
}}

QPlainTextEdit, QTextEdit {{
    background-color: {c["log_bg"]};
    color: {c["log_text"]};
    border: 1px solid {c["border"]};
    border-radius: 12px;
    padding: 6px;
}}
QPlainTextEdit#log {{ font-family: {_css_mono_families()}; font-size: 12px; }}

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


def _apply_app_font(app) -> None:
    """Set the base UI font as a real point size.

    The size lived in the stylesheet as ``font-size: 13px`` until a pixel size
    leaves ``QFont.pointSize()`` at -1, and ``QComboBox::showPopup`` passes the
    combo's point size back into ``QFont::setPointSize`` — one
    "Point size <= 0 (-1)" warning per dropdown opened. Setting it as the
    application font keeps the rendered size identical (Qt converts points at
    96 logical DPI, so 9.75pt is exactly the 13px it replaces, with high-DPI
    scaling applied on top as before) while giving every widget a valid point
    size. Per-widget sizes elsewhere in the sheet stay in px: none of them are
    combo boxes.
    """
    font = app.font()
    # Per platform, and filtered to what is installed (gui/fonts.py). The
    # symbol family trails the text ones as a glyph fallback: the field
    # captions and card badges carry symbols (▣ ◉ ⌁ ⇶ ≋) the text families do
    # not cover, and Qt substitutes per character rather than per label.
    font.setFamilies(app_families())
    font.setPointSizeF(_BASE_POINT_SIZE)
    app.setFont(font)


def apply_theme(app, theme_mode: str) -> None:
    """Apply ``theme_mode`` to the whole application."""
    global _STYLE, _CURRENT
    _apply_app_font(app)
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

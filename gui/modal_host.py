"""In-app presentation of the secondary windows (integrated window style).

With ``Settings.window_style == "integrated"`` the control panel's secondary
windows (settings, history, batch, announcement) do not open as separate OS
windows: they are reparented *into* the control panel as plain child widgets,
centred over its content with a dim layer behind them, and closed by Esc, the
floating ✕ or a click on the dim.

Why reparenting rather than the Tk tree's borderless-toplevel trick: a child
widget needs no per-window alpha and no stacking cooperation from the window
manager — the dim is painted into our own window's back buffer, over the
sibling widgets, which is something Qt does identically on every platform it
runs on. That is exactly what made integrated mode Windows-only under Tk (X11
without a compositor ignores per-window alpha, so the dim rendered solid black
and borderless panels would not stack; PR #25 r6), and it is why this one is
offered everywhere.

The panels stay the same ``QDialog`` subclasses, so every window-building
module runs unchanged — only where the widget lives differs. A window that is
content-sized (see ``gui/window_size.py``) is told how much room it has
through ``host_max_size`` and re-measures itself; the rest are simply clamped.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QObject, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QFrame, QPushButton, QWidget

# A panel may occupy at most this fraction of the control panel (per axis).
# The remainder is the dim margin that makes it read as a panel *over* the app
# rather than a replacement for it.
PANEL_FRACTION = 0.9

# Opacity of the black dim layer, 0-255. 150 ≈ the Tk host's 60 %.
DIM_ALPHA = 150

# Never shrink a panel below this: under it nothing inside is usable, and the
# control panel's own 420×420 minimum keeps us near this regime only there.
MIN_PANEL_W = 300
MIN_PANEL_H = 260

# The floating ✕: its size, and its inset from the panel's top-right corner.
_CLOSE_BOX = 30
_CLOSE_INSET = 12


def clamped_panel_size(
    design: QSize, main: QSize, fraction: float = PANEL_FRACTION
) -> QSize:
    """The size a panel gets inside a main window of ``main``.

    The size it asks for, clamped to ``fraction`` of the main window per axis,
    with a usability floor. A pure function so the rule is testable without a
    display.
    """
    width = min(design.width(), int(main.width() * fraction))
    height = min(design.height(), int(main.height() * fraction))
    return QSize(max(MIN_PANEL_W, width), max(MIN_PANEL_H, height))


class _Backdrop(QWidget):
    """The dim layer: semi-transparent black over the panel's own content.

    Paints rather than styles: a stylesheet background is composited the same
    way, but an ``rgba()`` there would be one more rule competing with the
    app sheet's for every plain ``QWidget``.
    """

    clicked = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        # Deliberately NOT WA_OpaquePaintEvent: Qt must keep painting the
        # widgets underneath into the same back buffer for the dim to have
        # anything to dim.
        self.setCursor(Qt.ArrowCursor)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, DIM_ALPHA))

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Swallowed either way: the widgets below are dimmed and must not be
        # reachable, which is the whole point of a modal backdrop.
        event.accept()
        self.clicked.emit()


@dataclass
class _Panel:
    win: QWidget
    design: QSize
    close_btn: QPushButton | None = None
    surface: QFrame | None = None


class ModalHost(QObject):
    """Presents secondary windows as in-app panels inside ``main``."""

    def __init__(self, main: QWidget):
        super().__init__(main)
        self._main = main
        self._panels: list[_Panel] = []
        self._backdrop = _Backdrop(main)
        self._backdrop.hide()
        self._backdrop.clicked.connect(self._on_backdrop_click)
        main.installEventFilter(self)

    # ── public API ───────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        return bool(self._panels)

    def is_presented(self, win: QWidget) -> bool:
        return any(panel.win is win for panel in self._panels)

    def present(self, win: QWidget) -> None:
        """Turn ``win`` into an in-app panel and show it."""
        if self.is_presented(win):
            win.raise_()
            return
        # The size it asks for as a window is the size it wants here too —
        # read before reparenting, which can reset an unshown widget's size.
        design = win.size().expandedTo(win.sizeHint())
        if win.isWindow() or win.parentWidget() is not self._main:
            # Skipped for a window that was already presented once and merely
            # closed: setParent hides the widget and drops its place in the
            # sibling order for nothing.
            win.setParent(self._main, Qt.Widget)
        win.setObjectName("modal_panel")
        # Qt does not re-evaluate the stylesheet when an objectName changes,
        # and this one is only set here — so without an explicit re-polish the
        # #modal_panel rule never reaches the widget at all. That is why the
        # panels used to sit on the dim as unstyled rectangles.
        win.style().unpolish(win)
        win.style().polish(win)
        panel = _Panel(win, design)
        self._panels.append(panel)
        win.installEventFilter(self)
        # Deliberately NOT also on destroyed(): a panel is always hidden before
        # it is deleted, so that signal only ever arrives during application
        # teardown — where the control panel has already taken the backdrop
        # down with it, and touching it threw "Internal C++ object already
        # deleted" out of a Qt slot on every exit with a panel open.

        self._backdrop.setGeometry(self._main.rect())
        self._backdrop.show()
        self._backdrop.raise_()
        self._layout_panel(panel)
        win.show()
        win.raise_()
        self._add_surface(panel)  # after raise_(): it stacks against the panel
        self._add_close_button(panel)
        win.setFocus(Qt.OtherFocusReason)

    # ── internals ────────────────────────────────────────────────────────
    def _on_backdrop_click(self) -> None:
        if self._panels:
            self._panels[-1].win.close()

    def _forget(self, win: QWidget) -> None:
        """Drop a panel, whether it was closed, hidden or destroyed."""
        panel = next((p for p in self._panels if p.win is win), None)
        if panel is None:
            return
        self._panels.remove(panel)
        win.removeEventFilter(self)
        if panel.close_btn is not None:
            panel.close_btn.deleteLater()
            panel.close_btn = None
        if panel.surface is not None:
            panel.surface.deleteLater()
            panel.surface = None
        if not self._panels:
            self._backdrop.hide()
            return
        # The dim drops back under the new topmost panel, which brightens.
        self._backdrop.raise_()
        top = self._panels[-1]
        top.win.raise_()
        # …and that panel's own background has to follow it back up, or the dim
        # is left painting over it.
        if top.surface is not None:
            top.surface.stackUnder(top.win)
        top.win.setFocus(Qt.OtherFocusReason)

    def _add_surface(self, panel: _Panel) -> None:
        """The panel's rounded, bordered background, painted behind it.

        Why a widget of its own rather than styling the panel: a ``QDialog``
        takes its stylesheet ``background-color`` through the PALETTE, and a
        palette brush fills the whole rect knowing nothing about
        ``border-radius`` — so ``#modal_panel``'s border and radius never
        rendered and the panels sat on the dim as hard-edged rectangles.
        Neither ``WA_StyledBackground`` nor setting it before the first show
        changes that. A ``QFrame`` behind the (now transparent) panel is
        styled the ordinary way and antialiases, which a ``setMask`` region —
        being one bit deep — cannot.

        It works because nothing reaches into a panel's corners: every one of
        them insets its content, and the scroll areas are transparent.
        """
        surface = QFrame(self._main)
        surface.setObjectName("panel_surface")
        panel.surface = surface
        self._sync_surface(panel)
        surface.show()
        surface.stackUnder(panel.win)

    @staticmethod
    def _sync_surface(panel: _Panel) -> None:
        if panel.surface is not None:
            panel.surface.setGeometry(panel.win.geometry())

    def _add_close_button(self, panel: _Panel) -> None:
        """A floating ✕ — the panel lost its titlebar and with it the only
        way to close the history window, which has no button of its own."""
        button = QPushButton("✕", panel.win)
        button.setObjectName("panel_close")
        button.setFixedSize(_CLOSE_BOX, _CLOSE_BOX)
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(panel.win.close)
        panel.close_btn = button
        self._place_close_button(panel)
        button.show()
        button.raise_()

    def _place_close_button(self, panel: _Panel) -> None:
        button = panel.close_btn
        if button is None:
            return
        button.move(
            panel.win.width() - _CLOSE_BOX - _CLOSE_INSET,
            _CLOSE_INSET,
        )

    def _layout_panel(self, panel: _Panel) -> None:
        room = clamped_panel_size(panel.design, self._main.size())
        # Content-sized windows re-measure themselves against the room they
        # have; the rest are resized straight to it (a fixed-size widget
        # ignores the resize, which is the correct outcome for those too).
        panel.win.host_max_size = room
        refresh = getattr(panel.win, "_resize_to_content", None)
        if callable(refresh):
            refresh()
        else:
            panel.win.resize(room)
        size = panel.win.size()
        panel.win.move(
            max(0, (self._main.width() - size.width()) // 2),
            max(0, (self._main.height() - size.height()) // 2),
        )
        self._sync_surface(panel)

    def _relayout(self) -> None:
        if not self._panels:
            return
        self._backdrop.setGeometry(self._main.rect())
        for panel in self._panels:
            self._layout_panel(panel)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt API
        kind = event.type()
        if obj is self._main:
            if kind == QEvent.Resize:
                self._relayout()
            return False
        if kind == QEvent.Hide:
            # A hidden PARENT hides its children too, and those get a
            # QHideEvent without ever being hidden themselves — releasing on
            # that would tear the panels down every time the window is
            # minimised. isHidden() is the attribute Qt sets only for the
            # widget the hide was asked of.
            if obj.isHidden():
                self._forget(obj)
        elif kind == QEvent.Resize and self.is_presented(obj):
            # A content-sized panel changes height as its content changes.
            for panel in self._panels:
                if panel.win is obj:
                    self._place_close_button(panel)
                    size = obj.size()
                    obj.move(
                        max(0, (self._main.width() - size.width()) // 2),
                        max(0, (self._main.height() - size.height()) // 2),
                    )
                    self._sync_surface(panel)
                    break
        return False

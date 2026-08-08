"""One size rule for the content-sized secondary windows.

Settings, Batch and Announcement are the same shape — a hero, a column of
cards that scrolls, and an action bar pinned below it — so they open at the
same size: a fixed width, and the height their content asks for up to a cap.
Three windows that differ by 40 px of width read as three unrelated dialogs
rather than one app.

The height cap is the smallest of :data:`SECONDARY_MAX_H`, a share of the
screen, and the room an in-app host has (see ``gui/modal_host.py``, which
sets ``window.host_max_size``). Past it the cards scroll rather than the
action bar being pushed off the bottom.
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QWidget

# Laid out for a single column of dropdowns, switches and buttons: wider and
# the cards stretch into a form pulled out of shape.
SECONDARY_WINDOW_W = 520

# Never taller than this, whatever the content adds up to. A full announcement
# list or the settings card stack comes to well over a thousand pixels, which
# is a screen-tall window for a box you type one line into.
#
# Not lower: the batch window's collapsed cards come to ~720, so a tighter cap
# would open it already scrolled and leave its More-settings expander unable to
# grow the window at all — it would only ever add a scrollbar.
SECONDARY_MAX_H = 760

# …and never more than this share of a short screen either. Public because the
# control panel's own opening size applies the same rule (gui/control_panel.py
# _default_size) — one number, so the app never has two ideas of "too tall for
# this screen".
MAX_SCREEN_SHARE = 0.92


def content_size(window: QWidget, natural_height: int) -> QSize:
    """The size ``window`` should take for content wanting ``natural_height``.

    Split from :func:`apply_content_size` so the rule can be checked without a
    window that has actually been laid out.
    """
    width = SECONDARY_WINDOW_W
    height = min(int(natural_height), SECONDARY_MAX_H)
    screen = window.screen()
    if screen is not None:
        height = min(
            height, int(screen.availableGeometry().height() * MAX_SCREEN_SHARE)
        )
    room: QSize | None = getattr(window, "host_max_size", None)
    if room is not None:
        width = min(width, room.width())
        height = min(height, room.height())
    return QSize(width, height)


def apply_content_size(window: QWidget, natural_height: int) -> None:
    """Fix ``window`` to the shared width and the height its content asks for.

    Fixed rather than merely resized: the content is a single column that
    stretches badly, and the height is recomputed whenever the content changes,
    so a user-chosen size would be overwritten on the next change anyway.
    """
    size = content_size(window, natural_height)
    window.setFixedSize(size.width(), size.height())

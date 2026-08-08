"""The strip between the panel header and the cards, and the two things in it.

Both notices — "a newer release exists" and "how are you finding MinbarLive?" —
are the same object: an accent-soft bar carrying one sentence, one named way out,
and a ✕, which opens a URL in the browser when the bar itself is clicked. Only
*when to appear* and *what the two buttons mean* differ, so that is all the
subclasses say (``gui/update_banner.py``, ``gui/review_banner.py``).

They share the object names as well as the class, so the stylesheet has one set
of rules for both: accent-soft rather than a warning colour, because each is an
offer and not a problem.

Two Qt rules are baked in here so neither subclass can get them wrong:

* **Hidden at construction, after being parented.** A parentless widget made
  visible is a top-level window; hiding is the safe half of the same rule.
* **The outer spacing is a stylesheet margin, not a layout's.** A hidden banner
  then takes no room at all — a layout's spacing would leave a gap above the
  cards whether or not there was anything to say.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton


class NoticeBanner(QFrame):
    """A dismissible one-line notice. Subclass it; don't build one directly."""

    def __init__(self, translate: Callable[[str, str], str], parent=None):
        super().__init__(parent)
        self._t = translate
        self._url: str | None = None
        self.setObjectName("update_banner")
        self.setCursor(Qt.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(8)
        self.label = QLabel("")
        self.label.setObjectName("update_text")
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        # The named way out: "Skip this version" / "Never show again". Sized to
        # its own label rather than fixed, because the six GUI languages
        # disagree about how long that is.
        self.action_btn = QPushButton("")
        self.action_btn.setObjectName("banner_skip")
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.clicked.connect(self.on_action)
        row.addWidget(self.action_btn)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("banner_close")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.on_close)
        row.addWidget(self.close_btn)

        self.setVisible(False)

    # ── for subclasses ───────────────────────────────────────────────────
    def show_notice(self, text: str, url: str, action_text: str) -> None:
        """Put the notice up. ``url`` is what a click on the bar opens."""
        self.label.setText(text)
        self.action_btn.setText(action_text)
        self._url = url
        self.setVisible(True)

    def hide_notice(self) -> None:
        """Take it down and forget the URL, so a click can no longer follow it."""
        self._url = None
        self.setVisible(False)

    def fill(self, key: str, fallback: str, **values: str) -> str:
        """A translated template with ``values`` substituted.

        A translation that lost or mangled its placeholder must not take the
        notice down with it — the label then reads a little oddly instead of the
        banner never appearing.
        """
        template = self._t(key, fallback)
        try:
            return template.format(**values)
        except (KeyError, IndexError):
            return template

    # ── hooks ────────────────────────────────────────────────────────────
    def on_action(self) -> None:
        """The named button. Override; the default is the ✕'s behaviour."""
        self.on_close()

    def on_close(self) -> None:
        """The ✕. Override when dismissal has to be remembered."""
        self.hide_notice()

    def on_activated(self) -> None:
        """The bar itself was clicked and the URL opened. Override if that
        should also settle the question for good."""

    # ── Qt ───────────────────────────────────────────────────────────────
    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        # Only the bar: a click on either button is consumed by that button and
        # never reaches here, which is what keeps ✕ from also opening a browser.
        if event.button() == Qt.LeftButton and self._url:
            webbrowser.open(self._url)
            self.on_activated()
        super().mouseReleaseEvent(event)

"""The "a newer release exists" strip.

It sits between the panel header and the cards, appears only when the check
finds a release newer than the running version, opens the release page when
clicked and can be dismissed for the session. Without it the
``check_for_updates`` setting would have a checkbox and nothing behind it.

The check itself is ``utils/update_check.py``: one anonymous request to the
GitHub releases API, which never raises.
"""

from __future__ import annotations

import threading
import webbrowser

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from utils.logging import log
from utils.update_check import UpdateInfo, check_for_update

# One request per process. A GUI-language switch rebuilds the panel, and a
# fresh request per rebuild would be pure waste — the answer cannot change in
# that time, and the banner should come straight back.
_result: UpdateInfo | None = None
_checked = False


def reset_cache() -> None:
    """Forget the cached result (tests; each wants its own answer)."""
    global _result, _checked
    _result, _checked = None, False


class _Check(QObject):
    """Runs the request off the GUI thread and reports back as a signal."""

    done = Signal(object)  # UpdateInfo, or None

    def start(self) -> None:
        def _run() -> None:
            # check_for_update never raises; a failed request is just None.
            info = check_for_update()
            try:
                self.done.emit(info)
            except RuntimeError:
                # The banner went away while the request was in flight — a GUI
                # language switch rebuilds the panel, and the request outlives
                # it. Nobody left to tell, which is the whole handling.
                pass

        threading.Thread(target=_run, daemon=True, name="qt-update-check").start()


class UpdateBanner(QFrame):
    """Hidden until a newer release is confirmed."""

    def __init__(self, translate, parent=None):
        super().__init__(parent)
        self._t = translate
        self._info: UpdateInfo | None = None
        self.setObjectName("update_banner")
        self.setCursor(Qt.PointingHandCursor)

        row = QHBoxLayout(self)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(8)
        self.label = QLabel("")
        self.label.setObjectName("update_text")
        self.label.setWordWrap(True)
        row.addWidget(self.label, 1)
        self.close_btn = QPushButton("✕")
        self.close_btn.setObjectName("banner_close")
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.hide)
        row.addWidget(self.close_btn)

        self._check = _Check(self)
        self._check.done.connect(self._on_result)
        # Parented before being hidden — a parentless widget made visible is a
        # top-level window, and hiding is the safe half of the same rule.
        self.setVisible(False)

    def start_check(self, enabled: bool) -> None:
        """Ask GitHub whether a newer release exists, unless opted out."""
        if not enabled:
            return
        global _checked
        if _checked:
            # Already answered this process; show it again after a rebuild.
            self._apply(_result)
            return
        _checked = True
        self._check.start()

    def _on_result(self, info: UpdateInfo | None) -> None:
        global _result
        _result = info
        if info is not None:
            log(f"Update available: v{info.version}")
        self._apply(info)

    def _apply(self, info: UpdateInfo | None) -> None:
        self._info = info
        if info is None:
            self.setVisible(False)
            return
        template = self._t(
            "update_available", "Version {version} available — click to download"
        )
        try:
            self.label.setText(template.format(version=info.version))
        except (KeyError, IndexError):  # a translation with a broken placeholder
            self.label.setText(template)
        self.setVisible(True)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.LeftButton and self._info is not None:
            webbrowser.open(self._info.url)
        super().mouseReleaseEvent(event)

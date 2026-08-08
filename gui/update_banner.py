"""The "a newer release exists" strip.

It sits between the panel header and the cards, appears only when the check
finds a release newer than the running version, and opens the release page when
clicked. Without it the ``check_for_updates`` setting would have a checkbox and
nothing behind it.

**Two ways to make it go away, and they are not the same thing.** The ✕ hides it
for this run of the app and it is back on the next launch — "not now". *Skip
this version* records that release in ``skipped_update_version`` and the notice
stays quiet for it permanently, coming back only for the release after it —
"not this one". A user who is deliberately staying on their build needs the
second; without it the only way to stop being asked is to turn the whole update
check off, which then also hides the release they *would* want.

The bar itself is ``gui/notice_banner.py``, shared with the review prompt. The
check is ``utils/update_check.py``: one anonymous request to the GitHub releases
API, which never raises.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from gui.notice_banner import NoticeBanner
from utils.logging import log
from utils.update_check import UpdateInfo, check_for_update, is_newer_version

# One request per process. A GUI-language switch rebuilds the panel, and a
# fresh request per rebuild would be pure waste — the answer cannot change in
# that time, and the banner should come straight back. Keyed on the
# pre-release flag, which is the one thing that *can* change the answer within
# a process: toggling it in settings must not replay the other channel's
# cached result.
_result: UpdateInfo | None = None
_checked_with: bool | None = None


def reset_cache() -> None:
    """Forget the cached result (tests; each wants its own answer)."""
    global _result, _checked_with
    _result, _checked_with = None, None


class _Check(QObject):
    """Runs the request off the GUI thread and reports back as a signal."""

    done = Signal(object)  # UpdateInfo, or None

    def start(self, include_prereleases: bool) -> None:
        def _run() -> None:
            # check_for_update never raises; a failed request is just None.
            info = check_for_update(include_prereleases)
            try:
                self.done.emit(info)
            except RuntimeError:
                # The banner went away while the request was in flight — a GUI
                # language switch rebuilds the panel, and the request outlives
                # it. Nobody left to tell, which is the whole handling.
                pass

        threading.Thread(target=_run, daemon=True, name="qt-update-check").start()


class UpdateBanner(NoticeBanner):
    """Hidden until a newer release is confirmed."""

    def __init__(
        self,
        translate,
        parent=None,
        on_skip: Callable[[str], None] | None = None,
    ):
        super().__init__(translate, parent)
        self._info: UpdateInfo | None = None
        # Set per check rather than read from settings here: this widget is
        # built by the panel and knows nothing about where preferences live.
        self._skipped = ""
        self._on_skip = on_skip
        self._check = _Check(self)
        self._check.done.connect(self._on_result)

    def start_check(
        self,
        enabled: bool,
        include_prereleases: bool = False,
        skipped_version: str = "",
    ) -> None:
        """Ask GitHub whether a newer release exists, unless opted out.

        ``skipped_version`` filters the ANSWER, not the request: the cached
        result stays the true state of the world, so a rebuild after a skip and
        a later release both do the right thing.
        """
        self._skipped = skipped_version or ""
        if not enabled:
            return
        global _checked_with
        if _checked_with == include_prereleases:
            # Already answered this process; show it again after a rebuild.
            self._apply(_result)
            return
        _checked_with = include_prereleases
        self._check.start(include_prereleases)

    def _is_skipped(self, version: str) -> bool:
        """True when ``version`` is the one the user passed over, or older.

        Not an equality test: "skip 1.2.0" means "stop telling me about 1.2.0",
        and a check that later answers with something no newer than that — a
        patch published on an older branch, or the same tag re-pointed — is the
        same news again. ``is_newer_version`` is reused rather than restated so
        the suffix ordering (``rc.1 < rc.2 < 1.2.0``) holds here too.
        """
        if not self._skipped:
            return False
        return not is_newer_version(version, self._skipped)

    def on_action(self) -> None:
        """Skip this version."""
        if self._info is None:
            return
        version = self._info.version
        self._skipped = version
        log(f"Update v{version} skipped at the user's request.")
        if self._on_skip is not None:
            self._on_skip(version)
        self.hide_notice()

    def _on_result(self, info: UpdateInfo | None) -> None:
        global _result
        _result = info
        if info is not None:
            log(f"Update available: v{info.version}")
        self._apply(info)

    def _apply(self, info: UpdateInfo | None) -> None:
        self._info = info
        if info is None or self._is_skipped(info.version):
            self.hide_notice()
            return
        self.show_notice(
            self.fill(
                "update_available",
                "Version {version} available — click to download",
                version=info.version,
            ),
            info.url,
            self._t("skip_this_version", "Skip this version"),
        )

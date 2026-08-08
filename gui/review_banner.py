"""The "how are you finding MinbarLive?" strip.

Same bar as the update notice (``gui/notice_banner.py``), asked at a different
moment: after the operator has actually used the thing. It appears once
``PROMPT_AFTER_SESSIONS`` live sessions have been *completed* — counted at Stop,
not at launch, because someone who has finished three khutbahs has something to
say and someone who has opened the app three times does not.

**Three ways out, and each means something different.** Clicking the bar opens
the form and settles it for good — they have given feedback, asking again would
be rude. *Never show again* settles it for good without the form. The ✕ is "not
this time": the counter goes back to zero and the question returns after another
three sessions. So the only way to be asked twice is to keep saying "not now",
which is exactly the person who has not decided yet.

The URL is the anonymous Google Form already linked from the README, the
CONTRIBUTING guide and the website's Feedback button — deliberately the same
one, so responses land in a single place. Nothing about the installation is sent
with it: it is a plain browser navigation, and Google sees what it would see if
the user had typed the address.
"""

from __future__ import annotations

from collections.abc import Callable

from gui.notice_banner import NoticeBanner
from utils.logging import log

# The anonymous feedback form. Same URL as README.md, CONTRIBUTING.md and the
# Feedback button in docs/index.html — one form, one place to read the answers.
FEEDBACK_FORM_URL = "https://forms.gle/DJ3F25HKrrLjH9h59"

# Completed sessions before the question is put. Three, so it lands after the
# app has been used in earnest rather than during the first evening of
# fiddling, and low enough that a mosque running one khutbah a week reaches it
# inside a month.
PROMPT_AFTER_SESSIONS = 3


class ReviewBanner(NoticeBanner):
    """Hidden until enough sessions have been completed.

    ``on_decision`` is how the panel is told to persist: it is called with the
    session count to store and whether the prompt is now off for good. The
    banner owns the policy and none of the storage, the same split the update
    banner uses for ``skipped_update_version``.
    """

    def __init__(
        self,
        translate,
        parent=None,
        on_decision: Callable[[int, bool], None] | None = None,
    ):
        super().__init__(translate, parent)
        self._on_decision = on_decision

    def maybe_show(self, sessions: int, disabled: bool) -> None:
        """Put the question up if it is due. Safe to call after every stop."""
        if disabled or sessions < PROMPT_AFTER_SESSIONS:
            return
        self.show_notice(
            self._t(
                "review_prompt",
                "Enjoying MinbarLive? Tell us what you think — click to open the form",
            ),
            FEEDBACK_FORM_URL,
            self._t("review_never", "Never show again"),
        )

    def on_activated(self) -> None:
        """They clicked through to the form. Never ask again."""
        log("Feedback form opened from the review prompt.")
        self._decide(disabled=True)

    def on_action(self) -> None:
        """Never show again."""
        log("Review prompt turned off for good.")
        self._decide(disabled=True)

    def on_close(self) -> None:
        """"Not this time": ask again after another run of sessions.

        The counter is reset rather than left at the threshold, which is the
        whole difference between this and *Never show again* — left alone it
        would be due again at the very next stop, and a notice that reappears
        immediately is the one people learn to turn off.
        """
        log("Review prompt dismissed; asking again later.")
        self._decide(disabled=False)

    def _decide(self, *, disabled: bool) -> None:
        self.hide_notice()
        if self._on_decision is not None:
            self._on_decision(0, disabled)

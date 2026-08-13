"""Remembers that the speaker is reciting, and says which ayah comes next.

Recitation is the one part of a khutbah where the *next* thing said is
predictable: someone who has just recited 2:255 and 2:256 is far more likely
to say 2:257 than any of the other 6,052 verses. Embedding similarity does not
know that, and it is at its weakest exactly here — a segment that straddles a
verse boundary blends two verses, and the blend scores below
``RAG_MIN_SIMILARITY`` or falls out of the top ``RAG_TOP_K``. The verses are
then never nominated, so the exact-text run verifier in ``translator.py``
never gets to see the case it was written for.

**This module only ever adds candidates. It never certifies one.** That
boundary is the whole safety argument. A tracker that lowered a threshold, or
handed GPT a verse marked "expected", would eventually put an ayah on a mosque
screen that nobody recited — the one failure this app must not have. Injected
candidates go to ``_select_verified_verse_run`` alone, which admits a verse
only if the concatenated dictionary text fuzzy-matches what was actually said
(``RAG_TEXT_MATCH_SIMILARITY``) and the length guards pass. A wrong
prediction fails that comparison and disappears; it cannot reach the subtitle.
The score-based single-verse bypass never sees these candidates at all.

State is process-global like the verse store itself, and is reset per session
(``AppController.start``) and per batch file, so one khutbah's recitation
cannot prime the next.
"""

from __future__ import annotations

import re
import threading
import time

from config import (
    RECITATION_LOOKAHEAD_AYAT,
    RECITATION_MIN_VERSES,
    RECITATION_WINDOW_SECONDS,
)
from translation.dictionary import quran_dict
from utils.logging import log

# Trailing (surah:ayah) reference in the reference-dictionary translations.
_AYAH_REF_RE = re.compile(r"\((\d+):(\d+)\)\s*$")

# (surah, ayah) -> Arabic verse, built once from the reference dictionary that
# every RAG candidate is guaranteed to be in. Rebuilt if that dictionary was
# still empty at import time (language switch, lazy load).
_by_ref: dict[tuple[int, int], str] = {}
_indexed_verses = 0


def _ensure_index() -> None:
    global _by_ref, _indexed_verses
    if _indexed_verses == len(quran_dict):
        return
    index: dict[tuple[int, int], str] = {}
    for arabic, translation in quran_dict.items():
        match = _AYAH_REF_RE.search(translation)
        if match:
            index[(int(match.group(1)), int(match.group(2)))] = arabic
    _by_ref = index
    _indexed_verses = len(quran_dict)


class RecitationTracker:
    """The refs certified recently, and what they imply comes next.

    Translation runs on one pipeline thread but batch mode and tests reach in
    from others, so the (very small) state is lock-guarded.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (surah, ayah) -> when it was certified.
        self._seen: dict[tuple[int, int], float] = {}
        # The surah currently being recited, how far into it we have got, and
        # when that verdict goes stale.
        self._surah: int | None = None
        self._highest = 0
        self._until = 0.0

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()
            self._surah = None
            self._highest = 0
            self._until = 0.0

    def note_certified(self, refs: list[tuple[int, int]]) -> None:
        """Record verses that passed verification — never mere candidates.

        **Verified verses arrive with gaps, and the gaps are the normal case.**
        A reciter pauses for breath or holds a madd, the segment carries half
        an ayah, the text check correctly refuses to certify it, and that verse
        is translated by GPT instead. So a real recitation reaches here as
        "81:7 verified, 81:8 missed, 81:9 verified" far more often than as a
        clean consecutive run — and it is exactly the missed verses that the
        prediction exists to rescue.

        Hence two different bars:

        - **Starting** a recitation needs RECITATION_MIN_VERSES of one surah
          inside the window, consecutive or not. One ayah quoted mid-sermon
          predicts nothing about what follows; two say the speaker is working
          through a surah.
        - **Continuing** one needs a single verse of that same surah. Requiring
          two again would drop out of recitation mode precisely when
          recognition got patchy, which is when the help is worth most.

        A wrong guess costs nothing — it fails the text check downstream — so
        the tracker is deliberately generous once it is convinced.
        """
        if not refs:
            return
        now = time.time()
        with self._lock:
            for ref in refs:
                self._seen[ref] = now
            for surah in sorted({s for s, _ in refs}):
                highest = max(a for s, a in refs if s == surah)
                if self._surah == surah and now <= self._until:
                    self._highest = max(self._highest, highest)
                else:
                    recent = [
                        a
                        for (s, a), seen in self._seen.items()
                        if s == surah and seen >= now - RECITATION_WINDOW_SECONDS
                    ]
                    if len(recent) < RECITATION_MIN_VERSES:
                        continue  # a lone quotation, not a recitation
                    self._surah = surah
                    self._highest = max(recent)
                self._until = now + RECITATION_WINDOW_SECONDS

    def note_nominated(self, refs: list[tuple[int, int]]) -> None:
        """Verses RAG merely *surfaced* — enough to move the anchor along, never
        enough to start a recitation.

        Certification is sparse: over a five-minute Tā-Hā recitation measured
        live 2026-08-13 the tracker offered only four distinct predictions in
        39 firings, because the anchor advanced only when a verse verified. It
        sat on 20:23-25 for nine consecutive segments while the reciter
        travelled from 20:23 to 20:33 — stale, and therefore useless in exactly
        the stretch it was meant to cover.

        A nomination is weak evidence, so it gets a correspondingly weak power:
        it may only advance an ALREADY ACTIVE recitation, and only onto a verse
        this tracker was already predicting (within RECITATION_LOOKAHEAD_AYAT
        of the anchor). So it can confirm "the reciter reached the verse we
        expected" and cannot invent a jump somewhere else — the anchor still
        walks the surah one prediction at a time.

        Safe by the same argument as everything else here: the anchor only
        decides which candidates are *offered*, and an offered verse still has
        to match the spoken words to be certified.
        """
        if not refs:
            return
        now = time.time()
        with self._lock:
            if self._surah is None or now > self._until:
                return  # nomination alone never starts a recitation
            reachable = [
                ayah
                for surah, ayah in refs
                if surah == self._surah
                and self._highest < ayah <= self._highest + RECITATION_LOOKAHEAD_AYAT
            ]
            if not reachable:
                return
            self._highest = max(reachable)
            self._until = now + RECITATION_WINDOW_SECONDS

    def expected_next(self) -> list[tuple[int, int]]:
        """The ayat likely to be recited next, or ``[]`` when not reciting.

        Counts on from the furthest ayah reached in the active surah — not
        from the last one certified, which after an out-of-order or repeated
        verse would walk the prediction backwards over ground already covered.
        """
        with self._lock:
            if self._surah is None or time.time() > self._until:
                return []
            surah, highest = self._surah, self._highest
        return [
            (surah, highest + step)
            for step in range(1, RECITATION_LOOKAHEAD_AYAT + 1)
        ]


_tracker = RecitationTracker()


def reset() -> None:
    """Forget the current recitation (new session, or next batch file)."""
    global _indexed_verses
    _tracker.reset()
    # Drop the ref index too. Its cache key is the dictionary's *length*, which
    # cannot tell two equally sized dictionaries apart — fine in production
    # where there is one, but enough to leak a stale index between tests.
    _indexed_verses = 0


def note_certified(refs: list[tuple[int, int]]) -> None:
    _tracker.note_certified(refs)


def note_nominated(refs: list[tuple[int, int]]) -> None:
    _tracker.note_nominated(refs)


def expected_next() -> list[tuple[int, int]]:
    """The ayat likely to be recited next, or ``[]`` when not reciting."""
    return _tracker.expected_next()


def expected_candidates(existing: list) -> list:
    """``existing`` plus the expected next ayat, as RAG-shaped candidates.

    Returns the list unchanged when no recitation is in progress or the
    expected verses are already nominated.

    The injected tuples carry a score of 0.0 deliberately. Nothing downstream
    may treat a *prediction* as evidence: 0.0 keeps them out of the top slot
    the score-based single-verse bypass reads, and leaves the run verifier —
    which ignores scores except to break ties between runs of equal length —
    to certify them on the text alone.
    """
    expected = _tracker.expected_next()
    if not expected:
        return existing

    _ensure_index()
    already = {verse for _score, verse, _hint in existing}
    injected = []
    for ref in expected:
        arabic = _by_ref.get(ref)
        if arabic is None or arabic in already:
            continue  # end of the surah, or the embedding already found it
        injected.append((0.0, arabic, quran_dict.get(arabic, arabic)))

    if not injected:
        return existing

    log(
        "Recitation in progress — offering "
        f"{', '.join(f'({s}:{a})' for s, a in expected)} to the text verifier "
        f"({len(injected)} not already nominated).",
        level="INFO",
    )
    return [*existing, *injected]

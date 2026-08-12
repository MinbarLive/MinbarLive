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
(``RAG_MULTI_VERSE_TEXT_SIMILARITY``) and the length guards pass. A wrong
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

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()

    def note_certified(self, refs: list[tuple[int, int]]) -> None:
        """Record verses that passed verification — never mere candidates."""
        if not refs:
            return
        now = time.time()
        with self._lock:
            for ref in refs:
                self._seen[ref] = now

    def expected_next(self) -> list[tuple[int, int]]:
        """The ayat likely to be recited next, or ``[]`` when not reciting.

        Continues the highest ayah of the longest recent run in a single
        surah. Requires RECITATION_MIN_VERSES of that surah inside
        RECITATION_WINDOW_SECONDS: one verse is a quotation mid-sermon, which
        predicts nothing about what follows.
        """
        cutoff = time.time() - RECITATION_WINDOW_SECONDS
        with self._lock:
            recent = [ref for ref, seen in self._seen.items() if seen >= cutoff]
        if len(recent) < RECITATION_MIN_VERSES:
            return []

        by_surah: dict[int, set[int]] = {}
        for surah, ayah in recent:
            by_surah.setdefault(surah, set()).add(ayah)

        best: tuple[int, int] | None = None  # (surah, last ayah of the run)
        best_length = 0
        for surah, ayahs in by_surah.items():
            ordered = sorted(ayahs)
            length = 1
            for index in range(1, len(ordered)):
                length = length + 1 if ordered[index] == ordered[index - 1] + 1 else 1
                if length > best_length:
                    best_length, best = length, (surah, ordered[index])
            if best_length == 0 and len(ordered) >= RECITATION_MIN_VERSES:
                # Same surah, non-consecutive (a skipped ayah, or one verse
                # mis-detected). Still recitation — continue from the last.
                best_length, best = 1, (surah, ordered[-1])

        if best is None or best_length < RECITATION_MIN_VERSES:
            return []
        surah, last = best
        return [(surah, last + step) for step in range(1, RECITATION_LOOKAHEAD_AYAT + 1)]


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

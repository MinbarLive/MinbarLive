"""Subtitle text that is not a widget: the footer wording and the block splitter.

Toolkit-free on purpose (see ``gui/levels.py``). Splitting an oversized block at
its sentence boundaries is text logic with real edge cases, and it should be
testable without building an overlay to hold it.
"""

from __future__ import annotations

import re

from config import FOOTER_TRANSLATIONS_PATH
from utils.json_helpers import load_json

# One sentence: everything up to a terminator (Latin and Arabic), plus any
# closing quotation marks, or the trailing fragment of an unfinished one.
_SENTENCE_RE = re.compile(r"[^.!?…؟؛]*[.!?…؟؛]+[\"'“”„»«]*\s*|[^.!?…؟؛]+$")

FOOTER_TRANSLATIONS = load_json(FOOTER_TRANSLATIONS_PATH)

# Default footer for languages not in the list
DEFAULT_FOOTER = (
    "AI Translation! Our association assumes no liability for accuracy or completeness."
)


def split_display_chunks(text: str, max_chars: int) -> list[str]:
    """Split a long settled translation at sentence boundaries into chunks
    of at most ``max_chars`` (a single over-long sentence stays whole).
    Continuous speech flushes up to 12s of speech as one utterance — its
    translation would otherwise render as a wall of text in the feed."""
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    sentences = [m.group(0).strip() for m in _SENTENCE_RE.finditer(text)]
    sentences = [s for s in sentences if s]
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        if current and len(current) + 1 + len(sentence) > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks

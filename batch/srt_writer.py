"""SRT subtitle formatting and file writing."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SrtEntry:
    """One subtitle: start/end in seconds plus the display text.

    ``source`` is the original transcription for a bilingual block. When set
    (and different from ``text``) it is rendered on its own line above the
    translation; left as ``None`` the block is single-language. Language-
    agnostic — the source line is whatever was transcribed, RTL or LTR, kept
    as plain logical text (subtitle players do the bidi shaping).
    """

    start: float
    end: float
    text: str
    source: str | None = None


def format_timestamp(seconds: float) -> str:
    """Format seconds as an SRT timestamp (HH:MM:SS,mmm)."""
    total_ms = max(0, round(seconds * 1000))
    ms = total_ms % 1000
    total_s = total_ms // 1000
    return f"{total_s // 3600:02d}:{total_s // 60 % 60:02d}:{total_s % 60:02d},{ms:03d}"


def build_srt(entries: list[SrtEntry]) -> str:
    """Render entries as SRT text; empty-text entries are dropped and the
    remaining blocks renumbered sequentially.

    When an entry carries a ``source`` distinct from its text, the block is
    bilingual: the source line sits above the translation. A source equal to
    the text (same-language runs, code-switching pass-through) collapses to a
    single line so identical text is never printed twice.
    """
    blocks = []
    index = 1
    for entry in entries:
        text = (entry.text or "").strip()
        if not text:
            continue
        source = (entry.source or "").strip()
        body = f"{source}\n{text}" if source and source != text else text
        blocks.append(
            f"{index}\n"
            f"{format_timestamp(entry.start)} --> {format_timestamp(entry.end)}\n"
            f"{body}"
        )
        index += 1
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt(entries: list[SrtEntry], path: str) -> None:
    """Write entries to an SRT file.

    UTF-8 with BOM: legacy Windows players mis-decode plain UTF-8 subtitles
    (Arabic, umlauts), and every modern player accepts the BOM.
    """
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(build_srt(entries))

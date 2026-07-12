"""Plain-text transcript/translation export for batch runs.

The "general transcription tool" output format: the full transcription and
the full translation as two readable sections, one paragraph per segment.
Deliberately no timestamps — the SRT output covers the timestamped view.

Honest scope note: this is a formatting alternative, not a quality fix —
words the STT model dropped are missing here exactly as they are in the SRT.
"""

from __future__ import annotations


def build_text(
    records: list[tuple[float, str, str]],
    source_language: str,
    target_language: str,
) -> str:
    """Render batch records ((start_s, transcription, translation)) as text.

    When every translation is identical to its transcription (same-language
    runs and the per-segment Arabic bypass), a single section is written
    instead of the same text twice — mirrors the history viewer's collapsing.
    """
    transcriptions = [t.strip() for _s, t, _tr in records if t.strip()]
    translations = [tr.strip() for _s, _t, tr in records if tr.strip()]
    if not transcriptions and not translations:
        return ""

    def section(title: str, paragraphs: list[str]) -> str:
        return f"{title}\n{'=' * len(title)}\n\n" + "\n\n".join(paragraphs)

    identical = all(t.strip() == tr.strip() for _s, t, tr in records)
    if identical:
        parts = [section(f"TRANSCRIPT ({source_language})", transcriptions)]
    else:
        parts = [
            section(f"TRANSCRIPT ({source_language})", transcriptions),
            section(f"TRANSLATION ({target_language})", translations),
        ]
    return "\n\n\n".join(parts) + "\n"


def write_text(
    records: list[tuple[float, str, str]],
    path: str,
    source_language: str,
    target_language: str,
) -> None:
    """Write the transcript document.

    UTF-8 with BOM for the same reason as the SRT writer: legacy Windows
    editors mis-decode plain UTF-8 Arabic/umlauts, and everything modern
    accepts the BOM.
    """
    with open(path, "w", encoding="utf-8-sig", newline="\n") as f:
        f.write(build_text(records, source_language, target_language))

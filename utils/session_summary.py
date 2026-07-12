"""Generate a natural-language summary of a past session's transcript.

Reuses the generic ``complete()`` text-generation call every translation
provider exposes (the same one the context manager uses for rolling
summaries). Given the paired transcript (original + translation), it asks the
chosen provider to produce a concise summary in a chosen language.
"""

from __future__ import annotations

from providers import get_default_model, get_translation_provider_for
from utils.history import HistoryEntry, pair_entries, parse_history_file
from utils.settings import load_settings

# Generous: on reasoning models (gpt-5.x) the hidden reasoning tokens count
# against this same budget, so 900 truncated long-session summaries mid-sentence.
_SUMMARY_MAX_OUTPUT_TOKENS = 3000
_SUMMARY_TEMPERATURE = 0.3


def _build_prompts(
    pairs: list[tuple[HistoryEntry, HistoryEntry | None]],
    target_language: str,
    session_label: str | None,
) -> tuple[str, str]:
    """Build the (system, user) prompt pair. Both the original speech and its
    translation are included so the model has the full context."""
    # Match the translation/context prompts: Islamic framing only in Islamic
    # mode. Follows the current setting (read fresh), simple and consistent.
    if load_settings().islamic_mode:
        system = (
            "You are an assistant that turns the transcript of an Islamic "
            "lecture or Friday sermon (khutbah) into a clear, self-contained "
            "text that reads like a passage from a book — not a report about a "
            "talk. "
            f"Write in {target_language}. "
            "Base it strictly on the transcript — do not add anything that is "
            "not present. Present the content and message directly in flowing "
            "prose; do NOT narrate the event or mention the speaker or setting "
            "(avoid phrasings like 'in the khutbah', 'the speaker said', 'the "
            "lecture explained'). Weave in the ideas, arguments and any Quran "
            "verses or hadith as part of the text itself. Keep Islamic terms "
            "and names respectful and untranslated where customary (e.g. Allah, "
            "insha'Allah). The transcript is machine-generated and may contain "
            "recognition errors; smooth over obvious noise. Organize it as "
            "prose under a few short section headings (named in the same "
            "language as the text), not as a bulleted list."
        )
    else:
        system = (
            "You are an assistant that turns a transcript of a talk or lecture "
            "into a clear, self-contained text that reads like a passage from "
            "a book — not a report about a talk. "
            f"Write in {target_language}. "
            "Base it strictly on the transcript — do not add anything that is "
            "not present. Present the content directly in flowing prose; do "
            "NOT narrate the event or mention the speaker or setting (avoid "
            "phrasings like 'in the talk', 'the speaker said', 'the lecture "
            "explained'). The transcript is machine-generated and may contain "
            "recognition errors; smooth over obvious noise. Organize it as "
            "prose under a few short section headings (named in the same "
            "language as the text), not as a bulleted list."
        )

    lines: list[str] = []
    if session_label:
        lines.append(f"Session: {session_label}")
        lines.append("")
    for source, target in pairs:
        lines.append(f"[{source.lang}] {source.text}")
        # Same-language records: transcription == translation, send it once.
        if target is not None and target.text != source.text:
            lines.append(f"[{target.lang}] {target.text}")
        lines.append("")

    user = (
        "Summarize the following session transcript. Each entry shows the "
        "original speech, followed by its translation when it differs.\n\n"
        + "\n".join(lines).strip()
    )
    return system, user


def summarize_session_file(
    path: str,
    *,
    target_language: str,
    provider_id: str,
    session_label: str | None = None,
    model: str | None = None,
) -> str:
    """Summarize a daily history file with the chosen provider/language.

    Args:
        path: Path to the daily history file.
        target_language: Language to write the summary in (e.g. "German").
        provider_id: Translation provider to use (e.g. "openai").
        session_label: Optional header shown to the model (date + language pair).
        model: Override model; defaults to the provider's default translation model.

    Raises:
        ValueError: If the file has no summarizable content.
    """
    entries = parse_history_file(path)
    pairs = pair_entries(entries)
    if not pairs:
        raise ValueError("Session has no content to summarize.")

    system, user = _build_prompts(pairs, target_language, session_label)
    provider = get_translation_provider_for(provider_id)
    model = model or get_default_model(provider_id, "translation")
    return provider.complete(
        model=model,
        system_prompt=system,
        user_prompt=user,
        max_output_tokens=_SUMMARY_MAX_OUTPUT_TOKENS,
        temperature=_SUMMARY_TEMPERATURE,
    )

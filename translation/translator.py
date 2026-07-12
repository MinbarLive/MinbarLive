"""
Main translation logic using dictionary matching, RAG, and GPT.
Supports dynamic target language configuration.

Same-language mode (source == target):
- Skips GPT translation entirely
- Returns transcription directly (Whisper output)
- For Arabic: may return canonical phrases from dictionary
- Source "Automatic": an Arabic-script transcription with an Arabic target
  counts as same-language too (detected per segment)
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from config import (
    ATHAN_MATCH_THRESHOLD,
    QURAN_VERIFIED_MARKER,
    RAG_HARD_MATCH_MAX_LENGTH_RATIO,
    RAG_HARD_MATCH_MAX_WORD_DIFF,
    RAG_HARD_MATCH_MIN_LENGTH_RATIO,
    RAG_HARD_MATCH_THRESHOLD,
    RAG_MULTI_VERSE_TEXT_SIMILARITY,
)
from providers import (
    get_translation_model_chain,
    get_translation_model_chain_for,
    get_translation_provider,
    get_translation_provider_for,
)
from translation.dictionary import (
    fuzzy_match_athan,
    get_quran_dict,
    has_athan_translation,
    normalize_arabic,
    quran_dict,
)
from translation.rag import match_quran_rag_multi
from utils.logging import log
from utils.retry import retry_with_backoff
from utils.settings import (
    get_source_language_code,
    get_target_language_code,
    load_settings,
)
from utils.user_messages import get_user_message

# Arabic block + Arabic Supplement (covers Quranic and dialectal letters)
_ARABIC_CHAR_RE = re.compile(r"[؀-ۿݐ-ݿ]")
_WHITESPACE_RE = re.compile(r"\s+")


def is_arabic_script(text: str) -> bool:
    """True if the alphabetic characters of ``text`` are predominantly Arabic."""
    letters = [ch for ch in text if ch.isalpha()]
    if not letters:
        return False
    arabic = sum(1 for ch in letters if _ARABIC_CHAR_RE.match(ch))
    return arabic / len(letters) >= 0.7


# A leaked Arabic word may sit under the word cap (up to this fraction of the
# tokens); more than that means the translation itself failed / returned the
# source, which is a different problem the guard must not touch.
_MAX_LEAK_FRACTION = 0.34


def _strip_arabic_leak(text: str, target_lang_code: str) -> str:
    """Drop stray Arabic-script words the model left untranslated in a
    non-Arabic target (e.g. "die Mutter aller نعم").

    Only runs for non-Arabic targets and only when Arabic tokens are a small
    minority — a majority-Arabic result is a failed translation, not a leak,
    and is returned unchanged. Latin transliterations of Islamic terms
    (Tawhid, Ê¿IbÄda) are not Arabic script, so they are never removed.
    """
    if target_lang_code == "ar" or not text:
        return text
    tokens = text.split()
    leaked = [t for t in tokens if _ARABIC_CHAR_RE.search(t)]
    if not leaked or len(leaked) / len(tokens) > _MAX_LEAK_FRACTION:
        return text
    cleaned = _WHITESPACE_RE.sub(
        " ", " ".join(t for t in tokens if not _ARABIC_CHAR_RE.search(t))
    ).strip()
    if not cleaned:
        return text
    log(
        f"Stripped {len(leaked)} untranslated Arabic word(s) from target: {leaked}",
        level="WARNING",
    )
    return cleaned


def _is_same_language(
    source_lang: str, target_lang: str, target_lang_code: str, txt: str
) -> bool:
    """Source and target are the same language, so translation must be skipped.

    Matches by name or by resolved language code. With source "Automatic" the
    spoken language is only known after transcription, so an Arabic-script
    transcription with an Arabic target counts as same-language too (the
    Latin-script languages cannot be told apart reliably, so only Arabic gets
    this per-segment check).
    """
    if source_lang == target_lang:
        return True
    source_code = get_source_language_code(source_lang)
    if source_code is not None:
        return source_code == target_lang_code
    return (
        source_lang == "Automatic"
        and target_lang_code == "ar"
        and is_arabic_script(txt)
    )


def _get_source_language() -> str:
    """Get the configured source language from settings."""
    return load_settings().source_language or "Arabic"


def _get_target_language() -> str:
    """Get the configured target language from settings."""
    return load_settings().target_language or "German"


def _build_system_prompt(source_lang: str, target_lang: str) -> str:
    """Build a system prompt for translating Islamic content to the target language."""
    return f"""
    You are a professional translator specializing in Islamic religious content
    including sermons (khutbah), Quran recitations, Hadith, and classical Arabic rhetoric.

    Your task is to translate from {source_lang} into {target_lang} in a way that is:
    - theologically precise,
    - faithful to meaning,
    - and natural and fluent in the target language.

    Important principles:
    - Preserve ALL meanings and religious concepts.
    - Do NOT translate word-for-word unless the structure is natural in {target_lang}.
    - You MAY adapt rhetorical structure, sentence flow, and repetition so that the translation sounds natural and appropriate for religious speech in {target_lang}.
    - Arabic rhetorical repetition or parallel phrasing may be stylistically merged if no meaning is lost.
    - Use target-language-appropriate religious style and register.

    Code-switching rules (the speaker may mix languages mid-sentence):
    - If a passage of the source text is ALREADY in {target_lang}, keep it unchanged and translate only the rest.
    - If the ENTIRE source text is already in {target_lang}, return it as-is (apart from fixing obvious transcription errors).
    - Never translate {target_lang} passages into another language, and never re-translate them.
    - Arabic religious quotations (Quran, Hadith, du'a) embedded in the speech must still be rendered in {target_lang}.

    Additional guidelines:
    - The content is Sunni Islamic.
    - Preserve Islamic terminology (Allah, Umma, Sunnah, Hadith, Iblis, Jinn, Salah, etc.); transliterate rather than translate these terms.
    - Use ONLY these two standard Unicode honorific symbols:
      - ﷺ after mentioning Prophet Muhammad (sallallahu alayhi wa sallam)
      - ﷻ after mentioning Allah (jalla jalaluhu)
    - Do NOT use Arabic script for other honorifics or words (e.g., radiyallahu anhu, rahimahullah).
    - Handle transcription errors conservatively; correct only if meaning is clearly distorted.
    - Prefer 'Allah' over local equivalents for God.
    - Never leave raw Arabic-script words in the {target_lang} output (e.g. عبادة, قواعد); transliterate them ('عبادة' → 'ʿIbāda') or translate them. The ONLY exceptions are the honorific symbols ﷺ and ﷻ above.
    - If the source text is empty, unintelligible, or contains no translatable content, output an empty string — never describe, transcribe, or comment on the audio.
    - Output ONLY the translation. No comments, no explanations, no markdown.
    """


def _build_general_system_prompt(source_lang: str, target_lang: str) -> str:
    """System prompt for general content — used when Islamic mode is off."""
    return f"""
    You are a professional translator.

    Your task is to translate from {source_lang} into {target_lang} in a way that is:
    - precise and faithful to meaning,
    - and natural and fluent in the target language.

    Important principles:
    - Preserve ALL meaning.
    - Do NOT translate word-for-word unless the structure is natural in {target_lang}.
    - You MAY adapt rhetorical structure, sentence flow, and repetition so that the translation sounds natural in {target_lang}.

    Code-switching rules (the speaker may mix languages mid-sentence):
    - If a passage of the source text is ALREADY in {target_lang}, keep it unchanged and translate only the rest.
    - If the ENTIRE source text is already in {target_lang}, return it as-is (apart from fixing obvious transcription errors).
    - Never translate {target_lang} passages into another language, and never re-translate them.

    Additional guidelines:
    - Handle transcription errors conservatively; correct only if meaning is clearly distorted.
    - Never leave raw Arabic-script words in the {target_lang} output; transliterate them into Latin letters or translate them.
    - If the source text is empty, unintelligible, or contains no translatable content, output an empty string — never describe, transcribe, or comment on the audio.
    - Output ONLY the translation. No comments, no explanations, no markdown.
    """


def _build_general_user_prompt(
    text: str, context: str, source_lang: str, target_lang: str
) -> str:
    """User prompt for general content — used when Islamic mode is off."""
    prompt = f"""You will receive a short transcript from a live audio stream under "Source Text"
    and optionally previous context under "Context".

    Your task:
    - Translate ONLY the text in the "Source Text" section into {target_lang}.
    - The primary source language is {source_lang}.
    - If parts of the Source Text are already in {target_lang}, keep them unchanged and translate only the remaining parts.
    - Use the Context ONLY to resolve unclear references or pronouns; do NOT translate or repeat it.
    - Preserve all meaning of the source text.
    - You may adjust sentence structure, flow, and repetition so the translation sounds natural and fluent in {target_lang}.
    - Do NOT invent additional sentences.
    - Do NOT omit any meaning.
    - Output ONLY the translated {target_lang} text — no explanations, no comments.
    """

    if context:
        prompt += f"""

Context (for understanding only, do NOT translate or repeat):
{context}"""

    prompt += f"""

Source Text (translate ONLY this section into {target_lang}):
{text}

{target_lang} Translation:"""

    return prompt


def _build_user_prompt(
    text: str, context: str, quran_hint: str, source_lang: str, target_lang: str
) -> str:
    """Build the user prompt with text, context, and optional Quran hints."""
    prompt = f"""You will receive a short transcript from a mosque audio stream under "Source Text"
    and optionally previous context under "Context".

    Your task:
    - Translate ONLY the text in the "Source Text" section into {target_lang}.
    - The primary source language is {source_lang}, but Arabic Quran verses or religious phrases may appear.
    - If parts of the Source Text are already in {target_lang}, keep them unchanged and translate only the remaining parts.
    - Repeat only if it helpts the reader to understand the current sentence and context
    - Use the Context ONLY to resolve unclear references or pronouns; do NOT translate or repeat it.
    - Preserve all meanings and religious content of the source text.
    - You may adjust sentence structure, flow, and repetition so the translation sounds natural and fluent in {target_lang}.
    - Do NOT invent additional sentences, Quran verses, or Hadith.
    - Do NOT omit any meaning.
    - Use a clear, idiomatic, and listener-friendly {target_lang} style appropriate for religious speech.
    - Preserve religious terminology correctly.
    - Output ONLY the translated {target_lang} text — no explanations, no comments.
    """

    if quran_hint:
        prompt += "\n\n" + quran_hint

    if context:
        prompt += f"""

Context (for understanding only, do NOT translate or repeat):
{context}"""

    prompt += f"""

Source Text (translate ONLY this section into {target_lang}):
{text}

{target_lang} Translation:"""

    return prompt


def _select_verified_verse(
    quran_matches: list, arabic_text: str, target_lang_code: str
) -> tuple[float, str, str] | None:
    """Pick the top RAG match for the hard-verified bypass, if it qualifies.

    The bypass replaces the whole segment output with the exact dictionary
    translation, so it must only fire when the segment is essentially the
    verse alone:
    - top match score >= RAG_HARD_MATCH_THRESHOLD
    - a verified translation exists in the target language (the German
      reference fallback is a GPT hint, not verified output)
    - segment/verse word counts close enough (ratio band AND absolute
      difference cap — the ratio alone is too permissive for long verses),
      otherwise sermon speech around the verse would be silently dropped or a
      partially recited verse would be over-completed

    Returns:
        (score, arabic_verse, verified_translation) or None.
    """
    if not quran_matches:
        return None

    score, ar_verse, translation = quran_matches[0]  # sorted by score desc
    if score < RAG_HARD_MATCH_THRESHOLD:
        return None

    if target_lang_code != "ar":
        target_dict = get_quran_dict(target_lang_code)
        if ar_verse not in target_dict:
            return None
        translation = target_dict[ar_verse]

    verse_words = len(normalize_arabic(ar_verse).split())
    if verse_words == 0:
        return None
    segment_words = len(normalize_arabic(arabic_text).split())
    ratio = segment_words / verse_words
    word_diff = abs(segment_words - verse_words)
    if (
        not (
            RAG_HARD_MATCH_MIN_LENGTH_RATIO <= ratio <= RAG_HARD_MATCH_MAX_LENGTH_RATIO
        )
        or word_diff > RAG_HARD_MATCH_MAX_WORD_DIFF
    ):
        log(
            f"Quran hard match rejected by length guard "
            f"(ratio={ratio:.2f}, word_diff={word_diff}, score={score:.3f})",
            level="DEBUG",
        )
        return None

    return score, ar_verse, translation


# Trailing (surah:ayah) reference in dictionary translations, e.g. "... (81:7)"
_AYAH_REF_RE = re.compile(r"\((\d+):(\d+)\)\s*$")


def _parse_ayah_ref(translation: str) -> tuple[int, int] | None:
    """Parse the trailing (surah:ayah) reference from a dictionary translation."""
    match = _AYAH_REF_RE.search(translation)
    if match:
        return int(match.group(1)), int(match.group(2))
    return None


def _select_verified_verse_run(
    quran_matches: list, arabic_text: str, target_lang_code: str
) -> str | None:
    """Verify a run of consecutive ayat recited back-to-back in one segment.

    The single-verse bypass cannot fire when a segment contains several
    complete verses: the blended embedding lowers every verse's score below
    the hard threshold and the length guard rejects any single verse. Here
    the embedding matches only NOMINATE candidates — certification is an
    exact-text check. Candidates that are consecutive ayat of the same surah
    (refs parsed from the German reference dictionary, which every RAG
    candidate is guaranteed to be in) are concatenated in ayah order and
    fuzzy-compared against the normalized segment. A run qualifies only when:
    - text similarity >= RAG_MULTI_VERSE_TEXT_SIMILARITY
    - the single-verse length guards pass against the run total (sermon
      speech around the verses must never be silently dropped)
    - every verse has a real target-language dictionary entry

    Returns:
        The joined verified translations in ayah order, or None.
    """
    if len(quran_matches) < 2:
        return None

    target_dict = get_quran_dict(target_lang_code) if target_lang_code != "ar" else None

    # (surah, ayah) -> (score, arabic_verse, output_translation)
    candidates: dict[tuple[int, int], tuple[float, str, str]] = {}
    for score, ar_verse, _hint in quran_matches:
        ref = _parse_ayah_ref(quran_dict.get(ar_verse, ""))
        if ref is None:
            continue
        if target_dict is None:
            translation = ar_verse
        elif ar_verse in target_dict:
            translation = target_dict[ar_verse]
        else:
            continue
        candidates[ref] = (score, ar_verse, translation)

    if len(candidates) < 2:
        return None

    # All runs of >= 2 consecutive ayat within one surah (sub-runs included,
    # so a spurious neighbor candidate cannot block the real pair).
    runs: list[list[tuple[int, int]]] = []
    by_surah: dict[int, list[int]] = {}
    for surah, ayah in candidates:
        by_surah.setdefault(surah, []).append(ayah)
    for surah, ayahs in by_surah.items():
        ayahs.sort()
        start = 0
        for i in range(1, len(ayahs) + 1):
            if i == len(ayahs) or ayahs[i] != ayahs[i - 1] + 1:
                streak = ayahs[start:i]
                for length in range(len(streak), 1, -1):
                    for offset in range(len(streak) - length + 1):
                        runs.append(
                            [(surah, a) for a in streak[offset : offset + length]]
                        )
                start = i

    runs.sort(key=lambda run: (-len(run), -sum(candidates[r][0] for r in run)))

    segment_norm = normalize_arabic(arabic_text)
    segment_words = len(segment_norm.split())

    for run in runs:
        run_norm = normalize_arabic(" ".join(candidates[ref][1] for ref in run))
        run_words = len(run_norm.split())
        if run_words == 0:
            continue
        ratio = segment_words / run_words
        word_diff = abs(segment_words - run_words)
        if (
            not (
                RAG_HARD_MATCH_MIN_LENGTH_RATIO
                <= ratio
                <= RAG_HARD_MATCH_MAX_LENGTH_RATIO
            )
            or word_diff > RAG_HARD_MATCH_MAX_WORD_DIFF
        ):
            continue

        refs = " ".join(f"({s}:{a})" for s, a in run)
        text_sim = SequenceMatcher(None, run_norm, segment_norm).ratio()
        if text_sim < RAG_MULTI_VERSE_TEXT_SIMILARITY:
            log(
                f"Quran verse run rejected by text check "
                f"(refs={refs}, text={text_sim:.2f})",
                level="DEBUG",
            )
            continue

        log(
            f"Quran verse run verified ({len(run)} verses {refs}, "
            f"text={text_sim:.2f}) → exact dictionary translations, GPT skipped",
            level="INFO",
        )
        return " ".join(candidates[ref][2] for ref in run)

    return None


def _build_quran_hint(quran_matches: list, target_lang: str) -> str:
    """Build the Quran RAG hint section for the prompt."""
    if not quran_matches:
        return ""

    blocks = []
    for idx, (score, ar_quran, ref_trans) in enumerate(quran_matches, start=1):
        # Note: ref_trans contains the stored reference translation (currently German)
        # For other languages, GPT will adapt based on the Arabic original
        blocks.append(
            f"""
Candidate {idx} (Score={score:.3f}):

Arabic Quran verse (candidate):
{ar_quran}

Reference translation (use as guide, adapt to {target_lang} if needed):
{ref_trans}
        """.strip()
        )

    return (
        f"""
NOTE – POSSIBLE QURAN VERSES:

The source text MAY contain Arabic Quran verses. The verses below are candidates detected via semantic matching.
Rules for usage:

- Use the provided reference translation ONLY if you recognize that this verse actually appears
  (completely or very clearly) in the "Source Text" section.
- If the corresponding verse does NOT appear in the current section, do NOT include its translation.
- Do NOT add additional Quran verses from this list if they don't appear in the current text.
- Adapt the reference translation to natural {target_lang} if needed, but preserve the meaning exactly.

Candidates:
""".strip()
        + "\n\n"
        + "\n\n".join(blocks)
    )


def translate_text(
    text: str,
    context: str = "",
    arabic_text: str = "",
    model: str | None = None,
    provider: str | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
) -> str:
    """
    Translate mosque audio transcription to the configured target language.

    Pipeline:
    1. Same-language mode: Return transcription directly (no GPT)
    2. Check for Athan phrases (direct dictionary match)
    3. Find potential Quran verses via RAG
    3b. Hard-verified verse (score >= RAG_HARD_MATCH_THRESHOLD and segment is
        essentially the verse alone): return the exact dictionary translation
        with the QURAN_VERIFIED_MARKER prefix, no GPT call
    3c. Multi-verse run: several complete consecutive ayat in one segment,
        certified by exact text comparison against the concatenated
        dictionary verses (see _select_verified_verse_run), no GPT call
    4. Use GPT for final translation with Quran hints

    Islamic mode off (settings.islamic_mode=False): steps 2-3b are skipped
    entirely and step 4 runs with a neutral professional-translator prompt —
    the app behaves as a general live translation tool.

    Args:
        text: Transcribed text in the source language.
        context: Previous transcriptions for context (not translated).
        arabic_text: Optional Arabic re-transcription of the same audio segment.
                     When provided and source language is not Arabic, this is used
                     for Athan/Quran RAG matching so that Arabic Quran verses and
                     Athan phrases are reliably detected even during non-Arabic sermons.
        model: Optional explicit translation model to lead the fallback chain
               (batch mode lets the user pick one per run); None uses settings.
        provider: Optional explicit translation provider id (batch per-run
               choice); None uses the configured ai_provider.
        source_language / target_language: Optional explicit language names
               (batch per-run choice); None uses settings.

    Returns:
        Translation in the configured target language, or transcription for same-language mode.
    """
    txt = (text or "").strip()
    if not txt:
        return ""

    source_lang = source_language or _get_source_language()
    target_lang = target_language or _get_target_language()
    target_lang_code = get_target_language_code(target_lang) or "de"
    islamic_mode = load_settings().islamic_mode

    # Use dedicated Arabic transcription for matching when available,
    # otherwise fall back to the main transcription (e.g. when source is Arabic).
    arabic_txt = (arabic_text or "").strip() or txt

    # --- 0) Same-language mode: skip translation ---
    if _is_same_language(source_lang, target_lang, target_lang_code, txt):
        log(
            f"Same-language mode ({source_lang}): returning transcription directly",
            level="INFO",
        )

        # For Arabic, try to match canonical Athan phrases
        if target_lang_code == "ar" and islamic_mode:
            score_athan, athan_canonical, ar_athan = fuzzy_match_athan(arabic_txt, "ar")
            if score_athan >= ATHAN_MATCH_THRESHOLD and athan_canonical:
                log(
                    f"Athan canonical match: '{ar_athan}' (Score={score_athan:.2f})",
                    level="INFO",
                )
                return athan_canonical

        # Return transcription as-is
        return txt

    quran_hint = ""
    if islamic_mode:
        # --- 1) Athan detection via dictionary ---
        # Check if we have a direct translation for the target language
        score_athan, athan_trans, ar_athan = fuzzy_match_athan(
            arabic_txt, target_lang_code
        )
        if score_athan >= ATHAN_MATCH_THRESHOLD and athan_trans:
            log(
                f"Athan detected: '{ar_athan}' → '{athan_trans}' "
                f"(Score={score_athan:.2f})",
                level="INFO",
            )
            # If we have a direct translation for this language, return it
            if has_athan_translation(target_lang_code):
                return athan_trans
            # Otherwise, fall through to GPT for translation

        # --- 2) Quran detection via RAG (multiple matches) ---
        quran_matches = match_quran_rag_multi(
            arabic_txt, target_lang_code=target_lang_code
        )

        # --- 2b) Hard-verified verse bypass: exact dictionary translation ---
        verified = _select_verified_verse(quran_matches, arabic_txt, target_lang_code)
        if verified is not None:
            score, ar_verse, verse_translation = verified
            log(
                f"Quran verse verified (Score={score:.3f}) → "
                "exact dictionary translation, GPT skipped",
                level="INFO",
            )
            return f"{QURAN_VERIFIED_MARKER} {verse_translation}"

        # --- 2c) Multi-verse run: consecutive complete ayat in one segment ---
        verified_run = _select_verified_verse_run(
            quran_matches, arabic_txt, target_lang_code
        )
        if verified_run is not None:
            return f"{QURAN_VERIFIED_MARKER} {verified_run}"

        quran_hint = _build_quran_hint(quran_matches, target_lang)

        log(
            f"Quran-RAG hints generated with {len(quran_matches)} candidates.",
            level="DEBUG",
        )

    # --- 3) GPT Translation with model fallback ---
    if islamic_mode:
        system_prompt = _build_system_prompt(source_lang, target_lang)
        user_prompt = _build_user_prompt(
            txt, context, quran_hint, source_lang, target_lang
        )
    else:
        system_prompt = _build_general_system_prompt(source_lang, target_lang)
        user_prompt = _build_general_user_prompt(txt, context, source_lang, target_lang)

    # Provider-aware chain: user's model first (if valid for the active
    # provider), then that provider's fallbacks. A caller (e.g. batch mode)
    # may pin an explicit provider and/or model per run.
    if provider:
        translation_provider = get_translation_provider_for(provider)
        models_to_try = get_translation_model_chain_for(provider, model)
    else:
        translation_provider = get_translation_provider()
        models_to_try = get_translation_model_chain()
        if model:
            models_to_try = [model, *[m for m in models_to_try if m != model]]

    last_error = None
    for model in models_to_try:
        try:
            log(f"Trying model: {model}", level="DEBUG")

            def _call_translation_api(model=model):
                return translation_provider.complete(
                    model=model,
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                )

            translation = retry_with_backoff(
                _call_translation_api,
                max_retries=2,  # Fewer retries per model since we have fallbacks
                operation_name=f"Translation ({model})",
            )
            translation = _strip_arabic_leak(translation, target_lang_code)
            log(
                f"TRANSLATOR Final output ({target_lang}): {translation}", level="DEBUG"
            )
            return translation

        except Exception as e:
            last_error = e
            log(f"Model {model} failed: {e}", level="WARNING")
            continue  # Try next model

    # All models failed
    log(f"All translation models failed. Last error: {last_error}", level="ERROR")
    return get_user_message("translation_unavailable")

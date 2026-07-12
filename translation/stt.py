"""Shared transcription helpers for the live segmented and batch pipelines.

Both pipelines transcribe a WAV segment through the provider's model-fallback
chain and, for non-Arabic sources in Islamic mode, run a secondary Arabic
transcription of the same audio to feed the Quran/Athan matchers. The logic
lived near-verbatim in app_controller._process_audio and batch/processor —
this module is the single copy. Deliberately parameter-driven (no settings
reads in here): the call sites resolve provider, models and flags themselves.
"""

from __future__ import annotations

from difflib import SequenceMatcher

from config import MIN_TRANSLATABLE_LETTERS, SEGMENT_OVERLAP_MAX_WORDS
from translation.dictionary import normalize_arabic
from translation.translator import is_arabic_script
from utils.logging import log
from utils.retry import retry_with_backoff

# Punctuation stripped from a word's edges before overlap comparison: the STT
# renders the same overlapped word with or without a trailing "." / "،" on
# each pass ("الخالق." vs "الخالق"), which an exact match would miss.
_EDGE_PUNCT = "؟،؛.!?…\"'“”«»()[]{}:؛-–—"
# Two words are treated as the same overlapped word when they normalize equal
# or are near-identical — the STT transcribes the overlap region slightly
# differently each pass (a dropped letter, a different hamza seat).
_FUZZY_WORD_RATIO = 0.85


def has_min_letters(text: str, minimum: int = MIN_TRANSLATABLE_LETTERS) -> bool:
    """True if ``text`` has at least ``minimum`` alphabetic characters.

    The fragment gate: a bare "م" (renders as "h") or a near-silent "Um"
    is not worth a translation call. Works for Arabic and Latin scripts —
    both count as alphabetic.
    """
    return sum(1 for ch in text if ch.isalpha()) >= minimum


def strip_overlap_prefix(
    previous: str, current: str, max_words: int = SEGMENT_OVERLAP_MAX_WORDS
) -> str:
    """Strip the leading words of ``current`` that repeat ``previous``'s tail.

    Live segments overlap by OVERLAP seconds, so ``current`` starts with the
    words that ended ``previous`` — a visible duplicate on every boundary.
    Words are compared after Arabic normalization (diacritics/letter variants)
    and case-folding, but stripped from the raw text so nothing else changes.

    The match is capped at ``max_words`` AND at ``len(current) - 1`` so the
    segment is never emptied: a real overlap is only ~OVERLAP/DURATION of the
    segment, so a full-length "overlap" would be a genuine repetition, not a
    boundary artifact — leaving it in loses no content.
    """
    prev_words = previous.split()
    cur_words = current.split()
    if not prev_words or len(cur_words) < 2:
        return current

    def _norm(word: str) -> str:
        return normalize_arabic(word).strip(_EDGE_PUNCT).lower()

    def _same_word(a: str, b: str) -> bool:
        if a == b:
            return True
        # Only fuzzy-match words long enough that a high ratio is meaningful —
        # short function words ("في", "و") must match exactly or unrelated
        # segments would look overlapped.
        if len(a) >= 3 and len(b) >= 3:
            return SequenceMatcher(None, a, b).ratio() >= _FUZZY_WORD_RATIO
        return False

    prev_norm = [_norm(w) for w in prev_words]
    cur_norm = [_norm(w) for w in cur_words]
    limit = min(max_words, len(prev_norm), len(cur_words) - 1)

    overlap = 0
    for k in range(1, limit + 1):
        if all(
            _same_word(p, c) for p, c in zip(prev_norm[-k:], cur_norm[:k], strict=False)
        ):
            overlap = k
    if overlap == 0:
        return current
    return " ".join(cur_words[overlap:])


def transcribe_with_fallback(
    provider,
    models_to_try: list[str],
    audio_wav: bytes,
    language: str | None,
    *,
    prompt: str | None = None,
    log_prefix: str = "",
) -> str | None:
    """Transcribe one segment, falling back through the model chain.

    ``prompt`` optionally carries the tail of the previous transcription for
    cross-segment continuity (batch prompt-chaining). Returns None when every
    model in the chain failed.
    """
    last_error = None
    for model in models_to_try:
        try:
            log(f"{log_prefix}Trying transcription model: {model}", level="DEBUG")

            def _call(audio=audio_wav, model=model, language=language, prompt=prompt):
                return provider.transcribe(
                    audio, model=model, language=language, prompt=prompt
                )

            return retry_with_backoff(
                _call,
                max_retries=2,  # Fewer retries since we have fallbacks
                operation_name=f"{log_prefix}Transcription ({model})",
            )
        except Exception as e:
            last_error = e
            log(
                f"{log_prefix}Transcription model {model} failed: {e}",
                level="WARNING",
            )
    log(
        f"{log_prefix}All transcription models failed. Last error: {last_error}",
        level="ERROR",
    )
    return None


def maybe_arabic_retranscription(
    provider,
    model: str,
    audio_wav: bytes,
    *,
    transcription: str,
    source_lang_code: str | None,
    source_language: str,
    target_language: str,
    islamic_mode: bool,
    log_prefix: str = "",
) -> str:
    """Secondary Arabic transcription for the Quran/Athan matchers, or "".

    Skipped when it would be pure cost: the source already is Arabic, Islamic
    mode is off (the matchers don't run), source and target are the same
    language (translation is bypassed entirely), or the primary transcription
    already came back in Arabic script (the matchers use it directly).
    Failures degrade to "" — the segment still translates, just without hints.
    """
    if (
        source_lang_code == "ar"
        or source_language == target_language
        or not islamic_mode
        or is_arabic_script(transcription)
    ):
        return ""
    try:

        def _call(audio=audio_wav, model=model):
            return provider.transcribe(audio, model=model, language="ar")

        return retry_with_backoff(
            _call,
            max_retries=1,
            operation_name=f"{log_prefix}Arabic transcription (RAG)",
        )
    except Exception as e:
        log(
            f"{log_prefix}Arabic re-transcription failed (RAG skipped): {e}",
            level="WARNING",
        )
        return ""

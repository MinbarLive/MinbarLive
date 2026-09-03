"""OpenAI speech-to-text provider (gpt-4o-transcribe family, whisper-1)."""

from __future__ import annotations

from providers.openai.client import get_client
from utils.cost_tracking import record_openai_transcription_usage

# Source-language codes the file-transcription endpoint accepts.
#
# NOT the Whisper 100 the docs point at. The endpoint validates ``language``
# against a narrower list and refuses anything outside it with a 400 naming
# the parameter ("Language code 'sq' is not recognized"), before any audio is
# read. Measured against the live API 2026-09-03: 63 codes, identical on
# gpt-4o-transcribe and gpt-4o-mini-transcribe -- the only two models the
# dropdown offers. The previous 100-code set was the documented Whisper list,
# and four languages in it (sq, ha, ps, so) are rejected outright.
#
# Kept separate from the Realtime allowlist in realtime.py even though that
# set is currently this one plus 'iw' (Hebrew's legacy alias). The two have
# already diverged in each direction, and each is measured against its own
# endpoint; deriving one from the other is what put a rejected language in
# the dropdown in the first place.
#
# CAVEAT: whisper-1, last in FALLBACK_TRANSCRIPTION_MODELS, is narrower still
# (57 codes; it also rejects bn, gu, ka, ml, te, yue). No dropdown offers it,
# so it cannot be selected -- but a segment in one of those six that falls all
# the way through the chain to whisper-1 fails there rather than transcribing.
#
# Re-derive by probing, never from the docs: one 1-second tone per code is
# enough. Acceptance means "the API takes the code", not "it is good at it".
# These are data, not code: a grid of ISO codes is scanned by eye against an
# API's own list, and one code per line makes that impossible to do and the
# diff between two engine sets unreadable. Keep the formatter off it.
# fmt: off
SUPPORTED_LANGUAGE_CODES = frozenset(
    {
        "af", "ar", "az", "be", "bg", "bn", "bs", "ca", "cs", "cy",
        "da", "de", "el", "en", "es", "et", "fa", "fi", "fr", "gl",
        "gu", "he", "hi", "hr", "hu", "hy", "id", "is", "it", "ja",
        "ka", "kk", "kn", "ko", "lt", "lv", "mi", "mk", "ml", "mr",
        "ms", "ne", "nl", "no", "pl", "pt", "ro", "ru", "sk", "sl",
        "sr", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur",
        "vi", "yue", "zh",
    }
)
# fmt: on


class OpenAITranscriptionProvider:
    """Implements providers.base.TranscriptionProvider."""

    def transcribe(
        self,
        audio_wav: bytes,
        *,
        model: str,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        kwargs = {
            "model": model,
            "file": ("audio.wav", audio_wav),
            # JSON returns the same transcript text as "text" but also exposes
            # the provider's token/duration usage, which the cost counter reads.
            "response_format": "json",
        }
        if language:  # None/empty means auto-detect
            kwargs["language"] = language
        if prompt:  # tail of the preceding transcript, for continuity
            kwargs["prompt"] = prompt

        result = get_client().audio.transcriptions.create(**kwargs)
        # Older SDKs and test doubles may still hand back a plain string.
        if isinstance(result, str):
            return result
        record_openai_transcription_usage(getattr(result, "usage", None), model=model)
        return str(getattr(result, "text", result))

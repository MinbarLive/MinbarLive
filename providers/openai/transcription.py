"""OpenAI speech-to-text provider (gpt-4o-transcribe family, whisper-1)."""

from __future__ import annotations

from providers.openai.client import get_client
from utils.cost_tracking import record_openai_transcription_usage

# Source-language codes the file-transcription endpoint accepts: the Whisper
# language set, which the gpt-4o-transcribe family shares (the docs point at
# the Whisper list for whisper-1 and give no separate one for gpt-transcribe).
#
# Deliberately WIDER than the Realtime session's allowlist in
# providers/openai/realtime.py — Somali, Pashto, Bengali, Hausa and Albanian
# transcribe here and are rejected there, which is why the two sets are
# separate constants instead of one "OpenAI" set. Accuracy varies a lot across
# the tail of this list; support here means "the API accepts it", not "it is
# good at it".
SUPPORTED_LANGUAGE_CODES = frozenset(
    {
        "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo",
        "br", "bs", "ca", "cs", "cy", "da", "de", "el", "en", "es",
        "et", "eu", "fa", "fi", "fo", "fr", "gl", "gu", "ha", "haw",
        "he", "hi", "hr", "ht", "hu", "hy", "id", "is", "it", "ja",
        "jw", "ka", "kk", "km", "kn", "ko", "la", "lb", "ln", "lo",
        "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
        "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt",
        "ro", "ru", "sa", "sd", "si", "sk", "sl", "sn", "so", "sq",
        "sr", "su", "sv", "sw", "ta", "te", "tg", "th", "tk", "tl",
        "tr", "tt", "uk", "ur", "uz", "vi", "yi", "yo", "yue", "zh",
    }
)


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

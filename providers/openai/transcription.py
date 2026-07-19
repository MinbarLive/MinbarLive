"""OpenAI speech-to-text provider (gpt-4o-transcribe family, whisper-1)."""

from __future__ import annotations

from providers.openai.client import get_client
from utils.cost_tracking import record_openai_transcription_usage


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
            # JSON keeps the exact same text result while also exposing the
            # provider's token/duration usage metadata for the cost counter.
            "response_format": "json",
        }
        if language:  # None/empty means auto-detect
            kwargs["language"] = language
        if prompt:  # tail of the preceding transcript, for continuity
            kwargs["prompt"] = prompt

        result = get_client().audio.transcriptions.create(**kwargs)
        # Keep accepting a plain string for older SDKs and test doubles.
        if isinstance(result, str):
            return result
        record_openai_transcription_usage(getattr(result, "usage", None), model=model)
        return str(getattr(result, "text", result))

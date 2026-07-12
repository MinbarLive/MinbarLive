"""OpenAI speech-to-text provider (gpt-4o-transcribe family, whisper-1)."""

from __future__ import annotations

from providers.openai.client import get_client


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
            "response_format": "text",
        }
        if language:  # None/empty means auto-detect
            kwargs["language"] = language
        if prompt:  # tail of the preceding transcript, for continuity
            kwargs["prompt"] = prompt

        result = get_client().audio.transcriptions.create(**kwargs)
        # response_format="text" yields a plain string
        return result if isinstance(result, str) else str(result)

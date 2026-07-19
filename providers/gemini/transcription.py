"""Gemini speech-to-text provider.

Gemini has no dedicated transcription endpoint; audio is passed to
generate_content with a verbatim-transcription instruction.
"""

from __future__ import annotations

from providers.gemini.client import get_client
from utils.cost_tracking import record_gemini_response

_INSTRUCTION = (
    "Transcribe the speech in this audio recording verbatim. "
    "Output ONLY the transcribed text in the original spoken language — "
    "no commentary, no timestamps, no speaker labels, no translation. "
    "If there is no intelligible speech, output nothing."
)


class GeminiTranscriptionProvider:
    """Implements providers.base.TranscriptionProvider."""

    def transcribe(
        self,
        audio_wav: bytes,
        *,
        model: str,
        language: str | None = None,
        prompt: str | None = None,
    ) -> str:
        from google.genai import types

        instruction = _INSTRUCTION
        if language:  # None/empty means auto-detect
            instruction += f" The speech is in language '{language}' (ISO 639-1)."
        if prompt:  # tail of the preceding transcript, for continuity
            instruction += (
                " For context, the transcript of the immediately preceding "
                f'audio was: "{prompt}". Continue from there; do not repeat it.'
            )

        resp = get_client().models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=audio_wav, mime_type="audio/wav"),
                instruction,
            ],
            # Verbatim transcription needs no reasoning; thinking only adds
            # latency on the Gemini 3.x models (see translation.py).
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_budget=0)
            ),
        )
        record_gemini_response(resp, model=model, role="transcription")
        return (resp.text or "").strip()

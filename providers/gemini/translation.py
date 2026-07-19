"""Gemini text generation provider (translation and summarization)."""

from __future__ import annotations

from providers.gemini.client import get_client
from utils.cost_tracking import record_gemini_response


class GeminiTranslationProvider:
    """Implements providers.base.TranslationProvider."""

    def complete(
        self,
        *,
        model: str,
        user_prompt: str,
        system_prompt: str | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        from google.genai import types

        config_kwargs = {
            # Gemini 3.x flash models think by default, which multiplies
            # latency (live-probed 2026-07-15: 4.6s → 2.3s on 3.5-flash).
            # Live subtitles can't afford it — same decision as Anthropic's
            # disabled extended thinking.
            "thinking_config": types.ThinkingConfig(thinking_budget=0),
        }
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt
        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            config_kwargs["temperature"] = temperature

        resp = get_client().models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        record_gemini_response(resp, model=model, role="translation")
        return (resp.text or "").strip()

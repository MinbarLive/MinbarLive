"""Gemini text generation provider (translation and summarization)."""

from __future__ import annotations

from providers.gemini.client import get_client


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

        config_kwargs = {}
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt
        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            config_kwargs["temperature"] = temperature

        resp = get_client().models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(**config_kwargs)
            if config_kwargs
            else None,
        )
        return (resp.text or "").strip()

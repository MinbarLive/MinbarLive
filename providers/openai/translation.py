"""OpenAI text generation provider (translation and summarization)."""

from __future__ import annotations

from providers.openai.client import create_chat_completion
from utils.cost_tracking import record_openai_chat_response
from utils.logging import log


class OpenAITranslationProvider:
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
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        kwargs = {}
        if temperature is not None:
            kwargs["temperature"] = temperature

        resp = create_chat_completion(
            model=model,
            messages=messages,
            max_output_tokens=max_output_tokens,
            **kwargs,
        )
        record_openai_chat_response(resp, model=model)
        choice = resp.choices[0]
        # On reasoning models the hidden reasoning tokens count against the
        # output budget too — a silent cutoff looks like a model bug upstream.
        if getattr(choice, "finish_reason", None) == "length":
            log(
                f"OpenAI completion truncated by max_output_tokens ({model})",
                level="WARNING",
            )
        return (choice.message.content or "").strip()

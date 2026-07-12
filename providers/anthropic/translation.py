"""Anthropic Claude text generation provider (translation and summarization)."""

from __future__ import annotations

from providers.anthropic.client import get_client

# The Messages API requires max_tokens. Subtitle translations and context
# summaries are short; callers needing tighter bounds pass max_output_tokens.
_DEFAULT_MAX_OUTPUT_TOKENS = 2048


class AnthropicTranslationProvider:
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
        # temperature is deliberately not forwarded: Claude Sonnet 5 rejects
        # non-default sampling parameters (400) — steering happens via prompts.
        kwargs = {}
        if system_prompt:
            kwargs["system"] = system_prompt

        resp = get_client().messages.create(
            model=model,
            max_tokens=max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS,
            # Sonnet 5 runs adaptive thinking when the field is omitted —
            # latency and token cost live subtitles can't afford.
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": user_prompt}],
            **kwargs,
        )
        return "".join(
            block.text for block in resp.content if block.type == "text"
        ).strip()

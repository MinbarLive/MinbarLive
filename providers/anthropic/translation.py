"""Anthropic Claude text generation provider (translation and summarization)."""

from __future__ import annotations

from collections.abc import Callable

from providers.anthropic.client import get_client
from utils.logging import log

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
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        # temperature is deliberately not forwarded: Claude Sonnet 5 rejects
        # non-default sampling parameters (400) — steering happens via prompts.
        kwargs = {}
        if system_prompt:
            kwargs["system"] = system_prompt

        if on_delta is not None:
            return self._complete_streaming(
                model=model,
                user_prompt=user_prompt,
                max_output_tokens=max_output_tokens,
                on_delta=on_delta,
                **kwargs,
            )

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

    def _complete_streaming(
        self,
        *,
        model: str,
        user_prompt: str,
        max_output_tokens: int | None,
        on_delta: Callable[[str], None],
        **kwargs,
    ) -> str:
        """Stream via the Messages API, calling on_delta per text delta.

        NOTE: untested against a live key (no Anthropic key available during
        development). The blocking fallback below guarantees a live session
        still translates if anything in the streaming path misbehaves.
        """
        try:
            parts: list[str] = []
            with get_client().messages.stream(
                model=model,
                max_tokens=max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": user_prompt}],
                **kwargs,
            ) as stream:
                for text in stream.text_stream:
                    if text:
                        parts.append(text)
                        on_delta(text)
            return "".join(parts).strip()
        except Exception as e:
            log(
                f"Anthropic streaming failed ({model}), falling back to "
                f"blocking: {e}",
                level="WARNING",
            )
            resp = get_client().messages.create(
                model=model,
                max_tokens=max_output_tokens or _DEFAULT_MAX_OUTPUT_TOKENS,
                thinking={"type": "disabled"},
                messages=[{"role": "user", "content": user_prompt}],
                **kwargs,
            )
            return "".join(
                block.text for block in resp.content if block.type == "text"
            ).strip()

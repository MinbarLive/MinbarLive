"""Gemini text generation provider (translation and summarization)."""

from __future__ import annotations

from collections.abc import Callable

from providers.gemini.client import get_client
from providers.gemini.thinking import THINKING_LEVEL as _THINKING_LEVEL
from utils.cost_tracking import record_gemini_response
from utils.logging import log


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
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        from google.genai import types

        config_kwargs = {
            # Gemini 3.x flash models think by default, which multiplies
            # latency (live-probed 2026-07-15: 4.6s → 2.3s on 3.5-flash).
            # Live subtitles can't afford it — same decision as Anthropic's
            # disabled extended thinking.
            #
            # thinking_level, NOT thinking_budget: the newer models dropped
            # the budget field and reject it outright (live-probed
            # 2026-07-22: gemini-3.6-flash and gemini-3.5-flash-lite both
            # return 400 INVALID_ARGUMENT for thinking_budget=0, while
            # thinking_level="minimal" works on every model we offer — and is
            # faster than sending nothing at all: 3.17s → 1.23s on 3.6-flash).
            "thinking_config": types.ThinkingConfig(thinking_level=_THINKING_LEVEL),
        }
        if system_prompt:
            config_kwargs["system_instruction"] = system_prompt
        if max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            config_kwargs["temperature"] = temperature

        config = types.GenerateContentConfig(**config_kwargs)

        if on_delta is not None:
            return self._complete_streaming(model, user_prompt, config, on_delta)

        resp = get_client().models.generate_content(
            model=model,
            contents=user_prompt,
            config=config,
        )
        record_gemini_response(resp, model=model, role="translation")
        return (resp.text or "").strip()

    def _complete_streaming(self, model, user_prompt, config, on_delta) -> str:
        """Stream the completion, calling on_delta per text chunk.

        The last streamed chunk carries the full usage_metadata, so cost
        tracking stays intact. Falls back to blocking on any streaming error so
        a live session degrades to "waits, but still translates".
        """
        try:
            parts: list[str] = []
            last_chunk = None
            for chunk in get_client().models.generate_content_stream(
                model=model, contents=user_prompt, config=config
            ):
                last_chunk = chunk
                text = chunk.text or ""
                if text:
                    parts.append(text)
                    on_delta(text)
            if last_chunk is not None:
                record_gemini_response(last_chunk, model=model, role="translation")
            return "".join(parts).strip()
        except Exception as e:
            log(
                f"Gemini streaming failed ({model}), falling back to blocking: {e}",
                level="WARNING",
            )
            resp = get_client().models.generate_content(
                model=model, contents=user_prompt, config=config
            )
            record_gemini_response(resp, model=model, role="translation")
            return (resp.text or "").strip()

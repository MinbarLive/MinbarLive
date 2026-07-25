"""OpenAI text generation provider (translation and summarization)."""

from __future__ import annotations

from collections.abc import Callable

from providers.openai.client import create_chat_completion, get_client
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
        on_delta: Callable[[str], None] | None = None,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        kwargs = {}
        if temperature is not None:
            kwargs["temperature"] = temperature

        if on_delta is not None:
            return self._complete_streaming(
                model=model,
                messages=messages,
                max_output_tokens=max_output_tokens,
                on_delta=on_delta,
                **kwargs,
            )

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

    def _complete_streaming(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None,
        on_delta: Callable[[str], None],
        **kwargs,
    ) -> str:
        """Stream the completion, calling on_delta per text fragment.

        ``include_usage`` makes the API append a final usage-only chunk (empty
        choices) so cost tracking keeps working; without it a streamed call
        reports no tokens. On any streaming error, fall back to the blocking
        path so a live session degrades to "waits, but still translates" rather
        than losing the subtitle.
        """
        payload = dict(messages=messages, **kwargs)
        # max_completion_tokens is the newer param; translation calls pass None,
        # so keep this simple and only forward it when set.
        if max_output_tokens is not None:
            payload["max_completion_tokens"] = max_output_tokens
        try:
            stream = get_client().chat.completions.create(
                model=model,
                stream=True,
                stream_options={"include_usage": True},
                **payload,
            )
            parts: list[str] = []
            usage_chunk = None
            for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage_chunk = chunk
                if not chunk.choices:
                    continue  # final usage-only chunk
                delta = chunk.choices[0].delta
                text = getattr(delta, "content", None)
                if text:
                    parts.append(text)
                    on_delta(text)
            if usage_chunk is not None:
                record_openai_chat_response(usage_chunk, model=model)
            return "".join(parts).strip()
        except Exception as e:
            log(
                f"OpenAI streaming failed ({model}), falling back to blocking: {e}",
                level="WARNING",
            )
            resp = create_chat_completion(model=model, **payload)
            record_openai_chat_response(resp, model=model)
            return (resp.choices[0].message.content or "").strip()

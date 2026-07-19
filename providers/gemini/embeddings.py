"""Gemini text embedding provider (gemini-embedding family).

Only used when the Gemini verse matrix exists (see
providers.get_embedding_space()) — query embeddings must live in the same
vector space as the precomputed verse embeddings.
"""

from __future__ import annotations

from providers.gemini.client import get_client
from utils.cost_tracking import record_unpriced_provider_request


class GeminiEmbeddingProvider:
    """Implements providers.base.EmbeddingProvider."""

    def embed(self, text: str, *, model: str) -> list[float]:
        resp = get_client().models.embed_content(model=model, contents=text)
        # google-genai 2.10 does not expose Developer API embedding token usage.
        # Retain the request as unpriced so the UI never presents an incomplete
        # Gemini total as exact (and avoid a latency-adding count_tokens call).
        record_unpriced_provider_request(
            provider="gemini", role="embedding", model=model
        )
        return list(resp.embeddings[0].values)

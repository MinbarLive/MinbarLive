"""Gemini text embedding provider (gemini-embedding family).

Only used when the Gemini verse matrix exists (see
providers.get_embedding_space()) — query embeddings must live in the same
vector space as the precomputed verse embeddings.
"""

from __future__ import annotations

from providers.gemini.client import get_client


class GeminiEmbeddingProvider:
    """Implements providers.base.EmbeddingProvider."""

    def embed(self, text: str, *, model: str) -> list[float]:
        resp = get_client().models.embed_content(model=model, contents=text)
        return list(resp.embeddings[0].values)

"""OpenAI text embedding provider (text-embedding-3 family)."""

from __future__ import annotations

from providers.openai.client import get_client


class OpenAIEmbeddingProvider:
    """Implements providers.base.EmbeddingProvider."""

    def embed(self, text: str, *, model: str) -> list[float]:
        resp = get_client().embeddings.create(model=model, input=[text])
        return resp.data[0].embedding

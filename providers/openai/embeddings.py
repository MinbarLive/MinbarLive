"""OpenAI text embedding provider (text-embedding-3 family)."""

from __future__ import annotations

from providers.openai.client import get_client
from utils.cost_tracking import record_openai_embedding_response


class OpenAIEmbeddingProvider:
    """Implements providers.base.EmbeddingProvider."""

    def embed(self, text: str, *, model: str) -> list[float]:
        resp = get_client().embeddings.create(model=model, input=[text])
        record_openai_embedding_response(resp, model=model)
        return resp.data[0].embedding

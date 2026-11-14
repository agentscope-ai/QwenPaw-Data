"""Keep model transport caches and deployment knobs isolated between tests."""

import pytest
from context_manager.model import embedding
from context_manager.model.embedding_cache import EmbeddingCache


@pytest.fixture(autouse=True)
def isolated_embedding_settings(monkeypatch):
    for name in (
        "EMBED_API_BATCH",
        "EMBED_API_CONCURRENCY",
        "EMBED_API_MAX_RETRIES",
        "EMBED_API_MAX_INPUT_CHARS",
        "CM_EMBED_CACHE_TTL_SEC",
        "EMBED_CACHE_MAX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(embedding, "embedding_cache", EmbeddingCache())

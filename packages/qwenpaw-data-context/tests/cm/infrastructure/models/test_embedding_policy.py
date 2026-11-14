"""Verify embedding batching, clipping, caching and rate-limit retries."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from context_manager.model import (
    LlmError,
    OpenAIEmbedCaller,
    embedding,
)


def _caller(**options):
    async def respond(**request):
        return SimpleNamespace(
            data=[
                SimpleNamespace(
                    index=i,
                    embedding=(float(len(text)),) * request.get("dimensions", 2),
                )
                for i, text in enumerate(request["input"])
            ]
        )

    request = AsyncMock(side_effect=respond)

    async def raw_embedding(**options):
        response = await request(**options)
        return SimpleNamespace(parse=lambda: response)

    sdk = SimpleNamespace(
        embeddings=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=raw_embedding),
        )
    )
    config = {
        "model": "model-a",
        "base_url": "https://models.example/v1",
        "dimensions": 2,
    }
    return OpenAIEmbedCaller(**(config | options), client=sdk), request


@pytest.mark.asyncio
async def test_cache_deduplicates_clipped_misses_and_restores_order(monkeypatch):
    monkeypatch.setenv("EMBED_API_MAX_INPUT_CHARS", "100")
    caller, request = _caller()
    prefix = "x" * 100

    assert await caller.embed((prefix + "one", "ab", prefix + "two")) == (
        (100.0, 100.0),
        (2.0, 2.0),
        (100.0, 100.0),
    )
    request.assert_awaited_once_with(
        model="model-a",
        input=[prefix, "ab"],
        dimensions=2,
    )
    assert await caller.embed(("ab", prefix + "three")) == ((2.0, 2.0), (100.0, 100.0))
    assert request.await_count == 1


@pytest.mark.asyncio
async def test_failed_miss_batch_does_not_publish_partial_results():
    caller, request = _caller(batch_size=1, max_concurrency=1)
    original = request.side_effect

    async def respond(**options):
        if options["input"] == ["b"]:
            raise RuntimeError("provider unavailable")
        return await original(**options)

    request.side_effect = respond
    with pytest.raises(LlmError):
        await caller.embed(("a", "b"))
    await caller.embed(("a",))
    assert [call.kwargs["input"] for call in request.await_args_list] == [
        ["a"],
        ["b"],
        ["a"],
    ]


@pytest.mark.parametrize("message", ["HTTP 429", "RateLimitError", "limit_burst_rate"])
@pytest.mark.asyncio
async def test_rate_limit_retries_use_exponential_backoff(message, monkeypatch):
    monkeypatch.setenv("EMBED_API_MAX_RETRIES", "2")
    sleep = AsyncMock()
    monkeypatch.setattr(embedding.asyncio, "sleep", sleep)
    monkeypatch.setattr(embedding.random, "uniform", lambda *_: 0.1)
    caller, request = _caller()
    response = await request.side_effect(model="model-a", input=["a"], dimensions=2)
    request.side_effect = [RuntimeError(message), RuntimeError(message), response]

    assert await caller.embed(("a",)) == ((1.0, 1.0),)
    assert request.await_count == 3
    assert [call.args[0] for call in sleep.await_args_list] == [0.6, 1.1]

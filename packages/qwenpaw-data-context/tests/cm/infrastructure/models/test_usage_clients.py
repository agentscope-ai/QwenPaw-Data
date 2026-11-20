"""Verify metering at real client response boundaries without a live Provider."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from context_manager.model import (
    ChatMessage,
    ModelConfigRepository,
    ModelService,
    ModelUsageTracker,
    OpenAIEmbedCaller,
    OpenAILlmCaller,
    ResolvedModel,
)
from context_manager.settings import get_settings


@pytest.fixture
def metering():
    events = []
    tracker = ModelUsageTracker(
        lambda event: events.append(event) or True,
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        user_id="original-user",
        provider_id="provider",
    )
    return tracker, events


def chat_response(content="answer", tokens=7):
    return SimpleNamespace(
        model="served-model",
        usage=SimpleNamespace(prompt_tokens=tokens, completion_tokens=3),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )


def embedding_sdk(create):
    async def raw_create(**options):
        response = await create(**options)
        return SimpleNamespace(parse=lambda: response)

    return SimpleNamespace(
        embeddings=SimpleNamespace(
            with_raw_response=SimpleNamespace(create=raw_create),
        )
    )


@pytest.mark.asyncio
async def test_embedding_cache_hit_has_no_additional_usage(metering):
    tracker, events = metering
    get_settings().embedding.cache_ttl_seconds = 600
    create = AsyncMock(
        return_value=SimpleNamespace(
            usage=SimpleNamespace(total_tokens=4),
            data=[SimpleNamespace(index=0, embedding=(0.1, 0.2))],
        )
    )
    async with OpenAIEmbedCaller(
        client=embedding_sdk(create),
        model="metering-cache",
        base_url="https://metering.invalid/v1",
        dimensions=2,
        usage_tracker=tracker,
    ) as caller:
        assert await caller.embed(("cached",)) == ((0.1, 0.2),)
        assert await caller.embed(("cached",)) == ((0.1, 0.2),)
    assert create.await_count == len(events) == 1


@pytest.mark.asyncio
async def test_probe_excludes_both_channels_but_next_real_call_is_recorded(
    metering, monkeypatch
):
    _, events = metering
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=AsyncMock(return_value=chat_response()))
        ),
        embeddings=embedding_sdk(
            AsyncMock(
                return_value=SimpleNamespace(
                    usage=SimpleNamespace(total_tokens=2),
                    data=[SimpleNamespace(index=0, embedding=(0.1, 0.2))],
                )
            )
        ).embeddings,
    )
    get_settings().embedding.cache_ttl_seconds = 0
    service = ModelService(
        ModelConfigRepository("tenant-1", "original-user", workspace_id="workspace-1"),
        llm_factory=lambda **options: OpenAILlmCaller(client=sdk, **options),
        embed_factory=lambda **options: OpenAIEmbedCaller(client=sdk, **options),
        enqueue_usage=lambda event: events.append(event) or True,
    )
    config = ResolvedModel(
        "provider", "requested", "https://metering.invalid/v1", "test-key", 2
    )
    monkeypatch.setattr(service, "_candidate", lambda channel, update: ({}, config))
    assert (await service.probe("llm", {}))["success"]
    assert (await service.probe("embedding", {}))["success"]
    assert events == []
    async with service.llm(config) as caller:
        await caller.complete((ChatMessage("user", "question"),))
    assert len(events) == 1

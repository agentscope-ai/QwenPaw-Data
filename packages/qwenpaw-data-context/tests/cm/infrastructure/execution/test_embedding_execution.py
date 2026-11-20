"""Keep SDK decoding and vector validation inside Runtime's bounded workers."""

import asyncio
import base64
import json
import struct
from threading import Event, get_ident

import httpx
import pytest
from context_manager.model import ResolvedModel, embedding
from context_manager.runtime import WorkspaceRuntime
from context_manager.runtime.execution import ExecutionResources
from context_manager.schema import GRAPH_SCHEMA
from context_manager.settings.execution import ExecutionSettings
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import GraphTarget
from openai import AsyncOpenAI

from cm.infrastructure.storage.schema_support import (
    make_glossary,
    relational_database,
)
from cm.metadata.test_snapshot_updates import TransactionStore


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_response_processing_shares_background_budget_and_drains_on_cancel(
    monkeypatch, cancel
):
    monkeypatch.setenv("CM_EMBED_CACHE_TTL_SEC", "0")
    monkeypatch.setenv("EMBED_API_BATCH", "1")
    pools = ExecutionResources(
        ExecutionSettings(
            background={"workers": 1},
        )
    )
    workspace = WorkspaceRuntime(
        GraphTarget("tenant-1", "workspace-1"),
        TransactionStore(),
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
        database_engine=relational_database(),
        executor=pools.background,
        graph_read_executor=pools.graph_read,
    )
    loop = asyncio.get_running_loop()
    owner = get_ident()
    entered, release = asyncio.Event(), Event()
    decoded, validated = [], []
    validate = embedding.require_vector_batch

    class Response(httpx.Response):
        def json(self, **kwargs):
            decoded.append(get_ident())
            assert get_ident() != owner
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(5), "event loop could not release the response worker"
            return super().json(**kwargs)

    def validate_vectors(*args, **kwargs):
        validated.append(get_ident())
        return validate(*args, **kwargs)

    async def respond(request):
        assert get_ident() == owner
        inputs = json.loads(request.content)["input"]
        vector = base64.b64encode(struct.pack("<ff", 1.0, 2.0)).decode()
        return Response(
            200,
            json={
                "data": [
                    {"index": i, "embedding": vector, "object": "embedding"}
                    for i, _ in enumerate(inputs)
                ],
                "model": "fixture",
                "object": "list",
                "usage": {"prompt_tokens": len(inputs), "total_tokens": len(inputs)},
            },
        )

    sdk = AsyncOpenAI(
        api_key="fixture",
        http_client=httpx.AsyncClient(
            transport=httpx.MockTransport(respond),
        ),
    )
    monkeypatch.setattr(embedding, "AsyncOpenAI", lambda **kwargs: sdk)
    monkeypatch.setattr(embedding, "require_vector_batch", validate_vectors)
    config = ResolvedModel(
        "provider", "fixture", "https://fixture.invalid/v1", "fixture", 2
    )

    async def embed_batch():
        async with workspace.models("user-1").embedder(config) as caller:
            return await caller.embed(("first", "second", "third"))

    async def next_batch():
        return await pools.background.run(lambda: 42)

    task = asyncio.create_task(pools.background.run(embed_batch))
    waiting = None
    try:
        await asyncio.wait_for(entered.wait(), 3)
        # A decoded response can occupy the only worker without blocking the loop.
        assert await asyncio.wait_for(pools.graph_read.run(lambda: 7), 1) == 7
        waiting = asyncio.create_task(next_batch())
        await asyncio.sleep(0)
        assert not waiting.done()
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done() and not sdk.is_closed()
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 3)
        else:
            assert await asyncio.wait_for(task, 3) == ((1.0, 2.0),) * 3
            assert len(decoded) == len(validated) == 3
        assert await asyncio.wait_for(waiting, 3) == 42
        assert decoded == validated
        assert sdk.is_closed()
    finally:
        release.set()
        await asyncio.gather(
            task, *([waiting] if waiting else []), return_exceptions=True
        )
        await sdk.close()
        await pools.aclose()

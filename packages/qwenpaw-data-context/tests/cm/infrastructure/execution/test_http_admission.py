"""HTTP dependencies and writes use their intended execution resources."""

import asyncio
from threading import Event

import anyio.to_thread
import httpx
import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.auth import bind_authorization
from context_manager.schema import GRAPH_SCHEMA, METRIC
from context_manager.sql import RdbConnector
from context_manager.store import NodeRecord

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.retrieval.http_support import RecordingStore


@pytest.mark.asyncio
async def test_graph_read_can_resolve_identity_and_runtime_while_default_worker_is_busy():
    store = RecordingStore()
    store.get_result = NodeRecord(METRIC, {"key": "metric-1", "name": "Revenue"})
    runtime = bound_context_runtime(store, RdbConnector(), make_glossary(GRAPH_SCHEMA))
    app = create_api_app()
    bind_context_runtime(app, runtime)
    limiter = anyio.to_thread.current_default_thread_limiter()
    original_tokens = limiter.total_tokens
    limiter.total_tokens = 1
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()

    def occupy():
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)

    busy = asyncio.create_task(anyio.to_thread.run_sync(occupy))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with bind_authorization(authorization()):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await asyncio.wait_for(
                    client.post(
                        "/api/v1/graph/get_nodes",
                        json={"node": "Metric", "keys": ["metric-1"]},
                    ),
                    2,
                )
        assert response.status_code == 200
        assert response.json()[0]["key"] == "metric-1"
        assert not busy.done()
    finally:
        release.set()
        await busy
        limiter.total_tokens = original_tokens
        await runtime.aclose()

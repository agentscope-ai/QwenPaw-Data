"""Opt-in UG transactions against a disposable localhost Neo4j, with model doubles.

Set CM_UG_TEST_NEO4J_URI to an auth-disabled test container. Each test owns a
UUID workspace and removes only its synthetic nodes when it finishes.
"""

import asyncio
import os
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from context_manager.auth import GraphScopeAuthority, WriteOwner
from context_manager.schema import (
    GRAPH_SCHEMA,
    HAS_MEMORY,
    USER,
    USER_MEMORY,
)
from context_manager.store import StoreError
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore, Neo4jRuntime
from context_manager.user import MemoryIngestService
from neo4j import GraphDatabase

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.user_memory.support import FakeLlmCaller
from cm.user_memory.test_reconciliation import NEW, _extraction


@pytest.fixture
def graph():
    uri = os.environ.get("CM_UG_TEST_NEO4J_URI")
    if not uri:
        pytest.skip("CM_UG_TEST_NEO4J_URI must name a disposable localhost Neo4j")
    if urlparse(uri).hostname not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("UG live tests require a disposable localhost Neo4j")
    suffix = uuid4().hex
    workspace = f"ug-live-{suffix}"
    context = authorization(
        user_id=f"user-{suffix}", tenant_id=f"tenant-{suffix}", workspace_id=workspace
    )
    driver = GraphDatabase.driver(
        uri,
        auth=None,
        connection_timeout=3,
        connection_acquisition_timeout=3,
        max_transaction_retry_time=0,
    )
    access = GraphScopeAuthority()
    store = Neo4jGraphStore(
        Neo4jRuntime(driver, GraphTarget(context.tenant_id, workspace)),
        GRAPH_SCHEMA,
        access,
        make_glossary(GRAPH_SCHEMA),
    )
    read = access.issue_read_scope(context)
    write = access.issue_write_scope(context, owner=WriteOwner.USER, zone="user")
    try:
        yield store, read, write
    finally:
        with driver.session() as session:
            session.run(
                "MATCH (n {workspace_id: $workspace}) DETACH DELETE n",
                workspace=workspace,
            ).consume()
        store.close()


async def prepare(store, read, *, count=1):
    caller = FakeLlmCaller((_extraction(),) * count)
    service = MemoryIngestService(
        store,
        StaticModelService(caller, FakeCaller(dimensions=1024)),
        executor=executor,
    )
    plans = [
        await service.prepare_plan(
            ({"id": "target", "text": "Use yuan from now on.", "timestamp": NEW},),
            read,
            target_message_id="target",
        )
        for _ in range(count)
    ]
    return service, caller, plans


@pytest.mark.asyncio
async def test_real_neo4j_edge_failure_rolls_back_generated_nodes(graph, monkeypatch):
    store, read, write = graph
    service, _, (plan,) = await prepare(store, read)

    def fail(self, *args, **kwargs):
        raise StoreError("injected UG edge failure", operation="create_edges")

    monkeypatch.setattr(Neo4jGraphStore, "create_edges", fail)
    with pytest.raises(StoreError, match="injected UG edge failure"):
        await service.execute_plan(plan, write)
    assert not store.list_nodes(USER, scope=read)
    assert not store.list_nodes(USER_MEMORY, scope=read)
    assert not store.list_edges(HAS_MEMORY, scope=read)


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_user", (False, True))
async def test_real_neo4j_concurrent_ingest_retries_without_duplicate_nodes(
    graph, monkeypatch, existing_user
):
    store, read, write = graph
    if existing_user:
        store.create_nodes(
            USER,
            ({"user_id": read.user_id, "profile": "{}", "status": "active"},),
            scope=write,
        )
    service, _, plans = await prepare(store, read, count=2)
    barrier = Barrier(2)
    original = MemoryIngestService._write_plan

    def write_together(self, plan, scope):
        barrier.wait(timeout=10)
        return original(self, plan, scope)

    monkeypatch.setattr(MemoryIngestService, "_write_plan", write_together)
    results = await asyncio.gather(
        *(service.execute_plan(plan, write) for plan in plans),
        return_exceptions=True,
    )
    monkeypatch.setattr(MemoryIngestService, "_write_plan", original)
    errors = [result for result in results if isinstance(result, BaseException)]
    assert len(errors) == 1
    assert isinstance(errors[0], StoreError)
    assert errors[0].code == "transaction_failed"
    users = store.list_nodes(USER, scope=read)
    memories = store.list_nodes(USER_MEMORY, scope=read)
    assert (len(users), len(memories)) == (1, 1)
    assert len(store.list_edges(HAS_MEMORY, scope=read)) == 1

    await service.execute_plan(plans[results.index(errors[0])], write)
    assert [node.key for node in store.list_nodes(USER, scope=read)] == [users[0].key]
    assert [node.key for node in store.list_nodes(USER_MEMORY, scope=read)] == [
        memories[0].key
    ]
    assert len(store.list_edges(HAS_MEMORY, scope=read)) == 1

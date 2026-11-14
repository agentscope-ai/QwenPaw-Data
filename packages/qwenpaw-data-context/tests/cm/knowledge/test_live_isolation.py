"""Verify private KG imports and HTTP access, including real Neo4j isolation."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import pytest
from context_manager.auth import WriteOwner
from context_manager.knowledge import KnowledgeDeleteService
from context_manager.knowledge.document import DocumentStatus
from context_manager.schema import (
    ENTITY,
    EVENT,
    PARTICIPATION,
    USER_MEMORY,
    USER_MEMORY_REFERS_TO_ENTITY,
)

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    graph as graph,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    local_driver as local_driver,  # noqa: PLC0414 - intentional test fixture
)
from cm.knowledge.document_support import (
    create_document,
    make_document_resources,
    set_document_status,
)
from cm.knowledge.support import MemoryGraphStore
from cm.knowledge.test_document_storage import document_for


@pytest.fixture(params=["memory", "neo4j"])
def private_graph(request):
    if request.param == "neo4j":
        store, _, (scope, _) = request.getfixturevalue("graph")
        return store, scope.workspace_id
    return MemoryGraphStore(), "workspace-1"


def test_document_deletion_preserves_shared_sources_and_cleans_cross_graph_edges(
    private_graph, tmp_path
):
    store, workspace = private_graph
    scope = store.access.issue_write_scope(
        authorization("alice", workspace_id=workspace),
        owner=WriteOwner.USER,
    )
    documents = make_document_resources(
        tmp_path,
        tenant_id=scope.tenant_id,
        workspace_id=workspace,
        user_id="alice",
    )
    document = document_for()
    create_document(documents, document, b"report")
    set_document_status(documents, document.document_id, DocumentStatus.READY)
    doomed, shared = store.get_nodes(
        ENTITY,
        store.create_nodes(
            ENTITY,
            (
                {
                    "identity_hash": "doomed",
                    "type": "product",
                    "source_ids": (document.document_id,),
                },
                {
                    "identity_hash": "shared",
                    "type": "product",
                    "source_ids": (document.document_id, "other"),
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )
    event = store.get_nodes(
        EVENT,
        store.create_nodes(
            EVENT,
            (
                {
                    "identity_hash": "event",
                    "type": "launch",
                    "source_ids": ("other",),
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]
    memory = store.get_nodes(
        USER_MEMORY,
        store.create_nodes(USER_MEMORY, ({"id": f"memory-{workspace}"},), scope=scope),
        scope=scope,
    )[0]
    store.create_edges(
        PARTICIPATION,
        ((doomed.key, event.key, {}), (shared.key, event.key, {})),
        scope=scope,
    )
    store.create_edges(
        USER_MEMORY_REFERS_TO_ENTITY, ((memory.key, doomed.key, {}),), scope=scope
    )
    other_scope = store.access.issue_write_scope(
        authorization("bob", workspace_id=workspace),
        owner=WriteOwner.USER,
    )
    other = store.get_nodes(
        ENTITY,
        store.create_nodes(
            ENTITY,
            (
                {
                    "identity_hash": "doomed",
                    "type": "product",
                    "source_ids": (document.document_id,),
                },
            ),
            scope=other_scope,
        ),
        scope=other_scope,
    )[0]

    asyncio.run(
        KnowledgeDeleteService(store, **documents.arguments).delete_document(
            document.document_id, scope
        )
    )

    assert store.get_nodes(ENTITY, (doomed.key,), scope=scope) == (None,)
    assert store.get_nodes(ENTITY, (shared.key,), scope=scope)[0].properties[
        "source_ids"
    ] == ("other",)
    assert store.get_nodes(USER_MEMORY, (memory.key,), scope=scope) == (memory,)
    assert store.get_nodes(EVENT, (event.key,), scope=scope) == (event,)
    assert store.get_nodes(ENTITY, (other.key,), scope=other_scope) == (other,)
    assert store.list_edges(USER_MEMORY_REFERS_TO_ENTITY, scope=scope) == ()
    assert [
        (edge.source_key, edge.target_key)
        for edge in store.list_edges(PARTICIPATION, scope=scope)
    ] == [(shared.key, event.key)]
    assert documents.repository.list() == ()


@pytest.mark.parametrize("remaining", [(), ("third-document",)])
def test_concurrent_document_deletions_read_sources_after_previous_commit(
    private_graph, tmp_path, remaining
):
    store, workspace = private_graph
    scope = store.access.issue_write_scope(
        authorization("alice", workspace_id=workspace),
        owner=WriteOwner.USER,
    )
    documents = make_document_resources(
        tmp_path,
        tenant_id=scope.tenant_id,
        workspace_id=workspace,
        user_id="alice",
    )
    first, second = document_for(), document_for(b"second", "second.pdf")
    create_document(documents, first, b"report")
    create_document(documents, second, b"second")
    shared = store.get_nodes(
        ENTITY,
        store.create_nodes(
            ENTITY,
            (
                {
                    "identity_hash": "shared",
                    "type": "product",
                    "source_ids": (first.document_id, second.document_id, *remaining),
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]
    services = [KnowledgeDeleteService(store, **documents.arguments) for _ in range(2)]
    first_prepared, release_first, second_started, second_prepared = (
        Event() for _ in range(4)
    )
    prepare_first, prepare_second = (service._prepare_plan for service in services)

    def hold_first(*args):
        plan = prepare_first(*args)
        first_prepared.set()
        assert release_first.wait(5)
        return plan

    def observe_second(*args):
        plan = prepare_second(*args)
        second_prepared.set()
        return plan

    def delete_second():
        second_started.set()
        asyncio.run(services[1].delete_document(second.document_id, scope))

    with (
        patch.object(services[0], "_prepare_plan", side_effect=hold_first),
        patch.object(
            services[1],
            "_prepare_plan",
            side_effect=observe_second,
        ),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        first_delete = pool.submit(
            lambda: asyncio.run(services[0].delete_document(first.document_id, scope))
        )
        try:
            assert first_prepared.wait(5)
            second_delete = pool.submit(delete_second)
            assert second_started.wait(5)
            with pytest.raises(FileExistsError, match="doc_operation_in_progress"):
                second_delete.result(timeout=5)
            assert not second_prepared.is_set()
        finally:
            release_first.set()
        first_delete.result(timeout=5)
        pool.submit(delete_second).result(timeout=5)

    assert second_prepared.is_set()
    result = store.get_nodes(ENTITY, (shared.key,), scope=scope)[0]
    if remaining:
        assert result.properties["source_ids"] == remaining
    else:
        assert result is None
    assert documents.repository.list() == ()

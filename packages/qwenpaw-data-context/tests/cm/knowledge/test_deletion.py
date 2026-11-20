"""Verify source retraction, graph rollback and retryable document cleanup."""

from pathlib import Path
from unittest.mock import patch

import pytest
from context_manager.auth import WriteOwner
from context_manager.knowledge import KnowledgeDeleteService, UploadedDocument
from context_manager.knowledge.document import DocumentStatus
from context_manager.schema import (
    ASSOCIATION,
    ENTITY,
    EVENT,
    PARTICIPATION,
    USER_MEMORY,
    USER_MEMORY_REFERS_TO_ENTITY,
    USER_MEMORY_REFERS_TO_EVENT,
)
from sqlalchemy.exc import SQLAlchemyError

from cm.infrastructure.access.support import authorization
from cm.knowledge.document_support import (
    create_document,
    fail_document_sql,
    import_service,
    make_document_resources,
    set_document_status,
)
from cm.knowledge.support import MemoryGraphStore
from cm.knowledge.test_document_storage import document_for


@pytest.fixture
def deletion(tmp_path):
    store = MemoryGraphStore()
    scope = store.access.issue_write_scope(authorization(), owner=WriteOwner.USER)
    documents = make_document_resources(
        tmp_path,
        tenant_id=scope.tenant_id,
        workspace_id=scope.workspace_id,
        user_id=scope.user_id,
    )
    document = document_for()
    create_document(documents, document, b"report")
    set_document_status(documents, document.document_id, DocumentStatus.READY)
    service = KnowledgeDeleteService(store, **documents.arguments)
    return service, store, documents, document, scope


def node(store, scope, name, sources, spec=ENTITY):
    return store.get_nodes(
        spec,
        store.create_nodes(
            spec,
            (
                {
                    "name": name,
                    "type": "product" if spec is ENTITY else "other",
                    "identity_hash": name,
                    "source_ids": sources,
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]


@pytest.mark.asyncio
async def test_retracts_sources_and_removes_only_unclaimed_nodes_and_incident_edges(
    deletion,
):
    service, store, documents, document, scope = deletion
    source = document.document_id
    shared = node(store, scope, "shared", (source, "other-document"))
    retained = node(store, scope, "retained", ("other-document",))
    removed = node(store, scope, "removed", (source,))
    event = node(store, scope, "event", (source,), EVENT)
    manual = node(store, scope, "manual", ())
    memory = store.get_nodes(
        USER_MEMORY,
        store.create_nodes(USER_MEMORY, ({"id": "memory-1"},), scope=scope),
        scope=scope,
    )[0]
    for spec, left, right in (
        (ASSOCIATION, shared, retained),
        (ASSOCIATION, shared, removed),
        (PARTICIPATION, removed, event),
        (USER_MEMORY_REFERS_TO_ENTITY, memory, removed),
        (USER_MEMORY_REFERS_TO_EVENT, memory, event),
    ):
        store.create_edges(spec, ((left.key, right.key, {}),), scope=scope)
    other_scope = store.access.issue_write_scope(
        authorization("user-2"), owner=WriteOwner.USER
    )
    other = node(store, other_scope, "removed", (source,))
    delete = store.delete_nodes

    def delete_after_edges(spec, keys, *, scope):
        assert store.transaction_bound
        assert all(
            not (
                edge.source is spec
                and left in keys
                or edge.target is spec
                and right in keys
            )
            for edge, left, right in store.edges
        )
        return delete(spec, keys, scope=scope)

    with patch.object(store, "delete_nodes", side_effect=delete_after_edges):
        await service.delete_document(source, scope)

    assert store.get_nodes(ENTITY, (removed.key,), scope=scope) == (None,)
    assert store.get_nodes(EVENT, (event.key,), scope=scope) == (None,)
    updated = store.get_nodes(ENTITY, (shared.key,), scope=scope)[0]
    assert dict(updated.properties) == {
        **shared.properties,
        "source_ids": ("other-document",),
    }
    for value in (retained, manual, memory):
        assert store.get_nodes(value.spec, (value.key,), scope=scope) == (value,)
    assert store.get_nodes(ENTITY, (other.key,), scope=other_scope) == (other,)
    assert set(store.edges) == {(ASSOCIATION, shared.key, retained.key)}
    assert documents.repository.list() == ()
    assert not documents.files.path(document.metadata.filename).exists()


@pytest.mark.parametrize("storage", ["original", "catalog"])
@pytest.mark.asyncio
async def test_cleanup_failure_keeps_retry_record_after_graph_commit(deletion, storage):
    service, store, documents, document, scope = deletion
    node(store, scope, "removed", (document.document_id,))
    path = documents.files.path(document.metadata.filename)
    unlink = Path.unlink

    def fail_selected(target, *args, **kwargs):
        if target == path:
            raise OSError("cleanup failed")
        return unlink(target, *args, **kwargs)

    failure = (
        patch.object(Path, "unlink", fail_selected)
        if storage == "original"
        else fail_document_sql(documents, "DELETE")
    )
    with failure, pytest.raises((OSError, SQLAlchemyError)):
        await service.delete_document(document.document_id, scope)
    assert not store.nodes
    assert (
        documents.repository.get(document.document_id).metadata.status
        is DocumentStatus.DELETING
    )
    async with import_service(documents) as (importer, import_scope):
        with pytest.raises(FileExistsError, match="filename already exists"):
            await importer.start_import(
                UploadedDocument(
                    document.metadata.filename,
                    document.metadata.content_type,
                    b"report",
                ),
                authorization(
                    import_scope.user_id,
                    import_scope.tenant_id,
                    import_scope.workspace_id,
                ),
                import_scope,
            )
        with pytest.raises(FileExistsError, match="doc_delete_in_progress"):
            await importer.start_reingest(
                document.document_id,
                authorization(
                    import_scope.user_id,
                    import_scope.tenant_id,
                    import_scope.workspace_id,
                ),
                import_scope,
            )
    await service.delete_document(document.document_id, scope)
    assert documents.repository.list() == ()

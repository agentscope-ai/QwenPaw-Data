"""Exercise user-owned document operations through the authenticated HTTP API."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import WriteOwner
from context_manager.db import initialize_database
from context_manager.knowledge.document import (
    Document,
    DocumentMetadata,
    DocumentStatus,
)
from context_manager.schema import ENTITY, GRAPH_SCHEMA
from context_manager.settings.db import DbSettings
from context_manager.sql import RdbConnector
from context_manager.store import StoreError
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, init_schema
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.models.support import StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.document_support import (
    create_document,
    runtime_documents,
    set_document_status,
)
from cm.knowledge.http_support import (
    ApiKnowledgeCaller,
    _content,
    _runtime,
    wait_for_imports,
)
from cm.knowledge.support import MemoryGraphStore


@pytest.fixture
def document_api(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'authorization.db'}"
    init_schema(dsn)
    initialize_database(DbSettings(url=dsn))
    with get_db(dsn) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name, neo4j_database) "
            "VALUES ('tenant-1', 'Tenant', 'tenant_1'), "
            "('tenant-2', 'Other tenant', 'tenant_2')"
        )
        for user, username in (("user-1", "analyst"), ("user-2", "administrator")):
            connection.execute(
                "INSERT INTO users (id, tenant_id, username) VALUES (?, 'tenant-1', ?)",
                (user, username),
            )
        connection.commit()
    graph = MemoryGraphStore()
    managers = {}
    authorizations = []

    def models(user_id):
        if user_id not in managers:
            managers[user_id] = StaticModelService(
                ApiKnowledgeCaller(), user_id=user_id
            )
        return managers[user_id]

    runtime = bound_context_runtime(
        graph,
        RdbConnector(),
        make_glossary(GRAPH_SCHEMA),
        model_service_factory=models,
        qwenpaw_data_home=tmp_path,
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)

    def authorize(request):
        user = request.headers.get("X-Test-User")
        authorizations.append(user)
        if user is None:
            raise HTTPException(401, "not authenticated")
        if user == "outsider":
            return replace(authorization(user), workspace_ids=frozenset())
        return authorization(user, role="ws_admin" if user == "user-2" else "member")

    install_context_authentication(
        app,
        AuthConfig(secret_keys=["test-secret"], control_plane_dsn=dsn),
        runtime.fixed_target,
        authorizer=authorize,
    )
    with TestClient(app, headers={"X-Test-User": "user-1"}) as client:
        yield client, runtime, graph, managers, authorizations


def seed_document(
    runtime,
    name,
    status=DocumentStatus.UPLOADED,
    *,
    user="user-1",
    content=None,
    content_type="application/pdf",
    order=0,
):
    documents = runtime_documents(runtime, authorization(user))
    content = name.encode() if content is None else content
    timestamp = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=order)
    document = Document.from_content(
        content,
        DocumentMetadata(
            filename=name,
            content_type=content_type,
            byte_size=len(content),
            status=DocumentStatus.UPLOADED,
            created_at=timestamp,
            updated_at=timestamp,
        ),
    )
    create_document(documents, document, content)
    if status is not DocumentStatus.UPLOADED:
        document = set_document_status(
            documents,
            document.document_id,
            status,
            error="safe failure" if status is DocumentStatus.FAILED else None,
            active=status in (DocumentStatus.PROCESSING, DocumentStatus.DELETING),
        )
    return document


def test_other_users_including_admin_cannot_download_delete_or_reingest_an_original(
    document_api,
):
    client, runtime, _, _managers, _ = document_api
    original = seed_document(runtime, "report.pdf")
    headers = {"X-Test-User": "user-2"}
    assert client.get("/api/v1/docs", headers=headers).json()["data"]["total"] == 0
    assert (
        client.get(
            f"/api/v1/docs/{original.document_id}/download", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/docs/{original.document_id}", headers=headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/v1/docs/reingest",
            params={"doc_id": original.document_id},
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        runtime_documents(runtime, authorization()).repository.get(original.document_id)
        == original
    )


@pytest.mark.parametrize(
    "legacy_original", [False, True], ids=["new-document", "existing-content-only-id"]
)
def test_same_content_files_have_independent_reingest_and_deletion(
    document_api, legacy_original
):
    client, runtime, graph, _, _ = document_api
    content = "AcmeChat 可以帮助分析销售数据。".encode()
    identifiers = []
    documents = runtime_documents(runtime, authorization())
    for name in ("产品介绍.txt", "产品介绍副本.txt"):
        if legacy_original and not identifiers:
            now = datetime.now(UTC)
            document = Document(
                f"document:{sha256(content).hexdigest()}",
                DocumentMetadata(
                    name, "text/plain", len(content), DocumentStatus.READY, now, now
                ),
            )
            create_document(documents, document, content)
            response = client.post(
                "/api/v1/docs/reingest", params={"doc_id": document.document_id}
            )
            assert response.json()["data"]["doc_id"] == document.document_id
        else:
            response = client.post(
                "/api/v1/docs/upload", files={"file": (name, content)}
            )
        assert response.status_code == 200, response.text
        identifiers.append(response.json()["data"]["doc_id"])
        wait_for_imports(client, runtime)
    original_id, copy_id = identifiers
    assert original_id != copy_id
    original = documents.repository.get(original_id)
    scope = runtime.issue_graph_read_scope(authorization(), zone="knowledge")
    (node,) = graph.list_nodes(ENTITY, scope=scope)
    assert set(node.properties["source_ids"]) == set(identifiers)

    assert (
        client.post("/api/v1/docs/reingest", params={"doc_id": copy_id}).status_code
        == 200
    )
    wait_for_imports(client, runtime)
    assert documents.repository.get(original_id) == original
    assert client.delete(f"/api/v1/docs/{original_id}").status_code == 200
    (retained,) = graph.list_nodes(ENTITY, scope=scope)
    assert retained.key == node.key and retained.properties["source_ids"] == (copy_id,)
    assert client.get(f"/api/v1/docs/{original_id}/download").status_code == 404
    assert client.get(f"/api/v1/docs/{copy_id}/download").content == content
    assert documents.repository.get(copy_id).metadata.status is DocumentStatus.READY
    assert client.delete(f"/api/v1/docs/{copy_id}").status_code == 200
    assert not graph.list_nodes(ENTITY, scope=scope) and not documents.repository.list()


def test_failed_graph_import_is_listed_and_reingest_reuses_original_with_new_snapshot(
    document_api, monkeypatch
):
    client, runtime, graph, _, _ = document_api
    content = _content()
    create = graph.create_nodes

    def fail_after_write(*args, **kwargs):
        create(*args, **kwargs)
        raise StoreError("commit failed", operation="create_nodes")

    monkeypatch.setattr(graph, "create_nodes", fail_after_write)
    failed = client.post(
        "/api/v1/docs/upload", files={"file": ("产品介绍.docx", content)}
    )
    assert failed.status_code == 200
    assert failed.json()["data"]["ingest_status"] == "building"
    assert failed.json()["data"]["retry_action"] is None
    wait_for_imports(client, runtime)
    assert not graph.nodes and not graph.edges
    item = client.get("/api/v1/docs?status=failed").json()["data"]["list"][0]
    assert item["can_delete"] is True and item["ingest_error"]
    assert item["retry_action"] == "reingest"
    store = runtime_documents(runtime, authorization())
    original = store.repository.get(item["doc_id"])
    assert client.get(item["download_url"]).content == content
    monkeypatch.setattr(graph, "create_nodes", create)
    retried = client.post("/api/v1/docs/reingest", params={"doc_id": item["doc_id"]})
    assert retried.status_code == 200
    assert retried.json()["data"]["ingest_status"] == "building"
    wait_for_imports(client, runtime)
    ready = client.get("/api/v1/docs").json()["data"]["list"][0]
    assert ready["doc_id"] == original.document_id and ready["ingest_status"] == "ready"
    assert ready["ingest_error"] is None
    assert ready["retry_action"] is None
    assert (
        store.repository.get(original.document_id).metadata.created_at
        == original.metadata.created_at
    )
    assert len(store.repository.list()) == 1 and len(graph.nodes) == 1
    scope = runtime.issue_graph_read_scope(authorization(), zone="knowledge")
    assert graph.list_nodes(ENTITY, scope=scope)[0].properties["source_ids"] == (
        original.document_id,
    )
    keys = tuple(graph.nodes)
    assert (
        client.post(
            "/api/v1/docs/reingest", params={"doc_id": item["doc_id"]}
        ).status_code
        == 200
    )
    wait_for_imports(client, runtime)
    assert tuple(graph.nodes) == keys


@pytest.mark.asyncio
async def test_runtime_shutdown_finishes_accepted_import_before_closing_clients(
    tmp_path,
):
    import asyncio

    runtime, caller = _runtime(tmp_path)
    context = authorization()
    importer = runtime.knowledge_importer(context)
    scope = runtime.issue_graph_write_scope(
        context, owner=WriteOwner.USER, zone="knowledge"
    )
    documents = runtime_documents(runtime, context)
    started, release = asyncio.Event(), asyncio.Event()
    prepare = importer.prepare_plan

    async def gated_prepare(*args, **kwargs):
        started.set()
        await release.wait()
        return await prepare(*args, **kwargs)

    importer.prepare_plan = gated_prepare
    from context_manager.knowledge import UploadedDocument

    accepted = await importer.start_import(
        UploadedDocument("report.docx", None, _content()), context, scope
    )
    await asyncio.wait_for(started.wait(), 5)
    closing = asyncio.create_task(runtime.aclose())
    release.set()
    await asyncio.wait_for(closing, 5)
    assert caller.close_calls == 1
    assert (
        documents.repository.get(accepted.document_id).metadata.status
        is DocumentStatus.READY
    )

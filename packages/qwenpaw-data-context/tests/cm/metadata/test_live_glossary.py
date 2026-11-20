"""Opt-in Glossary journeys through real PostgreSQL, Neo4j and the KG HTTP API.

CM_GLOSSARY_TEST_DSN and CM_BATCH_NEO4J_URI must point to disposable local
databases. The model responses here are deterministic; the live KG journey
separately exercises the configured provider.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.dependencies import require_context_authorization
from context_manager.auth import WriteOwner
from context_manager.db import CMDatabase, get_database, initialize_database
from context_manager.runtime import (
    ContextRuntime,
    Neo4jConnectionConfig,
    WorkspaceResolver,
    WorkspaceRuntimePool,
    create_workspace_runtime,
)
from context_manager.runtime.execution import ExecutionResources
from context_manager.schema import (
    ENTITY,
    GRAPH_SCHEMA,
    Glossary,
    UnknownTermError,
)
from context_manager.settings.db import DbSettings
from context_manager.settings.execution import ExecutionSettings
from context_manager.store.neo4j import Neo4jRuntime
from context_manager.store.neo4j.physical_schema import ensure_schema
from qwenpaw_data_auth.config import AuthConfig
from fastapi.testclient import TestClient
from neo4j import GraphDatabase
from sqlalchemy import create_engine, text

from cm.infrastructure.access.support import authorization
from cm.infrastructure.models.support import StaticModelService
from cm.knowledge.import_support import FakeKnowledgeCaller, _docx, _response


@pytest.fixture
def physical_settings(tmp_path, monkeypatch):
    dsn = os.environ.get("CM_GLOSSARY_TEST_DSN")
    uri = os.environ.get("CM_BATCH_NEO4J_URI")
    if not dsn or not uri:
        pytest.skip(
            "set CM_GLOSSARY_TEST_DSN and CM_BATCH_NEO4J_URI for real persistence"
        )
    assert urlparse(dsn).hostname in {"localhost", "127.0.0.1"}
    assert urlparse(uri).hostname in {"localhost", "127.0.0.1"}
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    identity = uuid4().hex
    settings = Neo4jConnectionConfig(
        tenant_id=f"glossary-tenant-{identity}",
        workspace_id=f"glossary-workspace-{identity}",
        neo4j_uri=uri,
        neo4j_user="neo4j",
        neo4j_password=os.environ["CM_BATCH_NEO4J_PASSWORD"],
    )
    initialize_database(DbSettings(url=dsn))
    with GraphDatabase.driver(uri, auth=("neo4j", settings.neo4j_password)) as driver:
        ensure_schema(Neo4jRuntime(driver, settings.target))
    yield settings
    with GraphDatabase.driver(uri, auth=("neo4j", settings.neo4j_password)) as driver:
        driver.execute_query(
            "MATCH (n {workspace_id: $workspace}) DETACH DELETE n",
            workspace=settings.workspace_id,
            database_="neo4j",
        )
    engine = create_engine(dsn)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM cm_kg_glossary WHERE tenant_id IN (:tenant, :other)"),
            {"tenant": settings.tenant_id, "other": settings.tenant_id + "-other"},
        )
    engine.dispose()


def user_context(settings, user="alice"):
    return authorization(user, settings.tenant_id, settings.workspace_id, role="member")


def test_concurrent_postgres_initialization_and_updates_keep_every_term(
    physical_settings,
):
    settings = physical_settings
    engine = create_engine(get_database().engine.url)
    CMDatabase(engine).initialize()
    start = Barrier(8)

    def add_term(index):
        # Separate engines model independent application processes sharing one row.
        connection_pool = create_engine(get_database().engine.url)
        try:
            start.wait(timeout=15)
            glossary = Glossary(
                GRAPH_SCHEMA,
                connection_pool,
                tenant_id=settings.tenant_id,
                workspace_id=settings.workspace_id,
            )
            glossary.upsert_term(
                ENTITY, "type", f"category_{index}", description=f"分类 {index}"
            )
        finally:
            connection_pool.dispose()

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add_term, range(8)))
        glossary = Glossary(
            GRAPH_SCHEMA,
            engine,
            tenant_id=settings.tenant_id,
            workspace_id=settings.workspace_id,
        )
        terms = glossary.list_terms(ENTITY, "type")
        assert len(terms) == 20
        assert {f"category_{index}" for index in range(8)} <= {
            term.value for term in terms
        }
    finally:
        engine.dispose()


def test_http_import_adds_workspace_term_and_store_applies_its_core_mapping(
    physical_settings,
):
    settings = physical_settings
    response = _response(entity_type="regulation")
    response["entities"][0]["properties"].update(
        name="星舟数据保护条例",
        canonical_name="星舟数据保护条例",
    )
    response["entities"][0]["type_description"] = "规定数据收集和使用边界的法律法规。"
    response["events"] = []
    response["relationships"] = []

    class Caller(FakeKnowledgeCaller):
        async def embed(self, texts, **options):
            return tuple((1.0,) + (0.0,) * 1023 for _ in texts)

    caller = Caller(response)
    execution = ExecutionResources(ExecutionSettings())
    pool = WorkspaceRuntimePool(
        lambda target: create_workspace_runtime(
            target,
            executor=execution.background,
            graph_read_executor=execution.graph_read,
            model_service_factory=lambda user_id: StaticModelService(
                caller,
                tenant_id=target.tenant_id,
                user_id=user_id,
                workspace_id=target.workspace_id,
            ),
        )
    )
    runtime = ContextRuntime(
        WorkspaceResolver(pool, connection=settings), execution=execution
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)
    app.state.auth_config = AuthConfig()
    context = user_context(settings)
    app.dependency_overrides[require_context_authorization] = lambda: context
    scope = runtime.issue_graph_write_scope(
        context, owner=WriteOwner.USER, zone="knowledge"
    )
    glossary = runtime.glossary(scope)
    store = runtime.graph_store(scope)

    with TestClient(app) as client:
        document = _docx("星舟数据保护条例要求，采集会员数据前必须征得同意。")
        uploaded = client.post(
            "/api/v1/docs/upload",
            files={
                "file": ("数据保护条例.docx", document.content, document.content_type),
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        doc_id = uploaded.json()["data"]["doc_id"]
        from cm.knowledge.document_support import wait_for_imports

        client.portal.call(wait_for_imports, runtime.knowledge_importer(context))
        listing = client.get("/api/v1/docs").json()["data"]["list"]
        assert listing[0]["ingest_status"] == "ready", listing
        term = glossary.resolve_term(ENTITY, "type", "regulation")
        assert term.graph_labels == frozenset()
        nodes = store.list_nodes(ENTITY, scope=scope)
        assert len(nodes) == 1
        node = nodes[0]
        assert node.properties["type"] == "regulation"
        assert doc_id in node.properties["source_ids"]
        assert len(node.properties["embedding"]) == 1024
        assert node.properties["embedding_hash"]

        def labels():
            rows = store.execute_query(
                "MATCH (n:Entity {workspace_id: $workspace_id, owner_id: $owner_id, key: $key}) "
                "RETURN labels(n) AS labels",
                {
                    "workspace_id": scope.workspace_id,
                    "owner_id": "alice",
                    "key": node.key,
                },
                scope=scope,
            )
            return rows[0]["labels"]

        assert "Core" not in labels()
        glossary.set_graph_labels(ENTITY, "type", "regulation", frozenset({"Core"}))
        assert (
            store.patch_nodes(
                ENTITY, ((node.key, {"type": "regulation"}),), scope=scope
            )
            is None
        )
        assert "Core" in labels()
        glossary.set_graph_labels(ENTITY, "type", "regulation", frozenset())
        store.patch_nodes(ENTITY, ((node.key, {"type": "regulation"}),), scope=scope)
        assert "Core" not in labels()

        context = user_context(settings, "bob")
        bob_scope = runtime.issue_graph_read_scope(
            context, owner_id="bob", zone="knowledge"
        )
        assert (
            runtime.glossary(bob_scope).resolve_term(ENTITY, "type", "regulation")
            == term
        )
        assert store.list_nodes(ENTITY, scope=bob_scope) == ()
        assert client.get("/api/v1/docs").json()["data"]["list"] == []
        assert client.get(f"/api/v1/docs/{doc_id}/download").status_code == 404

        context = user_context(settings)
        assert glossary.remove_term(ENTITY, "type", "regulation")
        with pytest.raises(UnknownTermError):
            store.create_nodes(
                ENTITY,
                ({"identity_hash": "removed-term", "type": "regulation"},),
                scope=scope,
            )
        assert len(store.list_nodes(ENTITY, scope=scope)) == 1

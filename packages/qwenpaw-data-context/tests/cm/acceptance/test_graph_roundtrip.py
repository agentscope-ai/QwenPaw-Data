"""Opt-in real model/Neo4j journeys, isolated from existing Host and graph data.

Set CM_RUN_LIVE_GRAPH_TESTS=1 and CM_LIVE_NEO4J_URI to a disposable localhost
Neo4j. Model defaults come from CM_LIVE_ENV_FILE (default: repository .env).
Only synthetic documents/chats are sent to that configured provider. Temporary
Host credentials are encrypted; graph cleanup targets this fixture's UUID scope.
"""

from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import WriteOwner
from context_manager.db import initialize_database
from context_manager.model import ModelConfigRepository, ModelService
from context_manager.process import build_embedding_text
from context_manager.runtime import (
    ContextRuntime,
    Neo4jConnectionConfig,
    create_context_runtime,
)
from context_manager.schema import (
    ENTITY,
    EVENT,
    GRAPH_SCHEMA,
    Zone,
)
from context_manager.settings.db import DbSettings
from context_manager.user.loader import (
    ChatLoader,
    ChatLoaderCheckpointRepository,
)
from cryptography.fernet import Fernet
from qwenpaw_data_auth.authentication import resolve_current_authorization
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import init_schema
from qwenpaw_data_auth.token import TokenIssuer
from docx import Document
from dotenv import dotenv_values
from neo4j import GraphDatabase
from semantic_config.integration import mount_semantic_config

from cm.infrastructure.access.support import authorization
from cm.user_memory.support import history_adapter

_ALICE = "live-alice"
_BOB = "live-bob"
_DATASOURCE = "live-retail"
_DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass
class LiveGraph:
    runtime: ContextRuntime
    http: httpx.AsyncClient
    tenant: str
    workspace: str
    database_path: Path
    checkpoints: ChatLoaderCheckpointRepository
    headers: dict[str, dict[str, str]] = field(repr=False)
    application: object = field(default=None, repr=False)

    def context(self, user=_ALICE):
        _, role, _ = resolve_current_authorization(
            f"sqlite:///{self.database_path}", user, self.tenant, self.workspace
        )
        return authorization(user, self.tenant, self.workspace, role=role)

    def read_scope(self, user=_ALICE, **options):
        return self.runtime.issue_graph_read_scope(self.context(user), **options)

    def write_scope(self, user=_ALICE, *, owner=WriteOwner.WORKSPACE, **options):
        return self.runtime.issue_graph_write_scope(
            self.context(user), owner=owner, **options
        )

    @property
    def store(self):
        return self.runtime.graph_store(self.read_scope())

    async def request(self, method, path, *, user=_ALICE, **options):
        return await self.http.request(
            method, path, headers=self.headers[user], **options
        )

    def add_chats(self, user, texts, *, first_sequence=1):
        # Fixed timestamps keep evidence ordering reproducible.
        start = datetime(2026, 8, 31, 10, tzinfo=UTC)
        with sqlite3.connect(self.database_path) as conn:
            for sequence, text in enumerate(texts, start=first_sequence):
                timestamp = (start + timedelta(minutes=sequence)).isoformat()
                conn.execute(
                    "INSERT INTO chats VALUES (?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?, NULL)",
                    (
                        f"{user}-{sequence}",
                        f"session-{user}",
                        self.tenant,
                        self.workspace,
                        user,
                        sequence,
                        text,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )


@pytest_asyncio.fixture
async def live_graph(tmp_path, monkeypatch):
    if os.environ.get("CM_RUN_LIVE_GRAPH_TESTS") != "1":
        pytest.skip("set CM_RUN_LIVE_GRAPH_TESTS=1 for real model and Neo4j journeys")
    uri = os.environ.get("CM_LIVE_NEO4J_URI", "")
    if urlparse(uri).hostname not in {"localhost", "127.0.0.1"}:
        pytest.fail("CM_LIVE_NEO4J_URI must select a disposable localhost Neo4j")
    env_file = Path(
        os.environ.get("CM_LIVE_ENV_FILE", Path(__file__).resolve().parents[5] / ".env")
    )
    config = {**dotenv_values(env_file), **os.environ}
    required = (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "LLM_MODEL",
        "EMBED_MODEL",
        "EMBED_DIM",
    )
    missing = [name for name in required if not config.get(name)]
    if missing:
        pytest.fail("missing live model settings: " + ", ".join(missing))
    assert int(config["EMBED_DIM"]) == 1024, (
        "live model must match the current Neo4j vector dimension"
    )
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "user-files"))
    monkeypatch.setenv("EMBED_DIM", "1024")

    tenant, workspace = "cm-live-tenant", f"cm-live-{uuid4().hex}"
    database_path = tmp_path / "host.sqlite3"
    dsn = f"sqlite:///{database_path}"
    init_schema(dsn)
    master = secrets.token_bytes(32)
    monkeypatch.setenv("QWENPAW_DATA_PREFERENCES_MASTER_SECRET", master.hex())
    cipher = Fernet(base64.urlsafe_b64encode(master))
    encrypted_key = "ENC:" + cipher.encrypt(config["OPENAI_API_KEY"].encode()).decode()
    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, neo4j_database) VALUES (?, ?, ?)",
            (tenant, "Synthetic live integration", "neo4j"),
        )
        conn.execute(
            "INSERT INTO workspaces (id, tenant_id, name, status, instance_status) VALUES (?, ?, ?, 'active', 'ready')",
            (workspace, tenant, "Synthetic graph"),
        )
        conn.execute(
            "CREATE TABLE user_providers (user_id TEXT, provider_id TEXT, api_key_enc TEXT, base_url TEXT, PRIMARY KEY (user_id, provider_id))"
        )
        conn.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, tenant_id TEXT, workspace_id TEXT, user_id TEXT, deleted_at TEXT, created_at TEXT DEFAULT '2026-08-01T00:00:00Z')"
        )
        conn.execute(
            "CREATE TABLE chats (id TEXT PRIMARY KEY, session_id TEXT, tenant_id TEXT, workspace_id TEXT, user_id TEXT, sequence INTEGER, user_input TEXT, status TEXT, completed_at TEXT, updated_at TEXT, created_at TEXT)"
        )
        from cm.user_memory.loader.test_loader import _history_context_tables

        _history_context_tables(conn)
        for user in (_ALICE, _BOB):
            conn.execute(
                "INSERT INTO users (id, tenant_id, username) VALUES (?, ?, ?)",
                (user, tenant, user),
            )
            conn.execute(
                "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) VALUES (?, ?, 'member', 'workspace', ?)",
                (f"binding-{user}", user, workspace),
            )
            conn.execute(
                "INSERT INTO sessions (id, tenant_id, workspace_id, user_id, deleted_at) VALUES (?, ?, ?, ?, NULL)",
                (f"session-{user}", tenant, workspace, user),
            )
            conn.execute(
                "INSERT INTO user_providers VALUES (?, 'live-provider', ?, ?)",
                (user, encrypted_key, config["OPENAI_BASE_URL"]),
            )
    initialize_database(DbSettings(url=dsn))
    for user in (_ALICE, _BOB):
        model_config = ModelService(
            ModelConfigRepository(tenant, user, workspace_id=workspace)
        )
        model_config.save(
            "llm", {"provider_id": "live-provider", "model": config["LLM_MODEL"]}
        )
        model_config.save(
            "embedding",
            {
                "provider_id": "live-provider",
                "model": config["EMBED_MODEL"],
                "dim": 1024,
            },
        )

    settings = Neo4jConnectionConfig(
        tenant_id=tenant,
        workspace_id=workspace,
        neo4j_uri=uri,
        neo4j_user="neo4j",
        neo4j_password=config.get("CM_LIVE_NEO4J_PASSWORD") or "local-test-only",
        neo4j_database="neo4j",
    )
    runtime = create_context_runtime(settings)
    app = create_api_app()
    bind_context_runtime(app, runtime)
    mount_semantic_config(app)
    auth_secret = secrets.token_hex(32)
    install_context_authentication(
        app,
        AuthConfig(secret_keys=[auth_secret], control_plane_dsn=dsn),
        settings.target,
    )
    issuer = TokenIssuer(auth_secret)
    headers = {
        user: {
            "Authorization": "Bearer "
            + issuer.issue_access(user, tenant, workspace, "member")
        }
        for user in (_ALICE, _BOB)
    }
    checkpoints = ChatLoaderCheckpointRepository()
    loader = ChatLoader(checkpoints)

    app.state.context_runtime.user_ingest = history_adapter(runtime, loader=loader)
    started, shutdown = asyncio.Event(), asyncio.Event()

    async def serve_lifespan():
        # MCP's AnyIO cancel scope must enter and exit in the same task.
        async with app.router.lifespan_context(app):
            started.set()
            await shutdown.wait()

    lifecycle = asyncio.create_task(serve_lifespan())
    try:
        await asyncio.wait_for(started.wait(), timeout=30)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://cm-live",
            timeout=180,
        ) as http:
            live = LiveGraph(
                runtime,
                http,
                tenant,
                workspace,
                database_path,
                checkpoints,
                headers,
                app,
            )
            assert live.store.health()["ok"]
            assert (await http.get("/api/v1/ug/me")).status_code == 401
            assert (await live.request("GET", "/api/v1/ug/me")).status_code == 200
            yield live
    finally:
        # The only deletion is this fixture's newly generated Workspace, not the database.
        shutdown.set()
        try:
            await lifecycle
        finally:
            with GraphDatabase.driver(
                uri, auth=(settings.neo4j_user, settings.neo4j_password)
            ) as driver:
                driver.execute_query(
                    "MATCH (n {workspace_id: $workspace}) DETACH DELETE n",
                    workspace=workspace,
                    database_="neo4j",
                )


def assert_embeddings(nodes):
    checked = 0
    for node in nodes:
        text = build_embedding_text(node.spec, node.properties)
        if not text:
            continue
        vector = node.properties["embedding"]
        assert len(vector) == 1024
        assert all(math.isfinite(value) for value in vector)
        assert any(value != 0 for value in vector)
        assert node.properties["embedding_hash"] == sha256(text.encode()).hexdigest()
        checked += 1
    assert checked > 0


def graph_nodes(live, zone, user=_ALICE):
    scope = live.read_scope(user, zone=zone.value)
    return tuple(
        node
        for spec in GRAPH_SCHEMA.nodes
        if spec.zone is zone
        for node in live.store.list_nodes(spec, limit=1000, scope=scope)
    )


def graph_edges(live, zone, user=_ALICE):
    scope = live.read_scope(user, zone=zone.value)
    return tuple(
        edge
        for spec in GRAPH_SCHEMA.edges
        if spec.source.zone is zone
        for edge in live.store.list_edges(spec, limit=1000, scope=scope)
    )


def knowledge_document():
    document = Document()
    document.add_paragraph(
        "星舟零售是一家经营会员商城的公司。星舟会员商城是星舟零售开发的电商产品。"
    )
    document.add_paragraph(
        "2026 年 11 月 11 日，星舟零售举办双十一大促，参与产品为星舟会员商城。活动目标是提升会员复购。"
    )
    content = io.BytesIO()
    document.save(content)
    return content.getvalue()


@pytest.mark.asyncio
async def test_knowledge_real_extraction_uses_glossary_and_rejects_duplicate_upload(
    live_graph,
):
    live = live_graph
    content = knowledge_document()
    response = await live.request(
        "POST",
        "/api/v1/docs/upload",
        files={"file": ("retail.docx", content, _DOCX_TYPE)},
    )
    assert response.status_code == 200, response.text
    first = response.json()["data"]
    assert first["ingest_status"] == "building", first
    from cm.knowledge.document_support import wait_for_imports

    await wait_for_imports(live.runtime.knowledge_importer(live.context()))
    listed = await live.request("GET", "/api/v1/docs")
    assert listed.json()["data"]["list"][0]["ingest_status"] == "ready"
    nodes = graph_nodes(live, Zone.KNOWLEDGE)
    edges = graph_edges(live, Zone.KNOWLEDGE)
    assert sum(node.spec is ENTITY for node in nodes) >= 2
    assert any(node.spec is EVENT for node in nodes)
    assert edges
    assert_embeddings(nodes)
    keys = {node.key for node in nodes}
    assert all(edge.source_key in keys and edge.target_key in keys for edge in edges)
    assert all(first["doc_id"] in node.properties["source_ids"] for node in nodes)
    scope = live.read_scope(owner_id=_ALICE, zone="knowledge")
    glossary = live.runtime.glossary(scope)
    labels = live.store.execute_query(
        "MATCH (n:Entity {workspace_id: $workspace_id, owner_id: $owner_id}) "
        "RETURN n.key AS key, labels(n) AS labels",
        {"workspace_id": live.workspace, "owner_id": _ALICE},
        scope=scope,
    )
    labels_by_key = {row["key"]: row["labels"] for row in labels}
    for node in nodes:
        if node.spec is ENTITY:
            term = glossary.resolve_term(ENTITY, "type", node.properties["type"])
            assert ("Core" in labels_by_key[node.key]) == ("Core" in term.graph_labels)
    bob_scope = live.read_scope(_BOB, owner_id=_BOB, zone="knowledge")
    assert live.runtime.glossary(bob_scope).list_terms(
        ENTITY, "type"
    ) == glossary.list_terms(ENTITY, "type")
    assert live.store.list_nodes(ENTITY, scope=bob_scope) == ()
    assert (await live.request("GET", "/api/v1/docs", user=_BOB)).json()["data"][
        "list"
    ] == []
    downloaded = await live.request("GET", first["download_url"])
    assert downloaded.status_code == 200
    assert downloaded.content == content

    repeated = await live.request(
        "POST",
        "/api/v1/docs/upload",
        files={"file": ("retail.docx", content, _DOCX_TYPE)},
    )
    assert repeated.status_code == 409, repeated.text
    assert repeated.json() == {"detail": "doc_already_exists"}
    assert {node.key for node in graph_nodes(live, Zone.KNOWLEDGE)} == keys
    assert {
        (edge.spec, edge.source_key, edge.target_key)
        for edge in graph_edges(live, Zone.KNOWLEDGE)
    } == {(edge.spec, edge.source_key, edge.target_key) for edge in edges}
    assert_embeddings(graph_nodes(live, Zone.KNOWLEDGE))
    print(
        f"KG readback: nodes={len(nodes)}, edges={len(edges)}, glossary/Core/duplicate rejection=ok"
    )

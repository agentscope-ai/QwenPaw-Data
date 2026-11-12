"""Exercise authenticated MG userspace routes and the Runtime business service."""

import json
from unittest.mock import Mock

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import WriteOwner
from context_manager.schema import DOMAIN
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import GraphTarget
from qwenpaw_data_auth.config import AuthConfig
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.support import MemoryGraphStore


@pytest.fixture
def api(monkeypatch):
    store = MemoryGraphStore()
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        make_glossary(store.schema),
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)
    app.state.audit = Mock()
    monkeypatch.setattr(
        "context_manager.api.native.metadata.userspace.log_event", app.state.audit
    )

    def authorize(request):
        user = request.headers.get("x-test-user")
        if user is None:
            raise HTTPException(401, "authenticated user is required")
        return authorization(user, role=request.headers.get("x-test-role", "member"))

    install_context_authentication(
        app,
        AuthConfig(secret_keys=["test"], control_plane_dsn="sqlite:///:memory:"),
        GraphTarget("tenant-1", "workspace-1"),
        authorizer=authorize,
    )
    shared = store.access.issue_write_scope(
        authorization("admin", role="ws_admin"),
        owner=WriteOwner.WORKSPACE,
        zone="metadata",
    )
    (domain,) = store.get_nodes(
        DOMAIN,
        store.create_nodes(
            DOMAIN, ({"domain_id": "sales", "name": "Sales"},), scope=shared
        ),
        scope=shared,
    )
    with TestClient(app) as client:
        yield client, store, shared, domain


def test_successful_pull_persists_actor_and_workspace_in_auth_audit(
    api, tmp_path, monkeypatch
):
    from qwenpaw_data_auth.audit import log_event
    from qwenpaw_data_auth.database import get_db, init_schema, query_all

    client, _, _, _ = api
    dsn = f"sqlite:///{tmp_path / 'audit.sqlite3'}"
    init_schema(dsn)
    client.app.state.auth_config = AuthConfig(
        secret_keys=["test"], control_plane_dsn=dsn
    )
    monkeypatch.setattr(
        "context_manager.api.native.metadata.userspace.log_event", log_event
    )

    result = client.post("/api/v1/mg/userspace/sync", headers={"x-test-user": "alice"})

    assert result.status_code == 200, result.text
    with get_db(dsn) as connection:
        records = query_all(
            connection, "SELECT event_type, actor_id, target_id, detail FROM audit_logs"
        )
    assert len(records) == 1
    event = records[0]
    assert (event["event_type"], event["actor_id"], event["target_id"]) == (
        "mg_userspace_synced",
        "alice",
        "workspace-1",
    )
    detail = json.loads(event["detail"])
    assert detail["tenant_id"] == "tenant-1" and detail["workspace_id"] == "workspace-1"
    assert detail["overwritten"] == 1 and detail["conflict_count"] == 0


def test_graph_reads_separate_personal_authoritative_and_admin_review_scopes(api):
    client, _, _, source = api
    alice = {"x-test-user": "alice"}
    bob = {"x-test-user": "bob"}
    assert client.post("/api/v1/mg/userspace/sync", headers=alice).status_code == 200
    for headers, expected in ((alice, ["dom:alice:sales"]), (bob, [])):
        result = client.post(
            "/api/v1/graph/list_nodes", headers=headers, json={"node": "Domain"}
        )
        assert result.status_code == 200, result.text
        assert [node["key"] for node in result.json()] == expected
    result = client.post(
        "/api/v1/graph/list_nodes",
        headers=bob,
        json={"node": "Domain", "owner_id": "__global__"},
    )
    assert result.status_code == 200, result.text
    assert [node["key"] for node in result.json()] == [source.key]
    foreign = client.post(
        "/api/v1/graph/list_nodes",
        headers=bob,
        json={"node": "Domain", "owner_id": "alice"},
    )
    assert foreign.status_code == 403
    admin = {"x-test-user": "admin", "x-test-role": "ws_admin"}
    review = client.post(
        "/api/v1/graph/get_nodes",
        headers=admin,
        json={
            "node": "Domain",
            "owner_id": "alice",
            "keys": ["dom:alice:sales"],
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()[0]["key"] == "dom:alice:sales"
    for headers, key, expected, matched in (
        (alice, source.key, 403, None),
        (admin, "dom:alice:sales", 404, None),
        (admin, source.key, 200, True),
    ):
        patch = client.post(
            "/api/v1/graph/patch_nodes",
            headers=headers,
            json={
                "node": "Domain",
                "patches": [
                    {"key": key, "changes": {"description": "Authority correction"}}
                ],
            },
        )
        assert patch.status_code == expected, patch.text
        if expected == 200:
            assert patch.json() is True

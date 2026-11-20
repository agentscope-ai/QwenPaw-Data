"""Exercise workspace-local graph authorization with real Auth role resolution."""

from unittest.mock import patch

import pytest
from context_manager.api.app import bind_context_runtime, create_api_app
from context_manager.api.authentication import install_context_authentication
from context_manager.auth import AccessMode
from context_manager.sql import RdbConnector
from context_manager.store.neo4j import GraphTarget
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, init_schema
from qwenpaw_data_auth.token import TokenIssuer
from fastapi.testclient import TestClient

from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.storage.schema_support import make_glossary
from cm.retrieval.http_support import RecordingStore


@pytest.fixture
def workspace_member(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'authorization.db'}"
    init_schema(dsn)
    with get_db(dsn) as connection:
        connection.execute(
            "INSERT INTO tenants (id, name, neo4j_database) "
            "VALUES ('tenant-1', 'Tenant', 'tenant_1')"
        )
        for workspace in ("workspace-A", "workspace-1"):
            connection.execute(
                "INSERT INTO workspaces (id, tenant_id, name) VALUES (?, 'tenant-1', ?)",
                (workspace, workspace),
            )
        connection.execute(
            "INSERT INTO users (id, tenant_id, username) VALUES ('user-1', 'tenant-1', 'analyst')"
        )
        for workspace, role in (("workspace-A", "ws_admin"), ("workspace-1", "member")):
            connection.execute(
                "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
                "VALUES (?, 'user-1', ?, 'workspace', ?)",
                (workspace, role, workspace),
            )
        connection.commit()
    store = RecordingStore()
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        make_glossary(store.schema),
    )
    app = create_api_app()
    bind_context_runtime(app, runtime)
    install_context_authentication(
        app,
        AuthConfig(secret_keys=["test-auth-secret"], control_plane_dsn=dsn),
        GraphTarget("tenant-1", "workspace-1"),
    )
    token = TokenIssuer("test-auth-secret").issue_access(
        "user-1", "tenant-1", "workspace-1", "member"
    )
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as client:
        yield client, store, runtime, dsn


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("GET", "/api/v1/ug/users", None),
        ("GET", "/api/v1/ug/users/user-1", None),
        ("GET", "/api/v1/ug/users/other/memories", None),
        ("POST", "/api/v1/graph/list_nodes", {"node": "Metric", "owner_id": "other"}),
        (
            "POST",
            "/api/v1/graph/patch_nodes",
            {
                "node": "Metric",
                "patches": [
                    {"key": "metric-1", "changes": {"description": "Unauthorized edit"}}
                ],
            },
        ),
        (
            "POST",
            "/api/v1/graph/patch_edges",
            {
                "relationship": "HAS",
                "source": "Domain",
                "target": "Metric",
                "patches": [
                    {
                        "source_key": "domain-1",
                        "target_key": "metric-1",
                        "changes": {"description": "Unauthorized edit"},
                    }
                ],
            },
        ),
    ],
)
def test_other_workspace_admin_is_rejected_before_graph_io(
    workspace_member, method, path, body
):
    client, store, _, _ = workspace_member
    response = client.request(method, path, json=body)
    assert response.status_code == 403
    assert store.calls == []


@pytest.mark.parametrize("role", ["ws_admin", "tenant_admin"])
def test_active_workspace_and_tenant_admins_keep_review_and_authoritative_write_access(
    workspace_member,
    role,
):
    client, store, runtime, dsn = workspace_member
    with get_db(dsn) as connection:
        scope_type, scope_id = (
            ("tenant", "tenant-1")
            if role == "tenant_admin"
            else ("workspace", "workspace-1")
        )
        connection.execute(
            "INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id) "
            "VALUES ('admin-grant', 'user-1', ?, ?, ?)",
            (role, scope_type, scope_id),
        )
        connection.commit()
    with patch.object(
        runtime, "issue_graph_read_scope", wraps=runtime.issue_graph_read_scope
    ) as issue:
        reviewed = client.get("/api/v1/ug/users")
    assert reviewed.status_code == 200
    assert issue.call_args.args[0].active_workspace_role == role
    from context_manager.schema import METRIC
    from context_manager.store import NodeRecord

    store.get_result = NodeRecord(METRIC, {"key": "metric-1"})
    updated = client.post(
        "/api/v1/graph/patch_nodes",
        json={
            "node": "Metric",
            "patches": [
                {"key": "metric-1", "changes": {"description": "Approved edit"}}
            ],
        },
    )
    assert updated.status_code == 200
    scope = store.calls[-1][2]["scope"]
    assert scope.write_owner_id == "__global__"
    assert scope.mode is AccessMode.WRITE

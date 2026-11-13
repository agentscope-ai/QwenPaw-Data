from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data_auth.database import get_db, query_one
from qwenpaw_data_auth.provision_backend import configured_provision_backends
from qwenpaw_data_auth.server import create_app

from test_security_flows import PASSWORD, bootstrap_and_onboard, tenant_login


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    dsn = f"sqlite:///{tmp_path / 'auth.db'}"
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "test-secret")
    monkeypatch.setenv("QWENPAW_DATA_DB_URL", dsn)
    monkeypatch.setenv("QWENPAW_DATA_ENV", "development")
    monkeypatch.setenv("QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN", "bootstrap-test-token")
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:3001"
    )
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    monkeypatch.delenv("QWENPAW_DATA_PROVISION_BACKENDS", raising=False)
    monkeypatch.delenv("NEO4J_URI", raising=False)
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    monkeypatch.delenv("HOLO_AGE_HOST", raising=False)
    monkeypatch.delenv("HOLO_AGE_PASSWORD", raising=False)
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, dsn


def test_tenant_admin_can_queue_workspace_provisioning(client):
    """创建 workspace 后，tenant_admin 可主动启动资源实例而非被动等待。"""
    http, dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")

    created = http.post("/api/workspaces", json={"name": "Analytics"})
    assert created.status_code == 201
    ws = created.json()["workspace"]
    assert ws["instance_status"] == "pending"

    queued = http.post(f"/api/workspaces/{ws['id']}/provision")
    assert queued.status_code == 202
    assert queued.json()["instance_status"] == "queued"

    with get_db(dsn) as conn:
        row = query_one(conn, "SELECT * FROM workspaces WHERE id = ?", (ws["id"],))
        tenant = query_one(conn, "SELECT * FROM tenants WHERE id = 'tenant-a'")
    assert row["instance_status"] == "queued"
    assert row["status"] == "provisioning"
    # workspace 级 provision 不得回退已激活租户的 onboarding 状态
    assert tenant["onboarding_state"] == "ready"
    assert tenant["status"] == "active"

    # 列表返回 instance_status 供前端展示按钮/状态
    listed = http.get("/api/workspaces").json()["workspaces"]
    assert any(w["id"] == ws["id"] and w["instance_status"] == "queued" for w in listed)

    # 非 pending/failed 状态不可重复入队
    assert http.post(f"/api/workspaces/{ws['id']}/provision").status_code == 409


def test_workspace_provision_requires_tenant_admin_and_tenant_scope(client):
    http, dsn = client
    platform_token, _tenant_id, _ws_id = bootstrap_and_onboard(http)

    # platform_admin 令牌不能走租户侧 provision 端点
    denied = http.post(
        "/api/workspaces/ws-a/provision",
        headers={"Authorization": f"Bearer {platform_token}"},
    )
    assert denied.status_code == 403

    tenant_login(http, "root")
    # 不存在 / 非本租户的 workspace → 404
    assert http.post("/api/workspaces/no-such-ws/provision").status_code == 404

    # failed 状态可重试入队
    created = http.post("/api/workspaces", json={"name": "Retry Me"})
    ws_id = created.json()["workspace"]["id"]
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE workspaces SET instance_status = 'failed' WHERE id = ?", (ws_id,)
        )
        conn.commit()
    retried = http.post(f"/api/workspaces/{ws_id}/provision")
    assert retried.status_code == 202
    assert retried.json()["instance_status"] == "queued"


def test_explicit_provision_backends_override_ignores_neo4j_creds(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_PROVISION_BACKENDS", "holo_age")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    assert configured_provision_backends() == ["holo_age"]


def test_workspace_list_exposes_configured_provision_backends(client):
    http, _dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")
    listed = http.get("/api/workspaces").json()
    assert listed["provision_backends"] == ["neo4j"]


def test_workspace_provision_rejects_unconfigured_holo_backend(client):
    http, _dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")
    created = http.post("/api/workspaces", json={"name": "No Holo"})
    ws_id = created.json()["workspace"]["id"]
    denied = http.post(
        f"/api/workspaces/{ws_id}/provision",
        json={"backend": "holo_age"},
    )
    assert denied.status_code == 400
    assert "not configured" in denied.json()["detail"]


def test_workspace_provision_can_queue_holo_backend(client, monkeypatch):
    monkeypatch.setenv("HOLO_AGE_HOST", "holo.example")
    monkeypatch.setenv("HOLO_AGE_PASSWORD", "secret")
    http, dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")

    listed = http.get("/api/workspaces").json()
    assert listed["provision_backends"] == ["neo4j", "holo_age"]

    created = http.post("/api/workspaces", json={"name": "Holo WS"})
    ws_id = created.json()["workspace"]["id"]
    queued = http.post(
        f"/api/workspaces/{ws_id}/provision",
        json={"backend": "holo_age"},
    )
    assert queued.status_code == 202
    assert queued.json()["backend"] == "holo_age"
    with get_db(dsn) as conn:
        row = query_one(conn, "SELECT * FROM workspaces WHERE id = ?", (ws_id,))
    assert row["instance_status"] == "queued"
    assert row["instance_version"] == "holo_age"

    other = http.post("/api/workspaces", json={"name": "Bad Backend"})
    other_id = other.json()["workspace"]["id"]
    bad = http.post(
        f"/api/workspaces/{other_id}/provision",
        json={"backend": "mysql"},
    )
    assert bad.status_code == 400


@pytest.mark.parametrize("backend", ["holo_age", "holoage", " HOLO-AGE "])
def test_workspace_provision_defaults_to_graph_backend(client, monkeypatch, backend):
    monkeypatch.setenv("GRAPH_BACKEND", backend)
    monkeypatch.delenv("HOLO_AGE_HOST", raising=False)
    monkeypatch.delenv("HOLO_AGE_PASSWORD", raising=False)
    http, dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")

    listed = http.get("/api/workspaces").json()
    assert listed["provision_backends"] == ["holo_age"]

    created = http.post("/api/workspaces", json={"name": "Default Holo"})
    ws_id = created.json()["workspace"]["id"]
    queued = http.post(f"/api/workspaces/{ws_id}/provision")
    assert queued.status_code == 202
    assert queued.json()["backend"] == "holo_age"
    with get_db(dsn) as conn:
        row = query_one(conn, "SELECT * FROM workspaces WHERE id = ?", (ws_id,))
    assert row["instance_version"] == "holo_age"


def test_tenant_name_must_be_unique(client):
    http, _dsn = client
    platform_token, _tenant_id, _ws_id = bootstrap_and_onboard(http)
    headers = {"Authorization": f"Bearer {platform_token}"}
    duplicate = http.post(
        "/api/platform/tenants",
        headers=headers,
        json={
            "name": "tenant a",
            "admin_username": "root-b",
            "admin_password": PASSWORD,
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Tenant name already exists"

    created = http.post(
        "/api/platform/tenants",
        headers=headers,
        json={
            "id": "tenant-b",
            "name": "Tenant B",
            "admin_username": "root-b",
            "admin_password": PASSWORD,
        },
    )
    assert created.status_code == 201
    renamed = http.put(
        "/api/platform/tenants/tenant-b",
        headers=headers,
        json={"name": "TENANT A"},
    )
    assert renamed.status_code == 409
    assert renamed.json()["detail"] == "Tenant name already exists"


def test_workspace_name_must_be_unique_in_tenant(client):
    http, _dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")
    first = http.post("/api/workspaces", json={"name": "Analytics"})
    assert first.status_code == 201
    duplicate = http.post("/api/workspaces", json={"name": "analytics"})
    assert duplicate.status_code == 409
    assert "already exists" in duplicate.json()["detail"]

    other = http.post("/api/workspaces", json={"name": "Reports"})
    assert other.status_code == 201
    renamed = http.put(
        f"/api/workspaces/{other.json()['workspace']['id']}",
        json={"name": "Analytics"},
    )
    assert renamed.status_code == 409

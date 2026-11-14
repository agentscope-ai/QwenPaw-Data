from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import insert

from qwenpaw_data_auth.auth_api import login_fails_by_ip, login_fails_by_user
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, new_id, reset_engines, role_bindings, tenants, users, workspaces
from qwenpaw_data_auth.password import hash_password
from qwenpaw_data_auth.server import create_app

PASSWORD = "Strong!Pass9"


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    for name in ("QWENPAW_DATA_AUTH_SECRET", "QWENPAW_DATA_SERVICE_SECRET",
                 "QWENPAW_DATA_API_TOKEN", "QWENPAW_DATA_DB_URL", "QWENPAW_DATA_AUTH_URL",
                 "QWENPAW_DATA_PROVISION_BACKENDS", "NEO4J_URI", "NEO4J_PASSWORD", "HOLO_AGE_HOST", "HOLO_AGE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    # Ported source tests assume multi-user login/RBAC; local-mode tests opt out via ``local_mode``.
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    login_fails_by_ip.clear()
    login_fails_by_user.clear()
    reset_engines()
    yield
    reset_engines()


@pytest.fixture
def local_mode(isolated_runtime, monkeypatch):
    """Run under the shipped default: QWENPAW_DATA_AUTH_MODE unset, i.e. local mode."""
    monkeypatch.delenv("QWENPAW_DATA_AUTH_MODE", raising=False)


@pytest.fixture
def multi_config(tmp_path):
    return AuthConfig(home=tmp_path / "multi", mode="multi-user", secret_keys=["test-signing-key"],
                      service_secret="test-machine-key", cm_base_url="", platform_bootstrap_token="test-bootstrap-key",
                      cors_allowed_origins=["http://localhost:3000"], authorization_redirect_origins=["http://localhost:3000"])


@pytest.fixture
def multi_client(multi_config):
    with TestClient(create_app(multi_config), base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client


def seed_user(cfg, *, user_id="u1", tenant_id="t1", username="alice", role="member", workspace_id="w1"):
    with get_db(cfg.control_plane_dsn) as conn:
        if not conn.execute("SELECT id FROM tenants WHERE id = ?", (tenant_id,)).fetchone():
            conn.execute(insert(tenants).values(id=tenant_id, name=tenant_id, neo4j_database="neo4j"))
        if not conn.execute("SELECT id FROM workspaces WHERE id = ?", (workspace_id,)).fetchone():
            conn.execute(insert(workspaces).values(id=workspace_id, tenant_id=tenant_id, name=workspace_id, instance_status="ready"))
        conn.execute(insert(users).values(id=user_id, tenant_id=tenant_id, username=username,
                     password_hash=hash_password(PASSWORD), active_workspace_id=workspace_id))
        conn.execute(insert(role_bindings).values(id=new_id(), user_id=user_id, role=role,
                     scope_type="tenant" if role == "tenant_admin" else "workspace",
                     scope_id=tenant_id if role == "tenant_admin" else workspace_id))
        conn.commit()
    return user_id


def login(client, username="alice", password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.cookies["qwenpaw_data_access"]


def bootstrap_and_onboard(client):
    response = client.post("/api/platform/bootstrap", headers={"X-QwenPaw-Data-Bootstrap-Token": "test-bootstrap-key"},
                           json={"username": "platform", "password": PASSWORD})
    assert response.status_code == 201, response.text
    token = response.cookies["qwenpaw_data_platform_access"]
    headers = {"Authorization": f"Bearer {token}"}
    response = client.post("/api/platform/tenants", headers=headers,
        json={"id": "tenant-a", "name": "Tenant A", "workspace_id": "ws-a", "admin_username": "root", "admin_password": PASSWORD})
    assert response.status_code == 201, response.text
    assert client.post("/api/platform/tenants/tenant-a/resume", headers=headers,
        json={"endpoint_ref": "bolt://127.0.0.1:7687", "secret_ref": "env://TEST_GRAPH_PASSWORD", "instance_version": "1"}).status_code == 200
    return token, response.json()

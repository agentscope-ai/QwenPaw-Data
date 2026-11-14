from __future__ import annotations

import pytest
from qwenpaw_data_auth import ControlPlaneReader
from qwenpaw_data_auth.breakglass import consume_raw_cypher_grant, raw_query_digest
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import get_db, query_one
from qwenpaw_data_auth.provision_backend import configured_provision_backends
from conftest import PASSWORD, bootstrap_and_onboard, login, seed_user


@pytest.fixture
def multi_config(tmp_path):
    # This suite uses HTTP loopback and explicitly provisions a loopback graph.
    # Production defaults retain secure cookies and an empty endpoint allowlist.
    return AuthConfig(
        home=tmp_path / "multi",
        mode="multi-user",
        secret_keys=["test-signing-key"],
        service_secret="test-machine-key",
        cm_base_url="",
        platform_bootstrap_token="test-bootstrap-key",
        cors_allowed_origins=["http://localhost:3000"],
        authorization_redirect_origins=["http://localhost:3000"],
        cookie_secure=False,
        workspace_endpoint_allowed_hosts=["127.0.0.1"],
    )


def test_platform_bootstrap_is_once_and_onboarding_atomic(multi_client):
    client = multi_client
    cfg = client.app.state.auth_config
    assert client.post("/api/platform/bootstrap", json={"username": "evil", "password": PASSWORD}).status_code == 403
    platform, onboarding = bootstrap_and_onboard(client)
    headers = {"Authorization": f"Bearer {platform}"}
    assert client.post("/api/platform/bootstrap", headers={"X-QwenPaw-Data-Bootstrap-Token": cfg.platform_bootstrap_token},
                       json={"username": "again", "password": PASSWORD}).status_code == 403
    assert client.get("/api/workspaces", headers=headers).status_code == 403
    assert client.get("/api/platform/me", headers=headers).json()["role"] == "platform_admin"
    assert client.post("/api/platform/tenants", headers=headers,
        json={"id": "duplicate", "name": "Tenant B", "admin_username": "root", "admin_password": PASSWORD}).status_code == 409
    assert client.post("/api/platform/tenants", headers=headers,
        json={"id": "duplicate-ws", "name": "Tenant B", "workspace_id": "ws-a", "admin_username": "root-b", "admin_password": PASSWORD}).status_code == 409
    with get_db(cfg.control_plane_dsn) as conn:
        assert query_one(conn, "SELECT count(*) AS n FROM tenants")["n"] == 1
        assert query_one(conn, "SELECT count(*) AS n FROM users")["n"] == 1
        assert query_one(conn, "SELECT * FROM users WHERE id = ?", (onboarding["tenant_admin_user_id"],))["status"] == "active"
    login(client, "root")
    assert client.get("/api/platform/me", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/api/platform/me", headers={"Authorization": f"Bearer {client.cookies.get('qwenpaw_data_access')}"}).status_code == 403


def test_tenant_and_workspace_provisioning_states_and_names(multi_client, monkeypatch):
    client = multi_client
    platform, _ = bootstrap_and_onboard(client)
    headers = {"Authorization": f"Bearer {platform}"}
    assert client.post("/api/platform/tenants", headers=headers,
        json={"name": "tenant a", "admin_username": "root-b", "admin_password": PASSWORD}).status_code == 409
    other = client.post("/api/platform/tenants", headers=headers,
        json={"id": "tenant-b", "name": "Tenant B", "workspace_id": "ws-b", "admin_username": "root-b", "admin_password": PASSWORD})
    assert other.status_code == 201
    assert client.put("/api/platform/tenants/tenant-b", headers=headers, json={"name": "TENANT A"}).status_code == 409
    assert client.post("/api/platform/tenants/tenant-b/provision", headers=headers).status_code == 202
    assert client.post("/api/platform/tenants/tenant-b/provision", headers=headers).status_code == 409
    rows = client.get("/api/platform/tenants", headers=headers).json()["tenants"]
    assert next(r for r in rows if r["id"] == "tenant-b")["instance_status"] == "queued"
    login(client, "root")
    response = client.post("/api/workspaces", json={"name": "Analytics"})
    assert response.status_code == 201
    workspace_id = response.json()["workspace"]["id"]
    assert client.post("/api/workspaces", json={"name": "analytics"}).status_code == 409
    assert client.put(f"/api/workspaces/{workspace_id}", json={"description": "public"}).status_code == 200
    assert client.post(f"/api/workspaces/{workspace_id}/provision", json={"backend": "holo_age"}).status_code == 400
    monkeypatch.setenv("QWENPAW_DATA_PROVISION_BACKENDS", "neo4j,holo_age")
    queued = client.post(f"/api/workspaces/{workspace_id}/provision", json={"backend": "holo_age"})
    assert queued.status_code == 202 and queued.json()["backend"] == "holo_age"
    assert client.post(f"/api/workspaces/{workspace_id}/provision").status_code == 409
    cfg = client.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        workspace = query_one(conn, "SELECT * FROM workspaces WHERE id = ?", (workspace_id,))
        assert workspace["instance_version"] == "holo_age" and workspace["status"] == "provisioning"
        assert query_one(conn, "SELECT status FROM tenants WHERE id='tenant-a'")["status"] == "active"
    assert client.delete(f"/api/workspaces/{workspace_id}").status_code == 200


@pytest.mark.parametrize("backend,expected", [("neo4j", ["neo4j"]), ("holo_age", ["holo_age"]), ("invalid", ["neo4j"])])
def test_graph_backend_defaults(monkeypatch, backend, expected):
    monkeypatch.setenv("GRAPH_BACKEND", backend)
    assert configured_provision_backends() == expected
    monkeypatch.setenv("QWENPAW_DATA_PROVISION_BACKENDS", "holo_age,holo_age,invalid")
    monkeypatch.setenv("NEO4J_URI", "bolt://localhost:7687")
    assert configured_provision_backends() == ["holo_age"]


def test_raw_cypher_grants_are_scoped_query_bound_and_one_time(multi_client):
    client = multi_client
    cfg = client.app.state.auth_config
    platform, onboarding = bootstrap_and_onboard(client)
    headers = {"Authorization": f"Bearer {platform}"}
    body = {"actor_user_id": onboarding["tenant_admin_user_id"], "tenant_id": "tenant-a", "workspace_id": "ws-a",
            "cypher": "RETURN $value", "params": {"value": 1}, "ttl_seconds": 60}
    response = client.post("/api/platform/raw-cypher-grants", headers=headers, json=body)
    assert response.status_code == 201
    data = response.json()
    kwargs = dict(token=data["grant_token"], actor_user_id=body["actor_user_id"], tenant_id="tenant-a", workspace_id="ws-a",
                  query_digest=raw_query_digest(body["cypher"], body["params"]))
    assert consume_raw_cypher_grant(cfg.control_plane_dsn, **{**kwargs, "workspace_id": "other"}) is None
    assert consume_raw_cypher_grant(cfg.control_plane_dsn, **{**kwargs, "query_digest": raw_query_digest("RETURN 2", {})}) is None
    assert consume_raw_cypher_grant(cfg.control_plane_dsn, **kwargs)
    assert consume_raw_cypher_grant(cfg.control_plane_dsn, **kwargs) is None
    second = client.post("/api/platform/raw-cypher-grants", headers=headers, json=body).json()
    assert client.delete(f"/api/platform/raw-cypher-grants/{second['grant_id']}", headers=headers).status_code == 200
    assert consume_raw_cypher_grant(cfg.control_plane_dsn, **{**kwargs, "token": second["grant_token"]}) is None
    with get_db(cfg.control_plane_dsn) as conn:
        stored = query_one(conn, "SELECT token_hash FROM raw_cypher_grants WHERE id = ?", (data["grant_id"],))
        assert stored["token_hash"] != data["grant_token"]
    for _ in range(3):
        assert client.post("/api/platform/raw-cypher-grants", headers=headers, json=body).status_code == 201
    assert client.post("/api/platform/raw-cypher-grants", headers=headers, json=body).status_code == 429


def test_platform_suspension_revokes_tenant_sessions(multi_client):
    platform, _ = bootstrap_and_onboard(multi_client)
    login(multi_client, "root")
    assert multi_client.post("/api/platform/tenants/tenant-a/suspend", headers={"Authorization": f"Bearer {platform}"}).status_code == 200
    assert multi_client.get("/api/auth/me").status_code in {401, 403}
    assert multi_client.post("/api/auth/refresh").status_code in {401, 403}


def test_public_join_request_approval_and_revoked_applicant(multi_client):
    cfg = multi_client.app.state.auth_config
    seed_user(cfg, user_id="root", username="root", role="tenant_admin")
    seed_user(cfg)
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute("INSERT INTO workspaces (id, tenant_id, name, join_open, instance_status) VALUES ('w2', 't1', 'Open', 1, 'ready')")
        conn.commit()
    login(multi_client)
    body = {"target_type": "workspace", "target_id": "w2"}
    response = multi_client.post("/api/access-requests", json=body)
    assert response.status_code == 201
    request_id = response.json()["id"]
    assert multi_client.post("/api/access-requests", json=body).status_code == 409
    multi_client.cookies.clear()
    login(multi_client, "root")
    assert multi_client.get("/api/access-requests").json()["requests"][0]["user_id"] == "u1"
    assert multi_client.post(f"/api/access-requests/{request_id}/approve").status_code == 200
    assert multi_client.post(f"/api/access-requests/{request_id}/approve").status_code == 409
    assert ControlPlaneReader(cfg.control_plane_dsn).get_user_workspace_ids("u1", "t1") == ["w1", "w2"]
    # Approval re-checks that a persisted applicant is still active in this tenant.
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute("INSERT INTO access_requests (id, user_id, tenant_id, target_type, target_id) VALUES ('pending', 'u1', 't1', 'workspace', 'w2')")
        conn.execute("UPDATE users SET status='disabled' WHERE id='u1'")
        conn.commit()
    assert multi_client.post("/api/access-requests/pending/approve").status_code == 403


def test_join_request_cross_tenant_denied_and_cancel(multi_client):
    cfg = multi_client.app.state.auth_config
    seed_user(cfg)
    seed_user(cfg, user_id="foreign", tenant_id="t2", username="foreign", workspace_id="foreign-ws", role="tenant_admin")
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute("UPDATE workspaces SET join_open=1")
        conn.execute("INSERT INTO workspaces (id, tenant_id, name, join_open, instance_status) VALUES ('w2', 't1', 'Open', 1, 'ready')")
        conn.commit()
    login(multi_client)
    assert multi_client.post("/api/access-requests", json={"target_type": "workspace", "target_id": "foreign-ws"}).status_code == 404
    response = multi_client.post("/api/access-requests", json={"target_type": "workspace", "target_id": "w2"})
    assert response.status_code == 201
    request_id = response.json()["id"]
    assert multi_client.post(f"/api/access-requests/{request_id}/cancel").status_code == 200
    assert multi_client.post(f"/api/access-requests/{request_id}/cancel").status_code == 404

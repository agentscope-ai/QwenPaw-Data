"""Auth test helpers with an import name independent of pytest collection order."""

from sqlalchemy import insert

from qwenpaw_data_auth.database import get_db, new_id, role_bindings, tenants, users, workspaces
from qwenpaw_data_auth.password import hash_password

PASSWORD = "Strong!Pass9"


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

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data_auth.config import ACCESS_TOKEN_TTL_DEFAULT, REFRESH_TOKEN_TTL_DEFAULT
from qwenpaw_data_auth.database import get_db, new_id, query_one
from qwenpaw_data_auth.password import hash_password
from qwenpaw_data_auth.server import create_app


PASSWORD = "Strong!Pass9"
RESET_PASSWORD = "Reset!Pass8"
CHANGED_PASSWORD = "Changed!Pass7"


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
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client, dsn


def bootstrap_and_onboard(client: TestClient) -> tuple[str, str, str]:
    bootstrap = client.post(
        "/api/platform/bootstrap",
        headers={"X-QwenPaw-Data-Bootstrap-Token": "bootstrap-test-token"},
        json={"username": "platform", "password": PASSWORD},
    )
    assert bootstrap.status_code == 201
    platform_token = bootstrap.cookies.get("qwenpaw_data_platform_access")
    assert platform_token
    onboard = client.post(
        "/api/platform/tenants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={
            "id": "tenant-a",
            "name": "Tenant A",
            "workspace_id": "ws-a",
            "admin_username": "root",
            "admin_password": PASSWORD,
            "endpoint_ref": "bolt://127.0.0.1:17687",
            "secret_ref": "env://TEST_WORKSPACE_NEO4J_SECRET",
            "instance_version": "1",
        },
    )
    assert onboard.status_code == 201
    assert onboard.json()["onboarding_state"] == "pending"
    activate = client.post(
        f"/api/platform/tenants/{onboard.json()['tenant_id']}/resume",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={
            "endpoint_ref": "bolt://127.0.0.1:17687",
            "secret_ref": "env://TEST_WORKSPACE_NEO4J_SECRET",
            "instance_version": "1",
        },
    )
    assert activate.status_code == 200
    return (
        platform_token,
        onboard.json()["tenant_id"],
        onboard.json()["default_workspace_id"],
    )


def tenant_login(client: TestClient, username: str, tenant_id: str = "tenant-a"):
    del tenant_id  # tenant is discovered from the globally unique username
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 200, response.text
    return response


def test_raw_cypher_grant_platform_flow(client):
    http, dsn = client
    platform_token, tenant_id, workspace_id = bootstrap_and_onboard(http)
    with get_db(dsn) as conn:
        admin = query_one(
            conn,
            "SELECT id FROM users WHERE tenant_id = ? AND username = 'root'",
            (tenant_id,),
        )
    assert admin
    body = {
        "actor_user_id": admin["id"],
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "cypher": "MATCH (n) RETURN n LIMIT $limit",
        "params": {"limit": 1},
        "ttl_seconds": 60,
    }

    invalid_ttl = http.post(
        "/api/platform/raw-cypher-grants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={**body, "ttl_seconds": "not-a-number"},
    )
    assert invalid_ttl.status_code == 400
    missing_target = http.post(
        "/api/platform/raw-cypher-grants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={**body, "actor_user_id": "missing"},
    )
    assert missing_target.status_code == 404

    with TestClient(http.app) as tenant_http:
        tenant_session = tenant_login(tenant_http, "root")
        tenant_token = tenant_session.cookies.get("qwenpaw_data_access")
        assert tenant_token
        non_platform = tenant_http.post(
            "/api/platform/raw-cypher-grants",
            headers={"Authorization": f"Bearer {tenant_token}"},
            json=body,
        )
        assert non_platform.status_code == 403

    issued = http.post(
        "/api/platform/raw-cypher-grants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json=body,
    )
    assert issued.status_code == 201
    payload = issued.json()
    assert payload["grant_id"]
    assert payload["grant_token"]
    assert payload["expires_at"]
    with get_db(dsn) as conn:
        stored = query_one(
            conn,
            "SELECT token_hash FROM raw_cypher_grants WHERE id = ?",
            (payload["grant_id"],),
        )
        second_admin_id = new_id()
        conn.execute(
            "INSERT INTO platform_admins (id, username, password_hash) VALUES (?, ?, ?)",
            (second_admin_id, "platform-2", hash_password(PASSWORD)),
        )
        conn.commit()
    assert stored
    assert stored["token_hash"] != payload["grant_token"]

    with TestClient(http.app) as second_http:
        second_login = second_http.post(
            "/api/platform/login",
            json={"username": "platform-2", "password": PASSWORD},
        )
        second_token = second_login.cookies.get("qwenpaw_data_platform_access")
        assert second_token
        denied_revoke = second_http.delete(
            f"/api/platform/raw-cypher-grants/{payload['grant_id']}",
            headers={"Authorization": f"Bearer {second_token}"},
        )
        assert denied_revoke.status_code == 404
    revoked = http.delete(
        f"/api/platform/raw-cypher-grants/{payload['grant_id']}",
        headers={"Authorization": f"Bearer {platform_token}"},
    )
    assert revoked.status_code == 200

    for index in range(4):
        response = http.post(
            "/api/platform/raw-cypher-grants",
            headers={"Authorization": f"Bearer {platform_token}"},
            json={**body, "cypher": f"RETURN {index}"},
        )
        assert response.status_code == 201
    rate_limited = http.post(
        "/api/platform/raw-cypher-grants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={**body, "cypher": "RETURN 99"},
    )
    assert rate_limited.status_code == 429


def test_platform_is_isolated_and_onboarding_is_atomic(client):
    http, dsn = client
    assert (
        http.post(
            "/api/platform/bootstrap",
            json={"username": "attacker", "password": PASSWORD},
        ).status_code
        == 403
    )
    assert (
        http.post(
            "/api/auth/register",
            json={"username": "legacy-root", "password": PASSWORD},
        ).status_code
        == 403
    )
    platform_token, tenant_id, workspace_id = bootstrap_and_onboard(http)

    denied = http.get(
        "/api/workspaces",
        headers={"Authorization": f"Bearer {platform_token}"},
    )
    assert denied.status_code == 403

    with get_db(dsn) as conn:
        tenant = query_one(conn, "SELECT * FROM tenants WHERE id = ?", (tenant_id,))
        workspace = query_one(
            conn, "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
        )
        admin = query_one(
            conn,
            "SELECT * FROM users WHERE tenant_id = ? AND username = 'root'",
            (tenant_id,),
        )
        binding = query_one(
            conn,
            "SELECT * FROM role_bindings WHERE user_id = ? AND role = 'tenant_admin'",
            (admin["id"],),
        )
    assert tenant["onboarding_state"] == "ready"
    assert workspace["endpoint_ref"] == "bolt://127.0.0.1:17687"
    assert binding["scope_type"] == "tenant"
    assert binding["scope_id"] == tenant_id

    duplicate_username = http.post(
        "/api/platform/tenants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={
            "id": "tenant-b",
            "name": "Tenant B",
            "workspace_id": "ws-b",
            "admin_username": "root",
            "admin_password": PASSWORD,
        },
    )
    assert duplicate_username.status_code == 409


def test_platform_admin_can_queue_workspace_provisioning(client):
    http, dsn = client
    bootstrap = http.post(
        "/api/platform/bootstrap",
        headers={"X-QwenPaw-Data-Bootstrap-Token": "bootstrap-test-token"},
        json={"username": "platform", "password": PASSWORD},
    )
    platform_token = bootstrap.cookies.get("qwenpaw_data_platform_access")
    onboard = http.post(
        "/api/platform/tenants",
        headers={"Authorization": f"Bearer {platform_token}"},
        json={
            "id": "tenant-queued",
            "name": "Queued Tenant",
            "workspace_id": "ws-queued",
            "admin_username": "queued-root",
            "admin_password": PASSWORD,
        },
    )
    assert onboard.status_code == 201

    queued = http.post(
        "/api/platform/tenants/tenant-queued/provision",
        headers={"Authorization": f"Bearer {platform_token}"},
    )
    assert queued.status_code == 202
    assert queued.json()["instance_status"] == "queued"
    assert (
        http.post(
            "/api/platform/tenants/tenant-queued/provision",
            headers={"Authorization": f"Bearer {platform_token}"},
        ).status_code
        == 409
    )

    with get_db(dsn) as conn:
        workspace = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-queued'")
        tenant = query_one(conn, "SELECT * FROM tenants WHERE id = 'tenant-queued'")
    assert workspace["instance_status"] == "queued"
    assert tenant["onboarding_state"] == "pending"

    listed = http.get(
        "/api/platform/tenants",
        headers={"Authorization": f"Bearer {platform_token}"},
    )
    listed_tenant = next(
        item for item in listed.json()["tenants"] if item["id"] == "tenant-queued"
    )
    assert listed_tenant["instance_status"] == "queued"


def test_ws_admin_cannot_see_or_write_outside_scope(client):
    http, dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")

    ws2 = http.post("/api/workspaces", json={"name": "Other"}).json()["workspace"]["id"]
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE workspaces SET status = 'active', instance_status = 'ready' WHERE id = ?",
            (ws2,),
        )
        conn.commit()
    assert (
        http.post(
            "/api/users",
            json={
                "username": "workspace-admin",
                "password": PASSWORD,
                "role": "ws_admin",
                "workspace_id": "ws-a",
            },
        ).status_code
        == 201
    )
    assert (
        http.post(
            "/api/users",
            json={
                "username": "other-member",
                "password": PASSWORD,
                "role": "member",
                "workspace_id": ws2,
            },
        ).status_code
        == 201
    )

    tenant_login(http, "workspace-admin")
    users = http.get("/api/users")
    assert users.status_code == 200
    assert [u["username"] for u in users.json()["users"]] == ["workspace-admin"]
    bindings = http.get("/api/roles/bindings").json()["bindings"]
    assert all(b["scope_id"] == "ws-a" for b in bindings)
    assert all(b["role"] != "tenant_admin" for b in bindings)
    assert (
        http.put(f"/api/workspaces/{ws2}", json={"name": "hijack"}).status_code == 403
    )
    assert http.get(f"/api/users?workspace_id={ws2}").status_code == 403
    assert (
        http.post(
            "/api/users",
            json={
                "username": "forged-workspace-admin",
                "password": PASSWORD,
                "role": "ws_admin",
                "workspace_id": "ws-a",
            },
        ).status_code
        == 403
    )
    member = http.post(
        "/api/users",
        json={
            "username": "new-member",
            "password": PASSWORD,
            "role": "member",
            "workspace_id": "ws-a",
        },
    )
    assert member.status_code == 201
    assert (
        http.post(
            "/api/roles/bindings",
            json={
                "user_id": member.json()["user"]["id"],
                "role": "ws_admin",
                "scope_type": "workspace",
                "scope_id": "ws-a",
            },
        ).status_code
        == 201
    )


def test_me_and_workspace_management_follow_active_workspace_role(client):
    http, dsn = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")

    created_workspace = http.post(
        "/api/workspaces",
        json={"name": "Member Workspace"},
    )
    assert created_workspace.status_code == 201
    member_workspace_id = created_workspace.json()["workspace"]["id"]
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE workspaces SET status = 'active', instance_status = 'ready' WHERE id = ?",
            (member_workspace_id,),
        )
        conn.commit()

    mixed_user = http.post(
        "/api/users",
        json={
            "username": "mixed-user",
            "password": PASSWORD,
            "role": "ws_admin",
            "workspace_id": "ws-a",
        },
    )
    assert mixed_user.status_code == 201
    mixed_user_id = mixed_user.json()["user"]["id"]
    member_binding = http.post(
        "/api/roles/bindings",
        json={
            "user_id": mixed_user_id,
            "role": "member",
            "scope_type": "workspace",
            "scope_id": member_workspace_id,
        },
    )
    assert member_binding.status_code == 201
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE users SET active_workspace_id = 'ws-a' WHERE id = ?",
            (mixed_user_id,),
        )
        conn.commit()

    tenant_login(http, "mixed-user")
    initial_me = http.get("/api/auth/me")
    assert initial_me.status_code == 200
    assert initial_me.json()["active_workspace_role"] == "ws_admin"
    assert "user:manage" in initial_me.json()["capabilities"]
    assert initial_me.json()["must_change_password"] is False

    switched = http.post(
        "/api/auth/switch-workspace",
        json={"workspace_id": member_workspace_id},
    )
    assert switched.status_code == 200
    member_me = http.get("/api/auth/me")
    assert member_me.status_code == 200
    assert member_me.json()["role"] == "ws_admin"
    assert member_me.json()["active_workspace_role"] == "member"
    assert "user:manage" not in member_me.json()["capabilities"]
    assert http.get("/api/users").status_code == 403
    assert http.get("/api/roles/bindings").status_code == 403
    assert (
        http.post(
            "/api/users",
            json={
                "username": "forged-member",
                "password": PASSWORD,
                "role": "member",
                "workspace_id": member_workspace_id,
            },
        ).status_code
        == 403
    )

    listed = http.get("/api/workspaces")
    assert listed.status_code == 200
    by_id = {workspace["id"]: workspace for workspace in listed.json()["workspaces"]}
    assert by_id["ws-a"]["can_manage"] is True
    assert by_id[member_workspace_id]["can_manage"] is False


def test_management_guards_and_forced_password_change(client):
    http, dsn = client
    platform_token, _, _ = bootstrap_and_onboard(http)
    platform_me = http.get(
        "/api/platform/me",
        headers={"Authorization": f"Bearer {platform_token}"},
    )
    assert platform_me.status_code == 200
    assert platform_me.json()["capabilities"] == []
    assert platform_me.json()["must_change_password"] is False

    tenant_login(http, "root")
    users = http.get("/api/users").json()["users"]
    root_user = next(user for user in users if user["username"] == "root")
    assert root_user["can_disable"] is False
    assert root_user["deny_reason"] == "CANNOT_MANAGE_SELF"

    bindings = http.get("/api/roles/bindings").json()["bindings"]
    root_binding = next(
        binding for binding in bindings if binding["user_id"] == root_user["id"]
    )
    assert root_binding["can_delete"] is False
    assert root_binding["deny_reason"] == "CANNOT_MODIFY_SELF"
    assert (
        http.post(
            "/api/users",
            json={
                "username": "unicode-password",
                "password": "Abcdefg中",
                "role": "member",
                "workspace_id": "ws-a",
            },
        ).status_code
        == 400
    )

    created = http.post(
        "/api/users",
        json={
            "username": "forced-user",
            "password": PASSWORD,
            "role": "member",
            "workspace_id": "ws-a",
        },
    )
    assert created.status_code == 201
    forced_user_id = created.json()["user"]["id"]
    reset = http.put(
        f"/api/users/{forced_user_id}",
        json={"password": RESET_PASSWORD},
    )
    assert reset.status_code == 200
    with get_db(dsn) as conn:
        forced_user = query_one(
            conn,
            "SELECT force_password_change FROM users WHERE id = ?",
            (forced_user_id,),
        )
    assert forced_user["force_password_change"] == 1

    login = http.post(
        "/api/auth/login",
        json={"username": "forced-user", "password": RESET_PASSWORD},
    )
    assert login.status_code == 200
    assert login.json()["must_change_password"] is True
    assert http.get("/api/auth/me").json()["must_change_password"] is True

    changed = http.post(
        "/api/auth/change-password",
        json={"new_password": CHANGED_PASSWORD},
    )
    assert changed.status_code == 200
    assert http.get("/api/auth/me").json()["must_change_password"] is False
    missing_current = http.post(
        "/api/auth/change-password",
        json={"new_password": PASSWORD},
    )
    assert missing_current.status_code == 400


def test_last_tenant_admin_and_refresh_replay(client):
    http, dsn = client
    bootstrap_and_onboard(http)
    login = tenant_login(http, "root")
    old_refresh = login.cookies.get("qwenpaw_data_refresh")
    assert old_refresh

    with get_db(dsn) as conn:
        conn.execute(
            """
            INSERT INTO workspaces
                (id, tenant_id, name, status, instance_status)
            VALUES ('ws-second', 'tenant-a', 'Second', 'active', 'ready')
            """
        )
        conn.commit()
    switched = http.post(
        "/api/auth/switch-workspace", json={"workspace_id": "ws-second"}
    )
    assert switched.status_code == 200

    first = http.post("/api/auth/refresh")
    assert first.status_code == 200
    assert http.get("/api/auth/me").json()["active_workspace_id"] == "ws-second"
    http.cookies.set("qwenpaw_data_refresh", old_refresh, path="/api/auth/refresh")
    replay = http.post("/api/auth/refresh")
    assert replay.status_code == 401
    tenant_login(http, "root")

    with get_db(dsn) as conn:
        root = query_one(
            conn,
            "SELECT id FROM users WHERE tenant_id = 'tenant-a' AND username = 'root'",
        )
    # Self-disable is forbidden, which also protects a sole tenant administrator.
    assert http.post(f"/api/users/{root['id']}/disable").status_code == 400

    second = http.post(
        "/api/users",
        json={"username": "root-2", "password": PASSWORD, "role": "tenant_admin"},
    )
    assert second.status_code == 201
    tenant_login(http, "root-2")
    second_binding = next(
        binding
        for binding in second.json()["user"]["role_bindings"]
        if binding["role"] == "tenant_admin"
    )
    assert second_binding["scope_type"] == "tenant"
    assert second_binding["scope_id"] == "tenant-a"
    # Even with multiple supported admins, an admin cannot remove its own
    # final authority.
    denied = http.delete(f"/api/roles/bindings/{second_binding['id']}")
    assert denied.status_code == 403


def test_authorization_code_is_origin_bound_and_single_use(client):
    http, _ = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")
    issued = http.post(
        "/api/auth/authorize",
        json={"redirect_uri": "http://localhost:3000/metrics"},
    )
    assert issued.status_code == 200
    code = issued.json()["code"]

    mismatch = http.post(
        "/api/auth/exchange",
        json={"code": code, "redirect_uri": "http://localhost:3001/account"},
    )
    assert mismatch.status_code == 400
    exchanged = http.post(
        "/api/auth/exchange",
        json={"code": code, "redirect_uri": "http://localhost:3000/metrics"},
    )
    assert exchanged.status_code == 200
    replay = http.post(
        "/api/auth/exchange",
        json={"code": code, "redirect_uri": "http://localhost:3000/metrics"},
    )
    assert replay.status_code == 400


@pytest.mark.parametrize(
    "username",
    ["a", "a" * 33, "has space", "has/slash", "has.dot", "张三", ""],
)
def test_create_user_rejects_malformed_usernames(client, username):
    http, _ = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")
    response = http.post(
        "/api/users",
        json={"username": username, "password": PASSWORD, "workspace_id": "ws-a"},
    )
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith("Invalid username")


@pytest.mark.parametrize("username", ["ab", "a" * 32, "al-ic_e", "User9"])
def test_create_user_accepts_valid_usernames(client, username):
    http, _ = client
    bootstrap_and_onboard(http)
    tenant_login(http, "root")
    response = http.post(
        "/api/users",
        json={"username": username, "password": PASSWORD, "workspace_id": "ws-a"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["user"]["username"] == username


def _cookie_max_age(response, name: str) -> int:
    for header in response.headers.get_list("set-cookie"):
        if not header.startswith(f"{name}="):
            continue
        for attribute in header.split(";"):
            key, _, value = attribute.strip().partition("=")
            if key.lower() == "max-age":
                return int(value)
        raise AssertionError(f"{name} cookie carries no Max-Age")
    raise AssertionError(f"{name} cookie was not set")


def test_session_cookies_carry_the_configured_lifetimes(client):
    """Cookie Max-Age matches the configured access and refresh ttls."""
    http, _ = client
    bootstrap_and_onboard(http)

    login = tenant_login(http, "root")
    assert _cookie_max_age(login, "qwenpaw_data_access") == ACCESS_TOKEN_TTL_DEFAULT
    assert _cookie_max_age(login, "qwenpaw_data_refresh") == REFRESH_TOKEN_TTL_DEFAULT

    platform_login = http.post(
        "/api/platform/login",
        json={"username": "platform", "password": PASSWORD},
    )
    assert platform_login.status_code == 200, platform_login.text
    assert (
        _cookie_max_age(platform_login, "qwenpaw_data_platform_access")
        == ACCESS_TOKEN_TTL_DEFAULT
    )

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from qwenpaw_data_auth import AuthenticationError, AuthConfig, authenticate_request, bootstrap_local, is_safe_loopback_request, prepare_auth_config
from qwenpaw_data_auth.database import get_db, query_one, reset_engines
from qwenpaw_data_auth.password import verify_password
from qwenpaw_data_auth.server import create_app
from qwenpaw_data_auth.token import TokenIssuer
from conftest import PASSWORD, seed_user

pytestmark = pytest.mark.usefixtures("local_mode")


def test_bootstrap_persistence_permissions_and_idempotence(tmp_path):
    cfg = AuthConfig(home=tmp_path / "state", cm_base_url="")
    first = bootstrap_local(cfg)
    with get_db(first.control_plane_dsn) as conn:
        user = query_one(conn, "SELECT * FROM users WHERE id = '__default__'")
        assert query_one(conn, "SELECT count(*) AS n FROM users")["n"] == 1
        assert query_one(conn, "SELECT count(*) AS n FROM tenants")["n"] == 1
        assert query_one(conn, "SELECT count(*) AS n FROM workspaces")["n"] == 1
        assert query_one(conn, "SELECT role, scope_type, scope_id FROM role_bindings") == {"role": "tenant_admin", "scope_type": "tenant", "scope_id": "default"}
    assert user["active_workspace_id"] == "default"
    assert not verify_password("admin", user["password_hash"])
    assert not verify_password(PASSWORD, user["password_hash"])
    reset_engines()
    second = bootstrap_local(cfg)
    assert first.secret_keys == second.secret_keys
    assert first.service_secret == second.service_secret
    assert len(first.secret_keys[0]) >= 64
    assert first.secret_keys[0] != first.service_secret
    assert not first.auth_enabled and not first.auth_url
    with get_db(second.control_plane_dsn) as conn:
        assert query_one(conn, "SELECT * FROM users WHERE id = '__default__'") == user
        assert query_one(conn, "SELECT count(*) AS n FROM role_bindings")["n"] == 1
    if os.name != "nt":
        assert (cfg.home / "auth").stat().st_mode & 0o777 == 0o700
        assert (cfg.home / "auth" / "secrets.json").stat().st_mode & 0o777 == 0o600
        assert (cfg.home / "auth" / "bootstrap.lock").stat().st_mode & 0o777 == 0o600
        assert (cfg.home / "control_plane.db").stat().st_mode & 0o777 == 0o600
    assert "password" not in json.loads((cfg.home / "auth" / "secrets.json").read_text())


def test_concurrent_bootstrap_threads_and_processes(tmp_path):
    cfg = AuthConfig(home=tmp_path / "threads", cm_base_url="")
    with ThreadPoolExecutor(max_workers=6) as pool:
        configs = list(pool.map(lambda _: bootstrap_local(cfg), range(6)))
    assert len({tuple(c.secret_keys) for c in configs}) == 1
    home = tmp_path / "processes"
    script = "from qwenpaw_data_auth import AuthConfig,bootstrap_local; import hashlib,sys; cfg=bootstrap_local(AuthConfig(home=sys.argv[1],cm_base_url='')); print(hashlib.sha256(cfg.secret_keys[0].encode()).hexdigest())"
    processes = [subprocess.Popen([sys.executable, "-c", script, str(home)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(4)]
    outputs = [p.communicate(timeout=60) for p in processes]
    assert all(p.returncode == 0 for p in processes), outputs
    assert len({out.strip() for out, _ in outputs}) == 1
    cfg = bootstrap_local(AuthConfig(home=home, cm_base_url=""))
    with get_db(cfg.control_plane_dsn) as conn:
        assert query_one(conn, "SELECT count(*) AS n FROM users")["n"] == 1
        assert query_one(conn, "SELECT count(*) AS n FROM role_bindings")["n"] == 1


def test_restart_does_not_restore_revoked_membership_or_disabled_user(tmp_path):
    cfg = bootstrap_local(AuthConfig(home=tmp_path / "state", cm_base_url=""))
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute("DELETE FROM role_bindings WHERE user_id = '__default__'")
        conn.execute("UPDATE users SET status = 'disabled' WHERE id = '__default__'")
        conn.commit()
    cfg = bootstrap_local(cfg)
    with TestClient(create_app(cfg), base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/auth/me").status_code == 401
    with get_db(cfg.control_plane_dsn) as conn:
        assert query_one(conn, "SELECT count(*) AS n FROM role_bindings")["n"] == 0


def test_local_startup_identity_and_password_setup(tmp_path):
    cfg = AuthConfig(home=tmp_path / "state", cm_base_url="")
    app = create_app(cfg)
    assert not cfg.home.exists()
    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        status = client.get("/api/auth/status").json()
        assert status["mode"] == "local" and not status["enabled"] and status["has_users"]
        me = client.get("/api/auth/me")
        assert me.status_code == 200 and me.json()["user_id"] == "__default__"
        assert client.get("/api/workspaces").status_code == 200
        assert client.post("/api/auth/change-password", json={"new_password": PASSWORD}).status_code == 200
        client.cookies.clear()
        login = client.post("/api/auth/login", json={"username": "local_admin", "password": PASSWORD})
        assert login.status_code == 200
        assert client.get("/api/auth/me").json()["user_id"] == "__default__"


@pytest.mark.parametrize("peer,base_url,headers", [
    ("192.0.2.1", "http://localhost", {}),
    ("localhost", "http://localhost", {}),
    ("127.0.0.1", "http://attacker.example", {}),
    ("127.0.0.1", "http://localhost.attacker.example", {}),
    ("127.0.0.1", "http://localhost", {"Origin": "https://attacker.example"}),
    ("127.0.0.1", "http://localhost", {"Origin": "null"}),
    ("127.0.0.1", "http://localhost", {"Referer": "https://attacker.example/path"}),
    ("127.0.0.1", "http://localhost", {"X-Forwarded-For": "127.0.0.1"}),
    ("127.0.0.1", "http://localhost", {"Sec-Fetch-Site": "cross-site"}),
])
def test_anonymous_local_access_rejects_unsafe_clients(tmp_path, peer, base_url, headers):
    with TestClient(create_app(AuthConfig(home=tmp_path / "state", cm_base_url="")), base_url=base_url, client=(peer, 50000)) as client:
        assert client.get("/api/auth/me", headers=headers).status_code == 403


@pytest.mark.parametrize("peer,host", [("127.0.0.1", "localhost"), ("::1", "[::1]"), ("127.0.0.2", "127.0.0.2")])
def test_actual_loopback_addresses_are_supported(tmp_path, peer, host):
    with TestClient(create_app(AuthConfig(home=tmp_path / "state", cm_base_url="")), base_url="http://localhost", client=(peer, 50000)) as client:
        assert client.get("/api/auth/me", headers={"Host": host, "Origin": f"http://{host}:3000"}).status_code == 200


@pytest.mark.parametrize("headers", [
    {"Authorization": "Bearer garbage"}, {"Authorization": "Basic garbage"},
    {"Authorization": "Bearer "}, {"X-QwenPaw-Data-Service-Secret": "wrong"},
    {"X-User-Id": "__default__"}, {"X-QwenPaw-Data-Api-Token": "wrong"},
])
def test_supplied_invalid_credentials_never_fall_through(tmp_path, headers):
    with TestClient(create_app(AuthConfig(home=tmp_path / "state", cm_base_url="")), base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert client.get("/api/workspaces", headers=headers).status_code == 401


def test_bad_cookie_and_bad_header_take_precedence_over_local_fallback(tmp_path):
    with TestClient(create_app(AuthConfig(home=tmp_path / "state", cm_base_url="")), base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        client.cookies.set("qwenpaw_data_access", "bad-cookie")
        assert client.get("/api/auth/me").status_code == 401
        cfg = client.app.state.auth_config
        token = TokenIssuer(cfg.secret_keys[0]).issue_access("__default__", "default", "default", "tenant_admin")
        client.cookies.set("qwenpaw_data_access", token)
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_configured_api_token_is_enforced(tmp_path):
    cfg = AuthConfig(home=tmp_path / "state", api_token="configured-api-token", cm_base_url="")
    with TestClient(create_app(cfg), base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        assert client.get("/api/auth/me").status_code == 401
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer invalid"}).status_code == 401
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer configured-api-token"}).json()["user_id"] == "__default__"
        assert client.get("/api/auth/me", headers={"X-QwenPaw-Data-Api-Token": "configured-api-token"}).status_code == 200
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer configured-api-token", "X-QwenPaw-Data-Api-Token": "wrong"}).status_code == 401
        assert client.get("/api/auth/me", headers={"Authorization": "Bearer configured-api-token", "Origin": "https://evil.example"}).status_code == 403


def test_multi_user_has_no_anonymous_admin_even_on_loopback(multi_client):
    cfg = multi_client.app.state.auth_config
    assert multi_client.get("/api/auth/status").json()["enabled"]
    assert multi_client.get("/api/auth/me").status_code == 401
    assert multi_client.get("/api/workspaces").status_code == 401
    with get_db(cfg.control_plane_dsn) as conn:
        assert query_one(conn, "SELECT count(*) AS n FROM users")["n"] == 0
    assert multi_client.post("/api/auth/register", json={"username": "admin", "password": PASSWORD}).status_code == 403


def test_machine_delegation_requires_explicit_path_and_current_rbac(multi_config):
    app = create_app(multi_config)

    @app.get("/machine")
    async def machine(request: Request):
        try:
            return asdict(authenticate_request(request, allow_machine=True))
        except AuthenticationError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    with TestClient(app, base_url="http://localhost", client=("192.0.2.1", 50000)) as client:
        cfg = app.state.auth_config
        seed_user(cfg)
        headers = {"X-QwenPaw-Data-Service-Secret": cfg.service_secret, "X-User-Id": "u1"}
        assert client.get("/machine", headers=headers).json()["role"] == "member"
        assert client.get("/api/auth/me", headers=headers).status_code == 401
        assert client.get("/machine", headers={"X-User-Id": "u1"}).status_code == 401
        assert client.get("/machine", headers={"X-QwenPaw-Data-Service-Secret": cfg.service_secret}).status_code == 403
        with get_db(cfg.control_plane_dsn) as conn:
            conn.execute("DELETE FROM role_bindings WHERE user_id = 'u1'")
            conn.commit()
        assert client.get("/machine", headers=headers).status_code == 403


def test_machine_local_identity_only_after_constant_time_validation(tmp_path):
    app = create_app(AuthConfig(home=tmp_path / "state", api_token="api-key", cm_base_url=""))

    @app.get("/machine")
    async def machine(request: Request):
        try:
            return asdict(authenticate_request(request, allow_machine=True))
        except AuthenticationError as exc:
            raise HTTPException(exc.status_code, exc.detail) from exc

    with TestClient(app, base_url="http://localhost", client=("192.0.2.1", 50000)) as client:
        cfg = app.state.auth_config
        assert client.get("/machine").status_code == 401
        assert client.get("/machine", headers={"Authorization": "Bearer api-key"}).json()["user_id"] == "__default__"
        assert client.get("/machine", headers={"X-QwenPaw-Data-Service-Secret": cfg.service_secret}).json()["user_id"] == "__default__"
        assert client.get("/machine", headers={"X-QwenPaw-Data-Service-Secret": cfg.secret_keys[0]}).status_code == 401


def test_secrets_are_not_logged_and_corrupt_state_fails_closed(tmp_path, caplog):
    cfg = prepare_auth_config(AuthConfig(home=tmp_path / "state"))
    assert cfg.secret_keys[0] not in caplog.text and cfg.service_secret not in caplog.text
    path = cfg.home / "auth" / "secrets.json"
    path.write_text('{}')
    with pytest.raises(RuntimeError, match="persisted"):
        prepare_auth_config(cfg)


@pytest.mark.parametrize("dsn", ["relative.db", "sqlite:///relative.db", "sqlite+aiosqlite:///relative.db", "sqlite:///relative.db?timeout=30"])
def test_explicit_runtime_home_controls_relative_database_paths(tmp_path, dsn):
    cfg = bootstrap_local(AuthConfig(home=tmp_path / "configured", control_plane_dsn=dsn, cm_base_url=""))
    assert (cfg.home / "relative.db").is_file()
    assert not (tmp_path / "runtime" / "relative.db").exists()
    with get_db(cfg.control_plane_dsn) as conn:
        assert query_one(conn, "SELECT count(*) AS n FROM users")["n"] == 1


@pytest.mark.parametrize("dsn", ["sqlite:///:memory:", "sqlite+aiosqlite:///:memory:"])
def test_preparing_memory_database_preserves_memory_url(tmp_path, dsn):
    cfg = prepare_auth_config(AuthConfig(home=tmp_path / "configured", control_plane_dsn=dsn))
    assert cfg.control_plane_dsn == dsn
    assert not (cfg.home / ":memory:").exists()


@pytest.mark.parametrize("host", ["localhost/path", "localhost?x=1", "localhost#x", "localhost\\path",
                                 "localhost other", "localhost\t", "localhost\r\n", "user@localhost", "localhost:bad", ""])
def test_malformed_host_authority_is_not_safe_loopback(host):
    request = Request({"type": "http", "scheme": "http", "path": "/", "query_string": b"",
                       "server": ("localhost", 80), "client": ("127.0.0.1", 50000),
                       "headers": [(b"host", host.encode())]})
    assert not is_safe_loopback_request(request)


def test_runtime_secret_symlink_is_refused(tmp_path):
    cfg = AuthConfig(home=tmp_path / "state")
    directory = cfg.home / "auth"
    directory.mkdir(parents=True)
    target = tmp_path / "external.json"
    target.write_text('{}')
    (directory / "secrets.json").symlink_to(target)
    with pytest.raises(OSError):
        prepare_auth_config(cfg)
    assert target.read_text() == '{}'


@pytest.mark.parametrize("home", ["relative/home", " /tmp/home", "/tmp/home "])
def test_runtime_home_rejects_ambiguous_configuration(home, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", home)
    with pytest.raises(ValueError, match="QWENPAW_DATA_HOME"):
        AuthConfig.from_env()
    with pytest.raises(ValueError, match="QWENPAW_DATA_HOME"):
        AuthConfig(home=home)


def test_windows_secret_io_does_not_require_posix_permission_functions(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from qwenpaw_data_auth import local

    class WindowsRuntimeOS:
        name = "nt"

        def __getattr__(self, name):
            if name == "fchmod":
                raise AssertionError("Windows bootstrap must not call fchmod")
            return getattr(os, name)

    monkeypatch.setattr(local, "os", WindowsRuntimeOS())
    monkeypatch.setitem(
        sys.modules, "msvcrt",
        SimpleNamespace(LK_LOCK=1, locking=lambda fd, mode, count: None),
    )
    first = prepare_auth_config(AuthConfig(home=tmp_path / "state"))
    second = prepare_auth_config(AuthConfig(home=tmp_path / "state"))
    assert first.secret_keys == second.secret_keys
    assert first.service_secret == second.service_secret

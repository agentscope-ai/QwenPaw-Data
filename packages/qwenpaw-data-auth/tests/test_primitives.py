from __future__ import annotations

import base64
import json
import time

import pytest
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from qwenpaw_data_auth import DEFAULT_PRINCIPAL, Principal, TokenIssuer, TokenVerifier, TokenVerifyError, check_permission
from qwenpaw_data_auth.config import AuthConfig, REFRESH_TOKEN_TTL_DEFAULT
from qwenpaw_data_auth.database import _resolve_url, metadata, users
from qwenpaw_data_auth.password import hash_password, validate_password_strength, verify_password


@pytest.mark.parametrize("kind", ["access", "refresh", "platform", "code"])
def test_token_round_trip_and_rotation(kind):
    issuer = TokenIssuer("old-key")
    token = {"access": lambda: issuer.issue_access("u", "t", "w", "member"),
             "refresh": lambda: issuer.issue_refresh("u", "t"),
             "platform": lambda: issuer.issue_platform_access("u"),
             "code": lambda: issuer.issue_authorization_code("u", "http://localhost/callback")}[kind]()
    payload = TokenVerifier(["new-key", "old-key"]).verify(token)
    assert payload["sub"] == "u"
    assert payload["iat"] < payload["exp"]
    assert "jti" in payload
    with pytest.raises(TokenVerifyError):
        TokenVerifier(["wrong-key"]).verify(token)
    with pytest.raises(TokenVerifyError):
        TokenVerifier(["old-key"]).verify(token + "0")


@pytest.mark.parametrize("token", ["", "not-a-token", "a.b.c", "é.é", ".", "x" * 17000])
def test_invalid_token_errors_are_normalized(token):
    with pytest.raises(TokenVerifyError):
        TokenVerifier(["key"]).verify(token)


@pytest.mark.parametrize("claims", [
    {"exp": None}, {"exp": "forever"}, {"exp": float("nan")}, {"exp": float("inf")},
    {"exp": 0}, {"iat": True}, {"iat": time.time() + 9999}, {"typ": "unknown"}, {"sub": ""}, {"jti": []},
])
def test_signed_malformed_payload_rejected(claims):
    issuer = TokenIssuer("key")
    payload = TokenVerifier(["key"]).verify(issuer.issue_access("u", "t", "w", "member"))
    with pytest.raises(TokenVerifyError):
        TokenVerifier(["key"]).verify(issuer._issue({**payload, **claims}))


def test_signed_non_object_and_expired_tokens():
    from qwenpaw_data_auth.token import _sign
    encoded = base64.urlsafe_b64encode(json.dumps([]).encode()).decode().rstrip("=")
    with pytest.raises(TokenVerifyError):
        TokenVerifier(["key"]).verify(f"{encoded}.{_sign(encoded, 'key')}")
    issuer = TokenIssuer("key")
    payload = {"sub": "u", "iat": time.time() - 60, "exp": time.time() - 1, "typ": "access", "jti": "id"}
    with pytest.raises(TokenVerifyError, match="expired"):
        TokenVerifier(["key"]).verify(issuer._issue(payload))
    with pytest.raises(ValueError):
        TokenIssuer("").issue_access("u", "t", "w", "member")


def test_password_hash_strength_and_byte_limits():
    value = "Strong!Pass9"
    first, second = hash_password(value), hash_password(value)
    assert first != second and "$12$" in first
    assert verify_password(value, first)
    assert not verify_password("Wrong!Pass9", first)
    assert not verify_password(value, "invalid-hash")
    assert not verify_password("x" * 73, first)
    assert not validate_password_strength(value)
    assert validate_password_strength("password")
    assert validate_password_strength("密" * 30 + "Az9!")
    with pytest.raises(ValueError):
        hash_password("x" * 73)


def test_principal_and_permission_contracts():
    assert DEFAULT_PRINCIPAL.user_id == "__default__"
    assert DEFAULT_PRINCIPAL.tenant_id == DEFAULT_PRINCIPAL.active_workspace_id == "default"
    assert DEFAULT_PRINCIPAL.is_tenant_admin
    assert Principal("u", "t", role="ws_admin").is_admin
    assert not Principal("u", "t").is_admin
    assert check_permission("member", "mg:read")
    assert not check_permission("member", "mg:write")
    assert check_permission("ws_admin", "mg:write")
    assert not check_permission("ws_admin", "ops:logs")
    assert not check_permission("unknown", "mg:read")


@pytest.mark.usefixtures("local_mode")
def test_explicit_mode_and_env_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "new-key/,old-key")
    cfg = AuthConfig.from_env()
    assert cfg.secret_keys == ["new-key/", "old-key"]
    assert cfg.local_mode and not cfg.auth_enabled and not cfg.auth_url
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    monkeypatch.setenv("QWENPAW_DATA_ACCESS_TOKEN_TTL", "999999999")
    assert AuthConfig.from_env().auth_enabled
    assert AuthConfig.from_env().access_token_ttl == REFRESH_TOKEN_TTL_DEFAULT
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "invalid")
    with pytest.raises(ValueError):
        AuthConfig.from_env()
    assert "old-key" not in repr(cfg)
    assert _resolve_url("relative.db")[1] == str(tmp_path / "runtime" / "relative.db")
    assert _resolve_url("postgresql+asyncpg://user:pass@db.example/auth")[0].startswith("postgresql+psycopg://")
    assert _resolve_url("sqlite+aiosqlite:///:memory:")[1] == ":memory:"


def test_public_schema_and_postgres_ddl():
    assert "password_hash" in users.c
    assert len(metadata.tables) == 10
    ddl = str(CreateTable(users).compile(dialect=postgresql.dialect()))
    assert "to_char" in ddl
    assert "orientation" not in ddl


@pytest.mark.parametrize("hologres", [False, True])
def test_schema_uses_configured_backend_and_serializes_postgres_ddl(monkeypatch, hologres):
    from unittest.mock import MagicMock
    from qwenpaw_data_auth import database

    monkeypatch.setenv("QWENPAW_DATA_DB_URL", "postgresql://db.example/auth" if hologres else "postgresql://instance.hologres.aliyuncs.com/auth")
    engine = MagicMock()
    engine.dialect.name = "postgresql"
    engine.url.host = "instance.hologres.aliyuncs.com" if hologres else "db.example"
    monkeypatch.setattr(database, "_get_engine", lambda _: engine)
    conn = (engine.connect.return_value.execution_options.return_value.__enter__.return_value
            if hologres else engine.begin.return_value.__enter__.return_value)
    events = []
    conn.exec_driver_sql.side_effect = lambda sql: events.append(("lock", sql))

    def create_all(bind):
        assert bind is (engine if hologres else conn)
        ddl = str(CreateTable(users).compile(dialect=postgresql.dialect()))
        assert ("orientation" in ddl) is hologres
        events.append(("create", None))

    def migrate(bind, sqlite):
        assert bind is conn and sqlite is False
        events.append(("migrate", None))

    monkeypatch.setattr(database.metadata, "create_all", create_all)
    monkeypatch.setattr(database, "_migrate_columns", migrate)
    database.init_schema("postgresql://configured/auth")
    assert [event for event, _ in events] == (["create", "migrate"] if hologres else ["lock", "create", "migrate"])
    if not hologres:
        assert events[0][1] == "SELECT pg_advisory_xact_lock(716234019)"
    assert database._target_is_hologres() is not hologres

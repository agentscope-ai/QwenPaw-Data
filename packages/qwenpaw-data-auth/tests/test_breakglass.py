from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from qwenpaw_data_auth.breakglass import (
    consume_raw_cypher_grant,
    issue_raw_cypher_grant,
    raw_query_digest,
    revoke_raw_cypher_grant,
)
from qwenpaw_data_auth.database import get_db, init_schema


def _dsn(tmp_path) -> str:
    dsn = f"sqlite:///{tmp_path / 'control.db'}"
    init_schema(dsn)
    with get_db(dsn) as conn:
        conn.execute(
            "INSERT INTO platform_admins (id, username, password_hash) VALUES ('p1', 'platform', 'x')"
        )
        conn.execute(
            "INSERT INTO tenants (id, name, neo4j_database) VALUES ('t1', 'Tenant', 'neo4j')"
        )
        conn.execute(
            "INSERT INTO workspaces (id, tenant_id, name, instance_status) VALUES ('w1', 't1', 'WS', 'ready')"
        )
        conn.execute(
            "INSERT INTO users (id, tenant_id, username) VALUES ('u1', 't1', 'admin')"
        )
        conn.commit()
    return dsn


def test_grant_is_query_bound_and_single_use(tmp_path) -> None:
    dsn = _dsn(tmp_path)
    digest = raw_query_digest("MATCH (n) RETURN n", {"limit": 1})
    token, grant = issue_raw_cypher_grant(
        dsn,
        platform_admin_id="p1",
        actor_user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        query_digest=digest,
        ttl_seconds=60,
    )
    assert grant["ttl_seconds"] == 60
    assert (
        consume_raw_cypher_grant(
            dsn,
            token=token,
            actor_user_id="u1",
            tenant_id="t1",
            workspace_id="w1",
            query_digest=raw_query_digest("MATCH (x) RETURN x", {}),
        )
        is None
    )
    consumed = consume_raw_cypher_grant(
        dsn,
        token=token,
        actor_user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        query_digest=digest,
    )
    assert consumed and consumed["id"] == grant["id"]
    assert (
        consume_raw_cypher_grant(
            dsn,
            token=token,
            actor_user_id="u1",
            tenant_id="t1",
            workspace_id="w1",
            query_digest=digest,
        )
        is None
    )


def test_platform_operator_can_revoke_unused_grant(tmp_path) -> None:
    dsn = _dsn(tmp_path)
    digest = raw_query_digest("RETURN 1", {})
    token, grant = issue_raw_cypher_grant(
        dsn,
        platform_admin_id="p1",
        actor_user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        query_digest=digest,
        ttl_seconds=60,
    )
    assert revoke_raw_cypher_grant(dsn, grant_id=grant["id"], platform_admin_id="p1")
    assert (
        consume_raw_cypher_grant(
            dsn,
            token=token,
            actor_user_id="u1",
            tenant_id="t1",
            workspace_id="w1",
            query_digest=digest,
        )
        is None
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor_user_id", "u2"),
        ("tenant_id", "t2"),
        ("workspace_id", "w2"),
        ("query_digest", "different"),
    ],
)
def test_grant_rejects_each_scope_mismatch(tmp_path, field: str, value: str) -> None:
    dsn = _dsn(tmp_path)
    digest = raw_query_digest("RETURN $value", {"value": 1})
    token, _ = issue_raw_cypher_grant(
        dsn,
        platform_admin_id="p1",
        actor_user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        query_digest=digest,
        ttl_seconds=60,
    )
    scope = {
        "actor_user_id": "u1",
        "tenant_id": "t1",
        "workspace_id": "w1",
        "query_digest": digest,
    }
    scope[field] = value
    assert consume_raw_cypher_grant(dsn, token=token, **scope) is None


def test_grant_ttl_is_clamped_and_expired_grant_is_rejected(tmp_path) -> None:
    dsn = _dsn(tmp_path)
    digest = raw_query_digest("RETURN 1", {})
    token, grant = issue_raw_cypher_grant(
        dsn,
        platform_admin_id="p1",
        actor_user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        query_digest=digest,
        ttl_seconds=1,
    )
    assert grant["ttl_seconds"] == 30
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE raw_cypher_grants SET expires_at = '2000-01-01T00:00:00Z' "
            "WHERE id = ?",
            (grant["id"],),
        )
        conn.commit()
    assert (
        consume_raw_cypher_grant(
            dsn,
            token=token,
            actor_user_id="u1",
            tenant_id="t1",
            workspace_id="w1",
            query_digest=digest,
        )
        is None
    )


def test_query_digest_is_canonical_for_param_key_order() -> None:
    first = raw_query_digest("RETURN $a, $b", {"a": 1, "b": {"x": 2, "y": 3}})
    second = raw_query_digest("RETURN $a, $b", {"b": {"y": 3, "x": 2}, "a": 1})
    assert first == second
    assert raw_query_digest("RETURN $a, $b ", {"a": 1, "b": {"x": 2, "y": 3}}) != first


def test_only_one_concurrent_consumer_succeeds(tmp_path) -> None:
    dsn = _dsn(tmp_path)
    digest = raw_query_digest("RETURN 1", {})
    token, _ = issue_raw_cypher_grant(
        dsn,
        platform_admin_id="p1",
        actor_user_id="u1",
        tenant_id="t1",
        workspace_id="w1",
        query_digest=digest,
        ttl_seconds=60,
    )

    def consume():
        return consume_raw_cypher_grant(
            dsn,
            token=token,
            actor_user_id="u1",
            tenant_id="t1",
            workspace_id="w1",
            query_digest=digest,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: consume(), range(2)))
    assert sum(result is not None for result in results) == 1

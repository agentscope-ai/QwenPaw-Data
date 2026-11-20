"""One-time, platform-issued authorization for Raw Cypher."""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, insert, select, update

from qwenpaw_data_auth.database import (
    get_db,
    new_id,
    query_one,
    raw_cypher_grants,
    utcnow_iso,
)


def raw_query_digest(cypher: str, params: dict[str, Any]) -> str:
    payload = json.dumps(
        {"cypher": cypher, "params": params},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_raw_cypher_grant(
    dsn: str,
    *,
    platform_admin_id: str,
    actor_user_id: str,
    tenant_id: str,
    workspace_id: str,
    query_digest: str,
    ttl_seconds: int,
) -> tuple[str, dict[str, Any]]:
    ttl = max(30, min(int(ttl_seconds), 900))
    token = secrets.token_urlsafe(32)
    grant_id = new_id()
    expires_at = (
        (datetime.now(timezone.utc) + timedelta(seconds=ttl))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    with get_db(dsn) as conn:
        conn.execute(
            insert(raw_cypher_grants).values(
                id=grant_id,
                token_hash=_token_hash(token),
                platform_admin_id=platform_admin_id,
                actor_user_id=actor_user_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                query_digest=query_digest,
                expires_at=expires_at,
            )
        )
        conn.commit()
    return token, {"id": grant_id, "expires_at": expires_at, "ttl_seconds": ttl}


def consume_raw_cypher_grant(
    dsn: str,
    *,
    token: str,
    actor_user_id: str,
    tenant_id: str,
    workspace_id: str,
    query_digest: str,
) -> dict[str, Any] | None:
    """Atomically validate and consume a grant; invalid grants are indistinguishable."""
    if not token:
        return None
    now = utcnow_iso()
    with get_db(dsn) as conn:
        grant = query_one(
            conn,
            select(raw_cypher_grants).where(
                and_(
                    raw_cypher_grants.c.token_hash == _token_hash(token),
                    raw_cypher_grants.c.actor_user_id == actor_user_id,
                    raw_cypher_grants.c.tenant_id == tenant_id,
                    raw_cypher_grants.c.workspace_id == workspace_id,
                    raw_cypher_grants.c.query_digest == query_digest,
                    raw_cypher_grants.c.consumed_at.is_(None),
                    raw_cypher_grants.c.revoked_at.is_(None),
                    raw_cypher_grants.c.expires_at > now,
                )
            ),
        )
        if not grant:
            conn.rollback()
            return None
        updated = conn.execute(
            update(raw_cypher_grants)
            .where(
                and_(
                    raw_cypher_grants.c.id == grant["id"],
                    raw_cypher_grants.c.consumed_at.is_(None),
                    raw_cypher_grants.c.revoked_at.is_(None),
                )
            )
            .values(consumed_at=now)
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return grant


def revoke_raw_cypher_grant(dsn: str, *, grant_id: str, platform_admin_id: str) -> bool:
    with get_db(dsn) as conn:
        updated = conn.execute(
            update(raw_cypher_grants)
            .where(
                and_(
                    raw_cypher_grants.c.id == grant_id,
                    raw_cypher_grants.c.platform_admin_id == platform_admin_id,
                    raw_cypher_grants.c.consumed_at.is_(None),
                    raw_cypher_grants.c.revoked_at.is_(None),
                )
            )
            .values(revoked_at=utcnow_iso())
        )
        conn.commit()
        return updated.rowcount == 1

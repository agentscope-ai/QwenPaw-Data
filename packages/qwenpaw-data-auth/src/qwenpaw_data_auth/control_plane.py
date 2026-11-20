"""
ControlPlaneReader — read-only access to the control-plane database.

Used by CM (and optionally Host) to resolve tenant → neo4j_database mappings
and to sync token-revocation timestamps without calling the Auth HTTP API.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import and_, case, or_, select

from qwenpaw_data_auth.database import (
    get_db,
    query_one,
    query_all,
    role_bindings,
    tenants,
    users,
    workspaces,
)


class ControlPlaneReader:
    def __init__(self, dsn: str = ""):
        self._dsn = dsn

    def get_tenant(self, tenant_id: str) -> dict | None:
        with get_db(self._dsn) as conn:
            return query_one(
                conn,
                select(tenants).where(
                    and_(tenants.c.id == tenant_id, tenants.c.status == "active")
                ),
            )

    def get_workspace(self, workspace_id: str) -> dict | None:
        with get_db(self._dsn) as conn:
            return query_one(
                conn,
                select(workspaces).where(
                    and_(
                        workspaces.c.id == workspace_id,
                        workspaces.c.status == "active",
                    )
                ),
            )

    def get_user_workspace_ids(self, user_id: str, tenant_id: str) -> list[str]:
        with get_db(self._dsn) as conn:
            rows = query_all(
                conn,
                select(role_bindings.c.scope_id)
                .distinct()
                .select_from(
                    role_bindings.join(
                        users, users.c.id == role_bindings.c.user_id
                    ).join(workspaces, workspaces.c.id == role_bindings.c.scope_id)
                )
                .where(
                    and_(
                        role_bindings.c.user_id == user_id,
                        users.c.tenant_id == tenant_id,
                        role_bindings.c.scope_type == "workspace",
                        workspaces.c.tenant_id == users.c.tenant_id,
                        workspaces.c.status == "active",
                    )
                ),
            )
            ws_ids = [r["scope_id"] for r in rows]

            user = query_one(conn, select(users).where(users.c.id == user_id))
            if user:
                bindings = query_all(
                    conn,
                    select(role_bindings).where(
                        and_(
                            role_bindings.c.user_id == user_id,
                            role_bindings.c.role == "tenant_admin",
                            role_bindings.c.scope_type == "tenant",
                            role_bindings.c.scope_id == tenant_id,
                        )
                    ),
                )
                if bindings:
                    all_ws = query_all(
                        conn,
                        select(workspaces.c.id).where(
                            and_(
                                workspaces.c.tenant_id == tenant_id,
                                workspaces.c.status == "active",
                            )
                        ),
                    )
                    ws_ids = [w["id"] for w in all_ws]

            return ws_ids

    def get_user_role(self, user_id: str) -> str:
        role_rank = case(
            (role_bindings.c.role == "tenant_admin", 3),
            (role_bindings.c.role == "ws_admin", 2),
            (role_bindings.c.role == "member", 1),
            else_=0,
        )
        with get_db(self._dsn) as conn:
            binding = query_one(
                conn,
                select(role_bindings.c.role)
                .select_from(
                    role_bindings.join(
                        users, users.c.id == role_bindings.c.user_id
                    ).outerjoin(
                        workspaces,
                        and_(
                            role_bindings.c.scope_type == "workspace",
                            role_bindings.c.scope_id == workspaces.c.id,
                        ),
                    )
                )
                .where(
                    and_(
                        role_bindings.c.user_id == user_id,
                        or_(
                            and_(
                                role_bindings.c.role == "tenant_admin",
                                role_bindings.c.scope_type == "tenant",
                                role_bindings.c.scope_id == users.c.tenant_id,
                            ),
                            and_(
                                role_bindings.c.role.in_(["ws_admin", "member"]),
                                role_bindings.c.scope_type == "workspace",
                                workspaces.c.tenant_id == users.c.tenant_id,
                                workspaces.c.status == "active",
                            ),
                        ),
                    )
                )
                .order_by(role_rank.desc())
                .limit(1),
            )
            return binding["role"] if binding else "member"

    def get_user_revocation_timestamps(
        self, since: Optional[datetime] = None
    ) -> dict[str, float]:
        with get_db(self._dsn) as conn:
            stmt = select(users.c.id, users.c.token_revoked_before).where(
                users.c.token_revoked_before.is_not(None)
            )
            if since:
                stmt = stmt.where(users.c.token_revoked_before > since.isoformat())
            rows = query_all(conn, stmt)

            result: dict[str, float] = {}
            for r in rows:
                ts_str = r["token_revoked_before"]
                try:
                    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    result[r["id"]] = dt.timestamp()
                except (ValueError, AttributeError):
                    pass
            return result

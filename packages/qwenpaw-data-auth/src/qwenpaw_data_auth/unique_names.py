"""Application-level unique names (Hologres cannot enforce UNIQUE)."""

from __future__ import annotations

from sqlalchemy import and_, func, select

from qwenpaw_data_auth.database import query_one, tenants, users, workspaces


def tenant_name_taken(conn, name: str, *, exclude_id: str | None = None) -> bool:
    key = name.strip().lower()
    if not key:
        return False
    stmt = select(tenants.c.id).where(func.lower(tenants.c.name) == key)
    if exclude_id:
        stmt = stmt.where(tenants.c.id != exclude_id)
    return query_one(conn, stmt) is not None


def workspace_name_taken(
    conn,
    tenant_id: str,
    name: str,
    *,
    exclude_id: str | None = None,
) -> bool:
    key = name.strip().lower()
    if not key:
        return False
    cond = [
        workspaces.c.tenant_id == tenant_id,
        func.lower(workspaces.c.name) == key,
    ]
    if exclude_id:
        cond.append(workspaces.c.id != exclude_id)
    return query_one(conn, select(workspaces.c.id).where(and_(*cond))) is not None


def username_conflict(
    conn, tenant_id: str, username: str, *, has_password: bool
) -> str:
    """Empty when ``username`` is free for a new seat, else the 409 detail.

    A user row is one tenant seat, so names only have to be unique inside one
    tenant. Rows that carry a password must also be globally unique because
    local sign-in looks an account up by username alone, with no tenant to
    disambiguate it.
    """
    if query_one(
        conn,
        select(users.c.id).where(
            and_(users.c.tenant_id == tenant_id, users.c.username == username)
        ),
    ):
        return "Username already exists"
    if has_password and query_one(
        conn,
        select(users.c.id).where(
            and_(users.c.username == username, users.c.password_hash != "")
        ),
    ):
        return "Username already taken by a password account"
    return ""

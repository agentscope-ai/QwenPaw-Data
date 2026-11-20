"""
User management API.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, exists, func, insert, select, update
from starlette.background import BackgroundTask

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.authorization import (
    load_target_user,
    require_admin,
)
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.control_plane import ControlPlaneReader
from qwenpaw_data_auth.database import (
    get_db,
    new_id,
    query_all,
    query_one,
    role_bindings,
    tenants,
    users,
    utcnow_iso,
    workspaces,
)
from qwenpaw_data_auth.mg_sync import sync_user_workspaces
from qwenpaw_data_auth.password import hash_password, validate_password_strength
from qwenpaw_data_auth.unique_names import username_conflict

router = APIRouter(prefix="/api/users", tags=["users"])

# Mirrors models.UserCreate.username so the rejection message matches reality.
USERNAME_RE = re.compile(r"[A-Za-z0-9_\-]{2,32}")


def _user_response(
    conn, user_id: str, visible_workspace_ids: set[str] | None = None
) -> dict | None:
    user = query_one(
        conn,
        select(
            users.c.id,
            users.c.tenant_id,
            users.c.username,
            users.c.display_name,
            users.c.status,
            users.c.created_at,
        ).where(users.c.id == user_id),
    )
    if not user:
        return None
    # 花名 for display; falls back to username when absent.
    user["display_name"] = user.get("display_name") or user["username"]
    bindings = query_all(
        conn,
        select(
            role_bindings.c.id,
            role_bindings.c.role,
            role_bindings.c.scope_type,
            role_bindings.c.scope_id,
            func.coalesce(
                workspaces.c.name, tenants.c.name, role_bindings.c.scope_id
            ).label("scope_name"),
        )
        .select_from(
            role_bindings.outerjoin(
                workspaces,
                and_(
                    role_bindings.c.scope_type == "workspace",
                    role_bindings.c.scope_id == workspaces.c.id,
                ),
            ).outerjoin(
                tenants,
                and_(
                    role_bindings.c.scope_type == "tenant",
                    role_bindings.c.scope_id == tenants.c.id,
                ),
            )
        )
        .where(role_bindings.c.user_id == user_id),
    )
    if visible_workspace_ids is not None:
        bindings = [
            binding
            for binding in bindings
            if binding["scope_type"] == "workspace"
            and binding["scope_id"] in visible_workspace_ids
        ]
    user["role_bindings"] = bindings
    return user


@router.get("")
async def list_users(request: Request):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    workspace_id = request.query_params.get("workspace_id")

    with get_db(cfg.control_plane_dsn) as conn:
        tenant_admin_count_row = query_one(
            conn,
            select(func.count(func.distinct(role_bindings.c.user_id)).label("cnt"))
            .select_from(
                role_bindings.join(users, users.c.id == role_bindings.c.user_id)
            )
            .where(
                and_(
                    role_bindings.c.role == "tenant_admin",
                    role_bindings.c.scope_type == "tenant",
                    role_bindings.c.scope_id == ctx.tenant_id,
                    users.c.status == "active",
                )
            ),
        )
        active_tenant_admin_count = (
            int(tenant_admin_count_row["cnt"]) if tenant_admin_count_row else 0
        )
        if ctx.role == "ws_admin":
            if workspace_id and workspace_id not in ctx.admin_workspace_ids:
                return JSONResponse(
                    status_code=403, content={"detail": "No access to this workspace"}
                )
            visible_ws = (
                {workspace_id} if workspace_id else set(ctx.admin_workspace_ids)
            )
            if not visible_ws:
                return {"users": []}
            s = role_bindings.alias("s")
            users_raw = query_all(
                conn,
                select(
                    users.c.id,
                    users.c.tenant_id,
                    users.c.username,
                    users.c.status,
                    users.c.created_at,
                )
                .distinct()
                .select_from(
                    users.join(role_bindings, role_bindings.c.user_id == users.c.id)
                )
                .where(
                    and_(
                        users.c.tenant_id == ctx.tenant_id,
                        role_bindings.c.scope_type == "workspace",
                        role_bindings.c.scope_id.in_(list(visible_ws)),
                        ~exists(
                            select(1)
                            .select_from(s)
                            .where(
                                and_(
                                    s.c.user_id == users.c.id,
                                    s.c.role == "tenant_admin",
                                )
                            )
                        ),
                    )
                )
                .order_by(users.c.created_at),
            )
        elif workspace_id:
            workspace = query_one(
                conn,
                select(workspaces.c.id).where(
                    and_(
                        workspaces.c.id == workspace_id,
                        workspaces.c.tenant_id == ctx.tenant_id,
                    )
                ),
            )
            if not workspace:
                return JSONResponse(
                    status_code=404, content={"detail": "Workspace not found"}
                )
            user_ids_rows = query_all(
                conn,
                select(role_bindings.c.user_id)
                .distinct()
                .where(
                    and_(
                        role_bindings.c.scope_type == "workspace",
                        role_bindings.c.scope_id == workspace_id,
                    )
                ),
            )
            user_ids = [r["user_id"] for r in user_ids_rows]
            if not user_ids:
                return {"users": []}
            users_raw = query_all(
                conn,
                select(
                    users.c.id,
                    users.c.tenant_id,
                    users.c.username,
                    users.c.status,
                    users.c.created_at,
                ).where(users.c.id.in_(user_ids)),
            )
        else:
            users_raw = query_all(
                conn,
                select(
                    users.c.id,
                    users.c.tenant_id,
                    users.c.username,
                    users.c.status,
                    users.c.created_at,
                )
                .where(users.c.tenant_id == ctx.tenant_id)
                .order_by(users.c.created_at),
            )

        results = []
        for u in users_raw:
            full = _user_response(
                conn,
                u["id"],
                set(ctx.admin_workspace_ids) if ctx.role == "ws_admin" else None,
            )
            if full:
                is_self = full["id"] == ctx.user_id
                is_active_tenant_admin = full["status"] == "active" and any(
                    binding["role"] == "tenant_admin"
                    and binding["scope_type"] == "tenant"
                    for binding in full["role_bindings"]
                )
                deny_reason = None
                if is_self:
                    deny_reason = "CANNOT_MANAGE_SELF"
                elif is_active_tenant_admin and active_tenant_admin_count <= 1:
                    deny_reason = "LAST_TENANT_ADMIN"
                full["can_disable"] = deny_reason is None
                full["deny_reason"] = deny_reason
                results.append(full)

    return {"users": results}


def _ensure_role_binding(
    conn, user_id: str, role: str, scope_type: str, scope_id: str
) -> None:
    existing = query_one(
        conn,
        """
        SELECT id FROM role_bindings
        WHERE user_id = ? AND role = ? AND scope_type = ? AND scope_id = ?
        """,
        (user_id, role, scope_type, scope_id),
    )
    if existing:
        return
    conn.execute(
        """
        INSERT INTO role_bindings (id, user_id, role, scope_type, scope_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        (new_id(), user_id, role, scope_type, scope_id),
    )


def _default_workspace_id(conn, tenant_id: str) -> str | None:
    row = query_one(
        conn,
        """
        SELECT id FROM workspaces
        WHERE tenant_id = ? AND status = 'active'
        ORDER BY created_at, id
        """,
        (tenant_id,),
    )
    return row["id"] if row else None


@router.post("")
async def create_user(request: Request):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not USERNAME_RE.fullmatch(username):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid username (2-32 chars, alphanumeric/_/-)"},
        )

    violations = validate_password_strength(password)
    if violations:
        return JSONResponse(
            status_code=400, content={"detail": "; ".join(violations)}
        )
    pwd_hash = hash_password(password)

    tenant_id = ctx.tenant_id
    target_role = str(body.get("role", "member"))
    if target_role not in {"member", "ws_admin", "tenant_admin"}:
        return JSONResponse(status_code=400, content={"detail": "Invalid role"})
    if ctx.active_workspace_role == "ws_admin" and target_role != "member":
        return JSONResponse(
            status_code=403,
            content={"detail": "ws_admin can only create member users"},
        )
    if target_role == "tenant_admin":
        scope_type, scope_id = "tenant", tenant_id
    else:
        workspace_id = str(body.get("workspace_id", "")).strip()
        if not workspace_id:
            workspace_id = ctx.active_workspace_id or (
                next(iter(ctx.workspace_ids)) if len(ctx.workspace_ids) == 1 else ""
            )
        if not workspace_id:
            return JSONResponse(
                status_code=400, content={"detail": "workspace_id is required"}
            )
        if (
            workspace_id not in ctx.admin_workspace_ids
            if ctx.active_workspace_role == "ws_admin"
            else workspace_id not in ctx.workspace_ids
        ):
            return JSONResponse(
                status_code=403, content={"detail": "No access to this workspace"}
            )
        scope_type, scope_id = "workspace", workspace_id
    user_id = new_id()
    reader = ControlPlaneReader(cfg.control_plane_dsn)
    previous = set()

    with get_db(cfg.control_plane_dsn) as conn:
        # Look up an existing seat in this tenant only.
        sync_workspace_id = (
            scope_id
            if scope_type == "workspace"
            else _default_workspace_id(conn, tenant_id)
        )
        existing = query_one(
            conn,
            select(users).where(
                and_(
                    users.c.username == username,
                    users.c.tenant_id == tenant_id,
                )
            ),
        )
        if existing:
            return JSONResponse(
                status_code=409, content={"detail": "Username already exists"}
            )
        conflict = username_conflict(
            conn, tenant_id, username, has_password=bool(pwd_hash)
        )
        if conflict:
            return JSONResponse(status_code=409, content={"detail": conflict})
        try:
            conn.execute(
                insert(users).values(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=username,
                    password_hash=pwd_hash,
                    password_changed_at=utcnow_iso() if pwd_hash else None,
                )
            )
            _ensure_role_binding(conn, user_id, target_role, scope_type, scope_id)
            if target_role == "tenant_admin" and sync_workspace_id:
                _ensure_role_binding(
                    conn, user_id, "member", "workspace", sync_workspace_id
                )
            conn.commit()
        except Exception:
            conn.rollback()
            return JSONResponse(
                status_code=409, content={"detail": "Unable to invite user"}
            )

    log_event(
        "register",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="user",
        target_id=user_id,
        dsn=cfg.control_plane_dsn,
    )

    with get_db(cfg.control_plane_dsn) as conn:
        user = _user_response(conn, user_id)
    joined = set(reader.get_user_workspace_ids(user_id, tenant_id)) - previous
    background = BackgroundTask(sync_user_workspaces, cfg, tenant_id, user_id, joined)
    return JSONResponse(status_code=201, content={"user": user}, background=background)


@router.put("/{user_id}")
async def update_user(request: Request, user_id: str):
    cfg: AuthConfig = request.app.state.auth_config
    ctx = require_admin(request)
    is_self = ctx.user_id == user_id
    if not is_self:
        load_target_user(request, ctx, user_id)

    body = await request.json()

    if "password" in body:
        violations = validate_password_strength(body["password"])
        if violations:
            return JSONResponse(
                status_code=400, content={"detail": "; ".join(violations)}
            )
        pwd_hash = hash_password(body["password"])
        force_password_change = 0 if is_self else 1
        with get_db(cfg.control_plane_dsn) as conn:
            conn.execute(
                update(users)
                .where(users.c.id == user_id)
                .values(
                    password_hash=pwd_hash,
                    password_changed_at=utcnow_iso(),
                    force_password_change=force_password_change,
                    token_revoked_before=utcnow_iso(),
                )
            )
            conn.commit()

        event = "password_changed" if is_self else "password_reset"
        with get_db(cfg.control_plane_dsn) as conn:
            reset_user = query_one(
                conn, select(users.c.username).where(users.c.id == user_id)
            )
        if reset_user:
            from qwenpaw_data_auth.auth_api import clear_login_failures

            clear_login_failures(reset_user["username"], ctx.tenant_id)
        log_event(
            event,
            actor_id=ctx.user_id,
            actor_ip=request.headers.get("X-Forwarded-For", ""),
            target_type="user",
            target_id=user_id,
            dsn=cfg.control_plane_dsn,
        )

    with get_db(cfg.control_plane_dsn) as conn:
        user = _user_response(conn, user_id)
    if not user:
        return JSONResponse(status_code=404, content={"detail": "User not found"})
    return {"user": user}


@router.post("/{user_id}/disable")
async def disable_user(request: Request, user_id: str):
    ctx = require_admin(request)
    if ctx.user_id == user_id:
        return JSONResponse(
            status_code=400, content={"detail": "Cannot disable yourself"}
        )
    target = load_target_user(request, ctx, user_id)
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        if any(b["role"] == "tenant_admin" for b in target["role_bindings"]):
            count = query_one(
                conn,
                select(func.count(func.distinct(role_bindings.c.user_id)).label("cnt"))
                .select_from(
                    role_bindings.join(users, users.c.id == role_bindings.c.user_id)
                )
                .where(
                    and_(
                        role_bindings.c.role == "tenant_admin",
                        role_bindings.c.scope_type == "tenant",
                        role_bindings.c.scope_id == ctx.tenant_id,
                        users.c.status == "active",
                    )
                ),
            )
            if count and count["cnt"] <= 1:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Cannot disable the last tenant_admin"},
                )
        conn.execute(
            update(users)
            .where(and_(users.c.id == user_id, users.c.tenant_id == ctx.tenant_id))
            .values(status="disabled", token_revoked_before=utcnow_iso())
        )
        conn.commit()
    log_event(
        "user_disabled",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="user",
        target_id=user_id,
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}


@router.post("/{user_id}/enable")
async def enable_user(request: Request, user_id: str):
    ctx = require_admin(request)
    load_target_user(request, ctx, user_id)
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(
            update(users)
            .where(and_(users.c.id == user_id, users.c.tenant_id == ctx.tenant_id))
            .values(status="active", token_revoked_before=utcnow_iso())
        )
        conn.commit()
    log_event(
        "user_enabled",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="user",
        target_id=user_id,
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}

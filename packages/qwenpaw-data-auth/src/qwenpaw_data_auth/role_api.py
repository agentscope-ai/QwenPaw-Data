"""
Role binding management API with R1-R4 validation rules.

R1 — Cannot grant a role higher than your own
R2 — ws_admin can only operate within their own workspaces
R3 — Cannot create/delete your own role bindings
R4 — Cannot delete the last tenant_admin binding
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, delete, exists, func, insert, select, update
from sqlalchemy.exc import IntegrityError
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

router = APIRouter(prefix="/api/roles", tags=["roles"])

ROLE_LEVEL = {"tenant_admin": 3, "ws_admin": 2, "member": 1}


@router.get("/bindings")
async def list_bindings(request: Request):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    workspace_id = request.query_params.get("workspace_id")

    with get_db(cfg.control_plane_dsn) as conn:
        if ctx.role == "ws_admin":
            if workspace_id and workspace_id not in ctx.admin_workspace_ids:
                return JSONResponse(
                    status_code=403, content={"detail": "No access to this workspace"}
                )
            visible_ws = (
                {workspace_id} if workspace_id else set(ctx.admin_workspace_ids)
            )
            if not visible_ws:
                return {"bindings": []}
            s = role_bindings.alias("s")
            rows = query_all(
                conn,
                select(
                    role_bindings,
                    users.c.username,
                    users.c.status.label("user_status"),
                    func.coalesce(workspaces.c.name, role_bindings.c.scope_id).label(
                        "scope_name"
                    ),
                )
                .select_from(
                    role_bindings.join(
                        users, users.c.id == role_bindings.c.user_id
                    ).join(
                        workspaces,
                        and_(
                            role_bindings.c.scope_type == "workspace",
                            role_bindings.c.scope_id == workspaces.c.id,
                        ),
                    )
                )
                .where(
                    and_(
                        users.c.tenant_id == ctx.tenant_id,
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
                .order_by(role_bindings.c.created_at),
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
            rows = query_all(
                conn,
                select(
                    role_bindings,
                    users.c.username,
                    users.c.status.label("user_status"),
                    func.coalesce(
                        workspaces.c.name, tenants.c.name, role_bindings.c.scope_id
                    ).label("scope_name"),
                )
                .select_from(
                    role_bindings.join(users, users.c.id == role_bindings.c.user_id)
                    .outerjoin(
                        workspaces,
                        and_(
                            role_bindings.c.scope_type == "workspace",
                            role_bindings.c.scope_id == workspaces.c.id,
                        ),
                    )
                    .outerjoin(
                        tenants,
                        and_(
                            role_bindings.c.scope_type == "tenant",
                            role_bindings.c.scope_id == tenants.c.id,
                        ),
                    )
                )
                .where(
                    and_(
                        role_bindings.c.scope_type == "workspace",
                        role_bindings.c.scope_id == workspace_id,
                        users.c.tenant_id == ctx.tenant_id,
                    )
                )
                .order_by(role_bindings.c.created_at),
            )
        else:
            rows = query_all(
                conn,
                select(
                    role_bindings,
                    users.c.username,
                    users.c.status.label("user_status"),
                    func.coalesce(
                        workspaces.c.name, tenants.c.name, role_bindings.c.scope_id
                    ).label("scope_name"),
                )
                .select_from(
                    role_bindings.join(users, users.c.id == role_bindings.c.user_id)
                    .outerjoin(
                        workspaces,
                        and_(
                            role_bindings.c.scope_type == "workspace",
                            role_bindings.c.scope_id == workspaces.c.id,
                        ),
                    )
                    .outerjoin(
                        tenants,
                        and_(
                            role_bindings.c.scope_type == "tenant",
                            role_bindings.c.scope_id == tenants.c.id,
                        ),
                    )
                )
                .where(users.c.tenant_id == ctx.tenant_id)
                .order_by(role_bindings.c.created_at),
            )
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
        for row in rows:
            deny_reason = None
            if row["user_id"] == ctx.user_id:
                deny_reason = "CANNOT_MODIFY_SELF"
            elif (
                row["role"] == "tenant_admin"
                and row["user_status"] == "active"
                and active_tenant_admin_count <= 1
            ):
                deny_reason = "LAST_TENANT_ADMIN"
            row["can_delete"] = deny_reason is None
            row["deny_reason"] = deny_reason

    return {"bindings": rows}


@router.post("/bindings")
async def create_binding(request: Request):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    target_user_id = body.get("user_id", "")
    target_role = body.get("role", "")
    scope_type = body.get("scope_type", "")
    scope_id = body.get("scope_id", "")

    if not all([target_user_id, target_role, scope_type, scope_id]):
        return JSONResponse(
            status_code=400,
            content={"detail": "user_id, role, scope_type, scope_id are required"},
        )

    if target_role not in ROLE_LEVEL:
        return JSONResponse(
            status_code=400, content={"detail": f"Invalid role: {target_role}"}
        )

    # R1: Cannot grant a role higher than your own
    if ROLE_LEVEL.get(target_role, 0) > ROLE_LEVEL.get(ctx.active_workspace_role, 0):
        return JSONResponse(
            status_code=403,
            content={"detail": "Cannot grant a role higher than your own"},
        )

    # R3: Cannot create your own role bindings
    if target_user_id == ctx.user_id:
        return JSONResponse(
            status_code=403, content={"detail": "Cannot modify your own role bindings"}
        )

    load_target_user(request, ctx, target_user_id)
    if target_role == "tenant_admin":
        if (
            ctx.active_workspace_role != "tenant_admin"
            or scope_type != "tenant"
            or scope_id != ctx.tenant_id
        ):
            return JSONResponse(
                status_code=403, content={"detail": "Invalid tenant role scope"}
            )
    else:
        if scope_type != "workspace":
            return JSONResponse(
                status_code=400,
                content={"detail": "Workspace role requires workspace scope"},
            )
        with get_db(cfg.control_plane_dsn) as conn:
            workspace = query_one(
                conn,
                select(workspaces.c.id).where(
                    and_(
                        workspaces.c.id == scope_id,
                        workspaces.c.tenant_id == ctx.tenant_id,
                        workspaces.c.status == "active",
                    )
                ),
            )
        if not workspace:
            return JSONResponse(
                status_code=404, content={"detail": "Workspace not found"}
            )
        if (
            ctx.active_workspace_role == "ws_admin"
            and scope_id not in ctx.admin_workspace_ids
        ):
            return JSONResponse(
                status_code=403, content={"detail": "No access to this workspace"}
            )

    binding_id = new_id()
    reader = ControlPlaneReader(cfg.control_plane_dsn)
    previous = set(reader.get_user_workspace_ids(target_user_id, ctx.tenant_id))
    with get_db(cfg.control_plane_dsn) as conn:
        try:
            conn.execute(
                insert(role_bindings).values(
                    id=binding_id,
                    user_id=target_user_id,
                    role=target_role,
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
            )
        except IntegrityError:
            conn.rollback()
            return JSONResponse(
                status_code=409, content={"detail": "Role binding already exists"}
            )
        conn.execute(
            update(users)
            .where(users.c.id == target_user_id)
            .values(token_revoked_before=utcnow_iso())
        )
        conn.commit()

    log_event(
        "role_binding_created",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="role_binding",
        target_id=binding_id,
        detail={
            "user_id": target_user_id,
            "role": target_role,
            "scope": f"{scope_type}:{scope_id}",
        },
        dsn=cfg.control_plane_dsn,
    )

    with get_db(cfg.control_plane_dsn) as conn:
        binding = query_one(
            conn,
            select(
                role_bindings,
                users.c.username,
                func.coalesce(
                    workspaces.c.name, tenants.c.name, role_bindings.c.scope_id
                ).label("scope_name"),
            )
            .select_from(
                role_bindings.join(users, users.c.id == role_bindings.c.user_id)
                .outerjoin(
                    workspaces,
                    and_(
                        role_bindings.c.scope_type == "workspace",
                        role_bindings.c.scope_id == workspaces.c.id,
                    ),
                )
                .outerjoin(
                    tenants,
                    and_(
                        role_bindings.c.scope_type == "tenant",
                        role_bindings.c.scope_id == tenants.c.id,
                    ),
                )
            )
            .where(role_bindings.c.id == binding_id),
        )
    joined = (
        set(reader.get_user_workspace_ids(target_user_id, ctx.tenant_id)) - previous
    )
    return JSONResponse(
        status_code=201,
        content={"binding": binding},
        background=BackgroundTask(
            sync_user_workspaces, cfg, ctx.tenant_id, target_user_id, joined
        ),
    )


@router.delete("/bindings/{binding_id}")
async def delete_binding(request: Request, binding_id: str):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config

    with get_db(cfg.control_plane_dsn) as conn:
        binding = query_one(
            conn, select(role_bindings).where(role_bindings.c.id == binding_id)
        )
        if not binding:
            return JSONResponse(
                status_code=404, content={"detail": "Binding not found"}
            )
        try:
            load_target_user(request, ctx, binding["user_id"])
        except Exception:
            return JSONResponse(
                status_code=404, content={"detail": "Binding not found"}
            )

        # R3: Cannot delete your own role bindings
        if binding["user_id"] == ctx.user_id:
            return JSONResponse(
                status_code=403,
                content={"detail": "Cannot modify your own role bindings"},
            )

        # R1: Cannot remove a binding for a role higher than your own
        if ROLE_LEVEL.get(binding["role"], 0) > ROLE_LEVEL.get(
            ctx.active_workspace_role, 0
        ):
            return JSONResponse(
                status_code=403,
                content={"detail": "Cannot remove a role higher than your own"},
            )

        # R2: ws_admin scope check
        if ctx.active_workspace_role == "ws_admin":
            if (
                binding["scope_type"] != "workspace"
                or binding["scope_id"] not in ctx.admin_workspace_ids
            ):
                return JSONResponse(
                    status_code=403, content={"detail": "No access to this workspace"}
                )

        # R4: Cannot delete the last tenant_admin binding
        if binding["role"] == "tenant_admin":
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
                    content={"detail": "Cannot delete the last tenant_admin binding"},
                )

        conn.execute(delete(role_bindings).where(role_bindings.c.id == binding_id))
        conn.execute(
            update(users)
            .where(users.c.id == binding["user_id"])
            .values(token_revoked_before=utcnow_iso())
        )
        conn.commit()

    log_event(
        "role_binding_deleted",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="role_binding",
        target_id=binding_id,
        detail={"user_id": binding["user_id"], "role": binding["role"]},
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}

"""Workspace management API."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.provision_backend import (
    configured_provision_backends,
    parse_provision_backend,
    queue_workspace_values,
)
from qwenpaw_data_auth.unique_names import workspace_name_taken
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.authorization import (
    authorize_request,
    require_admin,
    require_tenant_admin,
    require_workspace_access,
)
from sqlalchemy import and_, func, insert, select, update

from qwenpaw_data_auth.database import (
    get_db,
    new_id,
    query_one,
    query_all,
    role_bindings,
    workspaces,
)

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])


@router.get("")
async def list_workspaces(request: Request):
    cfg: AuthConfig = request.app.state.auth_config
    ctx = authorize_request(request)

    with get_db(cfg.control_plane_dsn) as conn:
        if ctx.role == "tenant_admin":
            rows = query_all(
                conn,
                select(workspaces)
                .where(workspaces.c.tenant_id == ctx.tenant_id)
                .order_by(workspaces.c.created_at),
            )
        else:
            if not ctx.workspace_ids:
                return {
                    "workspaces": [],
                    "provision_backends": configured_provision_backends(),
                }
            rows = query_all(
                conn,
                select(workspaces)
                .where(
                    and_(
                        workspaces.c.tenant_id == ctx.tenant_id,
                        workspaces.c.id.in_(list(ctx.workspace_ids)),
                    )
                )
                .order_by(workspaces.c.created_at),
            )

    for row in rows:
        member_count = 0
        with get_db(cfg.control_plane_dsn) as conn:
            cnt = query_one(
                conn,
                select(
                    func.count(func.distinct(role_bindings.c.user_id)).label("cnt")
                ).where(
                    and_(
                        role_bindings.c.scope_type == "workspace",
                        role_bindings.c.scope_id == row["id"],
                    )
                ),
            )
            if cnt:
                member_count = cnt["cnt"]
        row["member_count"] = member_count
        row["can_manage"] = ctx.can_manage_workspace(row["id"])

    return {
        "workspaces": rows,
        "provision_backends": configured_provision_backends(),
    }


@router.post("")
async def create_workspace(request: Request):
    ctx = require_tenant_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    name = body.get("name", "").strip()
    description = body.get("description", "")

    if not name:
        return JSONResponse(status_code=400, content={"detail": "name is required"})

    tenant_id = ctx.tenant_id
    ws_id = new_id()

    with get_db(cfg.control_plane_dsn) as conn:
        if workspace_name_taken(conn, tenant_id, name):
            return JSONResponse(
                status_code=409,
                content={"detail": "Workspace name already exists in this tenant"},
            )
        conn.execute(
            insert(workspaces).values(
                id=ws_id,
                tenant_id=tenant_id,
                name=name,
                description=description,
                status="provisioning",
                instance_status="pending",
            )
        )
        conn.commit()

    log_event(
        "workspace_created",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="workspace",
        target_id=ws_id,
        dsn=cfg.control_plane_dsn,
    )

    with get_db(cfg.control_plane_dsn) as conn:
        ws = query_one(conn, select(workspaces).where(workspaces.c.id == ws_id))
    return JSONResponse(status_code=201, content={"workspace": ws})


@router.post("/{workspace_id}/provision")
async def provision_workspace(request: Request, workspace_id: str):
    """Queue Neo4j instance provisioning for one workspace (tenant_admin).

    Mirrors ``POST /api/platform/tenants/{id}/provision`` but is tenant-scoped:
    the tenant_admin starts the resource instance of a workspace they created.
    Auth only writes queue state; the trusted provisioner process owns Docker.
    """
    ctx = require_tenant_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    if cfg.env != "development":
        return JSONResponse(
            status_code=403,
            content={"detail": "Local provisioning is only available in development"},
        )
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — empty body uses GRAPH_BACKEND
        body = {}
    backend = parse_provision_backend(body)
    if isinstance(backend, JSONResponse):
        return backend

    with get_db(cfg.control_plane_dsn) as conn:
        workspace = query_one(
            conn,
            select(workspaces.c.id, workspaces.c.instance_status).where(
                and_(
                    workspaces.c.id == workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                )
            ),
        )
        if not workspace:
            conn.rollback()
            return JSONResponse(
                status_code=404, content={"detail": "Workspace not found"}
            )
        if workspace["instance_status"] not in {"pending", "failed"}:
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={
                    "detail": (
                        "Workspace provisioning cannot be queued from state "
                        f"{workspace['instance_status']}"
                    )
                },
            )
        updated = conn.execute(
            update(workspaces)
            .where(
                and_(
                    workspaces.c.id == workspace_id,
                    workspaces.c.instance_status.in_(["pending", "failed"]),
                )
            )
            .values(**queue_workspace_values(backend))
        )
        if updated.rowcount != 1:
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Workspace provisioning state changed; reload and retry"
                },
            )
        conn.commit()

    log_event(
        "workspace_provisioning_queued",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="workspace",
        target_id=workspace_id,
        detail={"backend": backend},
        dsn=cfg.control_plane_dsn,
    )
    return JSONResponse(
        status_code=202,
        content={
            "workspace_id": workspace_id,
            "instance_status": "queued",
            "backend": backend,
        },
    )


@router.put("/{workspace_id}")
async def update_workspace(request: Request, workspace_id: str):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config

    with get_db(cfg.control_plane_dsn) as conn:
        existing = query_one(
            conn,
            select(workspaces.c.id).where(
                and_(
                    workspaces.c.id == workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                )
            ),
        )
    if not existing:
        return JSONResponse(status_code=404, content={"detail": "Workspace not found"})
    require_workspace_access(ctx, workspace_id, manage=True)

    body = await request.json()
    values: dict[str, str | int] = {}
    if "name" in body:
        values["name"] = str(body["name"]).strip()
        if not values["name"]:
            return JSONResponse(status_code=400, content={"detail": "name is required"})
    if "description" in body:
        values["description"] = body["description"]
    if "join_open" in body:
        values["join_open"] = 1 if body["join_open"] else 0
    if not values:
        return JSONResponse(status_code=400, content={"detail": "No fields to update"})

    with get_db(cfg.control_plane_dsn) as conn:
        if "name" in values and workspace_name_taken(
            conn, ctx.tenant_id, values["name"], exclude_id=workspace_id
        ):
            return JSONResponse(
                status_code=409,
                content={"detail": "Workspace name already exists in this tenant"},
            )
        conn.execute(
            update(workspaces)
            .where(
                and_(
                    workspaces.c.id == workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                )
            )
            .values(**values)
        )
        conn.commit()
        ws = query_one(conn, select(workspaces).where(workspaces.c.id == workspace_id))
    if "join_open" in values:
        log_event(
            "join_open_changed",
            actor_id=ctx.user_id,
            actor_ip=request.headers.get("X-Forwarded-For", ""),
            target_type="workspace",
            target_id=workspace_id,
            detail={"join_open": values["join_open"]},
            dsn=cfg.control_plane_dsn,
        )
    return {"workspace": ws}


@router.delete("/{workspace_id}")
async def archive_workspace(request: Request, workspace_id: str):
    ctx = require_tenant_admin(request)
    if workspace_id == "default":
        return JSONResponse(
            status_code=400, content={"detail": "Cannot archive default workspace"}
        )
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        existing = query_one(
            conn,
            select(workspaces.c.id).where(
                and_(
                    workspaces.c.id == workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                )
            ),
        )
    if not existing:
        return JSONResponse(status_code=404, content={"detail": "Workspace not found"})
    require_workspace_access(ctx, workspace_id, manage=True)
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(
            update(workspaces)
            .where(
                and_(
                    workspaces.c.id == workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                )
            )
            .values(status="archived")
        )
        conn.commit()

    log_event(
        "workspace_archived",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="workspace",
        target_id=workspace_id,
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}

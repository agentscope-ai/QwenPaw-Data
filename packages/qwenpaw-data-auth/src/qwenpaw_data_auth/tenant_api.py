"""
Tenant management API — tenant_admin only.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.config import AuthConfig
from sqlalchemy import select, update

from qwenpaw_data_auth.authorization import (
    require_tenant_admin,
    require_tenant_admin_verified,
)
from qwenpaw_data_auth.database import get_db, query_one, tenants

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


@router.get("")
async def list_tenants(request: Request):
    ctx = require_tenant_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        row = query_one(conn, select(tenants).where(tenants.c.id == ctx.tenant_id))
    return {"tenants": [row] if row else []}


@router.post("")
async def create_tenant(request: Request):
    require_tenant_admin(request)
    return JSONResponse(
        status_code=403,
        content={"detail": "Tenant creation requires /api/platform/tenants"},
    )


@router.get("/{tenant_id}")
async def get_tenant(request: Request, tenant_id: str):
    ctx = require_tenant_admin(request)
    if tenant_id != ctx.tenant_id:
        return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        tenant = query_one(conn, select(tenants).where(tenants.c.id == tenant_id))
    if not tenant:
        return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
    return {"tenant": tenant}


@router.put("/{tenant_id}")
async def update_tenant(request: Request, tenant_id: str):
    ctx = require_tenant_admin_verified(request)
    if tenant_id != ctx.tenant_id:
        return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    values: dict[str, str | int] = {}
    if "name" in body:
        values["name"] = body["name"]
    if "description" in body:
        values["description"] = body["description"]
    if "join_open" in body:
        values["join_open"] = 1 if body["join_open"] else 0
    if not values:
        return JSONResponse(status_code=400, content={"detail": "No fields to update"})
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(update(tenants).where(tenants.c.id == tenant_id).values(**values))
        conn.commit()
        tenant = query_one(conn, select(tenants).where(tenants.c.id == tenant_id))
    if "join_open" in values:
        log_event(
            "join_open_changed",
            actor_id=ctx.user_id,
            actor_ip=request.headers.get("X-Forwarded-For", ""),
            target_type="tenant",
            target_id=tenant_id,
            detail={"join_open": values["join_open"]},
            dsn=cfg.control_plane_dsn,
        )
    return {"tenant": tenant}


@router.delete("/{tenant_id}")
async def suspend_tenant(request: Request, tenant_id: str):
    require_tenant_admin(request)
    return JSONResponse(
        status_code=403,
        content={"detail": "Tenant suspension requires platform control plane"},
    )

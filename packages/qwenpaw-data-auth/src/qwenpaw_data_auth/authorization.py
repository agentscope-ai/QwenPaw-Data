"""Central authorization for every control-plane management API.

Token claims are treated only as identity hints. Status, revocation, role and
scope are resolved from the control-plane database on every request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from fastapi import HTTPException, Request
from sqlalchemy import and_, select

from qwenpaw_data_auth.authentication import AuthenticationError
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import (
    get_db,
    platform_admins,
    query_all,
    query_one,
    role_bindings,
    tenants,
    users,
    workspaces,
)
from qwenpaw_data_auth.local import request_identity_payload
from qwenpaw_data_auth.token import TokenVerifier, TokenVerifyError


def _timestamp(value: str | None) -> float:
    if not value:
        return 0
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        # Corrupt revocation state is not permission to use an old token.
        return float("inf")


def extract_token(request: Request, cookie: str = "qwenpaw_data_access") -> str | None:
    # A supplied Authorization header wins: a bad header must not fall
    # through to a cookie. A non-Bearer header yields an invalid empty token.
    header = request.headers.get("Authorization")
    if header is not None:
        return header[7:] if header.lower().startswith("bearer ") else ""
    return request.cookies.get(cookie)


@dataclass(frozen=True)
class AuthorizationContext:
    user_id: str
    tenant_id: str | None
    # Highest role across the tenant; display/audit only.
    role: str
    # Role for the active workspace; authorization decisions use this value.
    active_workspace_role: str = "member"
    workspace_ids: frozenset[str] = field(default_factory=frozenset)
    admin_workspace_ids: frozenset[str] = field(default_factory=frozenset)
    active_workspace_id: str | None = None
    is_platform_admin: bool = False
    payload: dict = field(default_factory=dict)

    def can_manage_workspace(self, workspace_id: str) -> bool:
        return self.role == "tenant_admin" or (
            self.role == "ws_admin" and workspace_id in self.admin_workspace_ids
        )


def _verify_payload(request: Request, token: str | None = None) -> dict:
    cfg: AuthConfig = request.app.state.auth_config
    if token is None:
        # Tenant JWTs, the static API token and the local identity resolve in
        # one place; supplied credentials never fall back to local access.
        try:
            payload = request_identity_payload(request, cfg)
        except AuthenticationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    else:
        try:
            payload = TokenVerifier(cfg.secret_keys).verify(token)
        except TokenVerifyError as exc:
            raise HTTPException(status_code=401, detail="Invalid token") from exc
    if payload.get("typ") != "access":
        raise HTTPException(status_code=401, detail="Access token required")
    return payload


def authorize_request(
    request: Request,
    *,
    platform_only: bool = False,
    allow_platform: bool = False,
) -> AuthorizationContext:
    """Authenticate and resolve current roles/scopes from persistent state."""
    cfg: AuthConfig = request.app.state.auth_config
    token = (
        extract_token(request, cookie="qwenpaw_data_platform_access")
        if platform_only
        else None
    )
    payload = _verify_payload(request, token)
    user_id = payload.get("sub", "")

    with get_db(cfg.control_plane_dsn) as conn:
        if payload.get("platform") is True:
            admin = query_one(
                conn,
                select(platform_admins).where(platform_admins.c.id == user_id),
            )
            if not admin or admin["status"] != "active":
                raise HTTPException(status_code=401, detail="Platform account disabled")
            if payload.get("iat", 0) < _timestamp(admin.get("token_revoked_before")):
                raise HTTPException(status_code=401, detail="Token revoked")
            if not (platform_only or allow_platform):
                raise HTTPException(
                    status_code=403,
                    detail="platform_admin cannot access tenant data APIs",
                )
            return AuthorizationContext(
                user_id=user_id,
                tenant_id=None,
                role="platform_admin",
                active_workspace_role="platform_admin",
                is_platform_admin=True,
                payload=payload,
            )

        if platform_only:
            raise HTTPException(status_code=403, detail="platform_admin required")

        user = query_one(conn, select(users).where(users.c.id == user_id))
        if not user or user["status"] != "active":
            raise HTTPException(status_code=401, detail="User not found or disabled")
        tenant_id = user["tenant_id"]
        if payload.get("tenant_id") != tenant_id:
            raise HTTPException(status_code=401, detail="Token tenant mismatch")
        if payload.get("iat", 0) < _timestamp(user.get("token_revoked_before")):
            raise HTTPException(status_code=401, detail="Token revoked")
        tenant = query_one(
            conn, select(tenants.c.status).where(tenants.c.id == tenant_id)
        )
        if not tenant or tenant["status"] != "active":
            raise HTTPException(status_code=403, detail="Tenant unavailable")

        bindings = query_all(
            conn,
            select(
                role_bindings.c.role,
                role_bindings.c.scope_type,
                role_bindings.c.scope_id,
            ).where(role_bindings.c.user_id == user_id),
        )
        tenant_super = any(
            b["role"] == "tenant_admin"
            and b["scope_type"] == "tenant"
            and b["scope_id"] == tenant_id
            for b in bindings
        )

        if tenant_super:
            role = "tenant_admin"
            rows = query_all(
                conn,
                select(workspaces.c.id).where(
                    and_(
                        workspaces.c.tenant_id == tenant_id,
                        workspaces.c.status == "active",
                    )
                ),
            )
            workspace_ids = frozenset(row["id"] for row in rows)
            admin_workspace_ids = workspace_ids
        else:
            workspace_ids = frozenset(
                b["scope_id"]
                for b in bindings
                if b["scope_type"] == "workspace"
                and query_one(
                    conn,
                    select(workspaces.c.id).where(
                        and_(
                            workspaces.c.id == b["scope_id"],
                            workspaces.c.tenant_id == tenant_id,
                            workspaces.c.status == "active",
                        )
                    ),
                )
            )
            admin_workspace_ids = frozenset(
                b["scope_id"]
                for b in bindings
                if b["role"] == "ws_admin"
                and b["scope_type"] == "workspace"
                and b["scope_id"] in workspace_ids
            )
            role = "ws_admin" if admin_workspace_ids else "member"

    active_ws = payload.get("active_ws")
    if active_ws and active_ws not in workspace_ids:
        active_ws = None

    active_workspace_role = (
        "tenant_admin"
        if role == "tenant_admin"
        else "ws_admin"
        if active_ws in admin_workspace_ids
        else "member"
    )
    return AuthorizationContext(
        user_id=user_id,
        tenant_id=tenant_id,
        role=role,
        active_workspace_role=active_workspace_role,
        workspace_ids=workspace_ids,
        admin_workspace_ids=admin_workspace_ids,
        active_workspace_id=active_ws,
        payload=payload,
    )


def require_admin(request: Request) -> AuthorizationContext:
    ctx = authorize_request(request)
    if ctx.active_workspace_role not in {"tenant_admin", "ws_admin"}:
        raise HTTPException(status_code=403, detail="Admin required")
    return ctx


def require_tenant_admin(request: Request) -> AuthorizationContext:
    ctx = authorize_request(request)
    if ctx.active_workspace_role != "tenant_admin":
        raise HTTPException(status_code=403, detail="tenant_admin required")
    return ctx


def require_platform_admin(request: Request) -> AuthorizationContext:
    return authorize_request(request, platform_only=True)


def require_tenant_admin_verified(request: Request) -> AuthorizationContext:
    """``require_tenant_admin`` for privileged writes."""
    return require_tenant_admin(request)


def require_platform_admin_verified(request: Request) -> AuthorizationContext:
    """``require_platform_admin`` for privileged writes."""
    return require_platform_admin(request)


def require_workspace_access(
    ctx: AuthorizationContext, workspace_id: str, *, manage: bool = False
) -> None:
    allowed = (
        ctx.can_manage_workspace(workspace_id)
        if manage
        else workspace_id in ctx.workspace_ids
    )
    if not allowed:
        raise HTTPException(status_code=403, detail="No access to this workspace")


def load_target_user(request: Request, ctx: AuthorizationContext, user_id: str) -> dict:
    """Return a manageable target or hide it with 404 across tenant boundaries."""
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        user = query_one(conn, select(users).where(users.c.id == user_id))
        if not user or user["tenant_id"] != ctx.tenant_id:
            raise HTTPException(status_code=404, detail="User not found")
        bindings = query_all(
            conn,
            select(
                role_bindings.c.role,
                role_bindings.c.scope_type,
                role_bindings.c.scope_id,
            ).where(role_bindings.c.user_id == user_id),
        )
    if ctx.role == "ws_admin":
        if any(b["role"] == "tenant_admin" for b in bindings):
            raise HTTPException(status_code=404, detail="User not found")
        target_ws = {b["scope_id"] for b in bindings if b["scope_type"] == "workspace"}
        if not target_ws or not target_ws.issubset(ctx.admin_workspace_ids):
            raise HTTPException(status_code=404, detail="User not found")
    user["role_bindings"] = bindings
    return user

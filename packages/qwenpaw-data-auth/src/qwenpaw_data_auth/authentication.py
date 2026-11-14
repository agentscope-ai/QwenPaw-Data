"""Reusable request-token authentication for QwenPawData services."""

from __future__ import annotations

from datetime import datetime

from qwenpaw_data_auth.database import get_db, query_all, query_one
from qwenpaw_data_auth.principal import Principal
from qwenpaw_data_auth.token import TokenVerifier, TokenVerifyError


class AuthenticationError(PermissionError):
    def __init__(self, detail: str, status_code: int = 401):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code

    def __str__(self) -> str:
        return self.detail


def extract_access_token(
    cookie_token: str | None, authorization_header: str
) -> str | None:
    # A supplied Authorization header wins, matching the credential that
    # request_identity_payload verified; a non-Bearer header yields no token.
    if authorization_header:
        if authorization_header.lower().startswith("bearer "):
            return authorization_header[7:]
        return None
    if cookie_token:
        return cookie_token
    return None


def _timestamp(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        # Corrupt revocation state is not permission to use an old token.
        return float("inf")


def resolve_current_authorization(
    dsn: str,
    user_id: str,
    tenant_id: str,
    active_ws: str,
) -> tuple[list[str], str, float]:
    with get_db(dsn) as conn:
        user = query_one(
            conn,
            """
            SELECT * FROM users
            WHERE id = ? AND tenant_id = ? AND status = 'active'
            """,
            (user_id, tenant_id),
        )
        if not user:
            raise AuthenticationError("User is inactive or no longer exists", 403)
        tenant = query_one(
            conn,
            "SELECT status FROM tenants WHERE id = ?",
            (tenant_id,),
        )
        if not tenant or tenant["status"] != "active":
            raise AuthenticationError("Tenant unavailable", 403)
        rows = query_all(
            conn,
            """
            SELECT role, scope_type, scope_id FROM role_bindings
            WHERE user_id = ?
            """,
            (user_id,),
        )
        is_tenant_admin = any(
            row["role"] == "tenant_admin"
            and row["scope_type"] == "tenant"
            and row["scope_id"] == tenant_id
            for row in rows
        )
        if is_tenant_admin:
            workspaces = query_all(
                conn,
                """
                SELECT id FROM workspaces
                WHERE tenant_id = ? AND status = 'active'
                """,
                (tenant_id,),
            )
            workspace_ids = sorted(str(row["id"]) for row in workspaces)
            role = "tenant_admin"
        else:
            candidate_ids = sorted(
                {
                    str(row["scope_id"])
                    for row in rows
                    if row["scope_type"] == "workspace"
                }
            )
            if candidate_ids:
                placeholders = ",".join(["?"] * len(candidate_ids))
                workspaces = query_all(
                    conn,
                    f"""
                    SELECT id FROM workspaces
                    WHERE id IN ({placeholders}) AND tenant_id = ?
                      AND status = 'active'
                    """,
                    (*candidate_ids, tenant_id),
                )
                workspace_ids = sorted(str(row["id"]) for row in workspaces)
            else:
                workspace_ids = []
            active_roles = [
                row["role"]
                for row in rows
                if row["scope_type"] == "workspace" and row["scope_id"] == active_ws
            ]
            role = (
                "ws_admin"
                if "ws_admin" in active_roles
                else "member"
                if "member" in active_roles
                else ""
            )
        if active_ws not in workspace_ids or not role:
            raise AuthenticationError("Active workspace is not authorized", 403)
        revoked_at = _timestamp(user.get("token_revoked_before"))
    return workspace_ids, role, revoked_at


def authenticate_access_token(
    token: str,
    *,
    secrets: list[str],
    dsn: str,
) -> tuple[Principal, float, float]:
    try:
        payload = TokenVerifier(secrets=secrets).verify(token)
    except TokenVerifyError as exc:
        raise AuthenticationError(str(exc), 401) from exc
    if payload.get("typ") != "access" or payload.get("platform") is True:
        raise AuthenticationError("Tenant access token required", 401)

    user_id = str(payload.get("sub") or "")
    tenant_id = str(payload.get("tenant_id") or "")
    active_ws = str(payload.get("active_ws") or "")
    workspace_ids, role, revoked_at = resolve_current_authorization(
        dsn,
        user_id,
        tenant_id,
        active_ws,
    )

    principal = Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        workspace_ids=workspace_ids,
        active_workspace_id=active_ws,
        role=role,
    )
    return principal, float(payload.get("iat") or 0), revoked_at


def principal_from_payload(payload: dict, dsn: str) -> tuple[Principal, float, float]:
    """Revalidate a verified identity payload against current control-plane state.

    Used for every credential ``request_identity_payload`` accepts (user JWT,
    local identity, delegated machine user); revoked tokens are rejected here.
    """
    if payload.get("typ") != "access" or payload.get("platform") is True:
        raise AuthenticationError("Tenant access token required", 401)
    user_id, tenant_id, active_ws = (
        payload.get(key) for key in ("sub", "tenant_id", "active_ws")
    )
    if not all(isinstance(value, str) and value for value in (user_id, tenant_id, active_ws)):
        raise AuthenticationError("Invalid tenant token claims", 401)
    workspace_ids, role, revoked_at = resolve_current_authorization(
        dsn,
        user_id,
        tenant_id,
        active_ws,
    )
    iat = float(payload.get("iat") or 0)
    if iat < revoked_at:
        raise AuthenticationError("Token revoked", 401)
    principal = Principal(
        user_id=user_id,
        tenant_id=tenant_id,
        workspace_ids=workspace_ids,
        active_workspace_id=active_ws,
        role=role,
    )
    return principal, iat, revoked_at


def authenticate_request(request, *, config=None, allow_machine: bool = False) -> Principal:
    """Authenticate one request for Host/Context-style callers.

    ``allow_machine`` must be opted into by machine-only routes; it is what
    lets a valid service secret delegate ``X-User-Id``.
    """
    from qwenpaw_data_auth.local import request_identity_payload

    cfg = config or request.app.state.auth_config
    payload = request_identity_payload(request, cfg, allow_machine=allow_machine)
    return principal_from_payload(payload, cfg.control_plane_dsn)[0]


__all__ = [
    "AuthenticationError",
    "authenticate_access_token",
    "authenticate_request",
    "extract_access_token",
    "principal_from_payload",
    "resolve_current_authorization",
]

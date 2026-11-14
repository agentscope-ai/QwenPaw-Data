"""
Authentication API endpoints.

POST /api/auth/register    — first-user-only registration
POST /api/auth/login       — username/password login with rate limiting
GET  /api/auth/status      — public auth status
GET  /api/auth/verify      — token verification
GET  /api/auth/me          — current user identity (from cookie)
POST /api/auth/switch-workspace — switch active workspace
POST /api/auth/temporary-access — service-signed access token
POST /api/auth/logout      — logout + cookie clear + token revocation
POST /api/auth/refresh     — refresh token rotation
POST /api/auth/exchange    — authorization code → cookie
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.authorization import authorize_request
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.control_plane import ControlPlaneReader
from sqlalchemy import and_, func, insert, select, update

from qwenpaw_data_auth.database import (
    authorization_codes,
    get_db,
    new_id,
    platform_admins,
    query_one,
    query_all,
    refresh_tokens,
    role_bindings,
    users,
    utcnow_iso,
    workspaces,
)
from qwenpaw_data_auth.local import is_safe_loopback_request, service_secret_ok
from qwenpaw_data_auth.models import (
    LoginRequest,
    SwitchWorkspaceRequest,
    ExchangeCodeRequest,
    ChangePasswordRequest,
    TemporaryAccessRequest,
)
from qwenpaw_data_auth.password import (
    hash_password,
    verify_password,
    validate_password_strength,
)
from qwenpaw_data_auth.permissions import PERMISSION_MATRIX
from qwenpaw_data_auth.token import TokenIssuer, TokenVerifier, TokenVerifyError

logger = logging.getLogger("qwenpaw_data_auth.auth_api")

router = APIRouter(prefix="/api/auth", tags=["auth"])

login_fails_by_ip: dict[str, list[float]] = defaultdict(list)
login_fails_by_user: dict[str, list[float]] = defaultdict(list)

RATE_WINDOW = 900  # 15 minutes
MAX_IP_FAILS = 20
MAX_USER_FAILS = 5


def clear_login_failures(username: str, tenant_id: str | None = None) -> None:
    """Clear account-scoped failures after an authenticated/admin reset."""
    del tenant_id  # retained for API compatibility
    login_fails_by_user.pop(username, None)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(timestamps: list[float]) -> list[float]:
    cutoff = time.time() - RATE_WINDOW
    return [t for t in timestamps if t > cutoff]


def _check_rate_limit(ip: str, username: str) -> Optional[JSONResponse]:
    now = time.time()

    ip_fails = _prune(login_fails_by_ip[ip])
    login_fails_by_ip[ip] = ip_fails
    if len(ip_fails) >= MAX_IP_FAILS:
        retry_after = int(RATE_WINDOW - (now - ip_fails[0]))
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Too many login attempts from this IP",
                "retry_after": max(retry_after, 1),
            },
        )

    user_fails = _prune(login_fails_by_user[username])
    login_fails_by_user[username] = user_fails
    if len(user_fails) >= MAX_USER_FAILS:
        retry_after = int(RATE_WINDOW - (now - user_fails[0]))
        return JSONResponse(
            status_code=429,
            content={
                "detail": "Too many login attempts for this account",
                "retry_after": max(retry_after, 1),
            },
        )
    return None


def _set_auth_cookies(
    response: Response, access_token: str, refresh_token: str, cfg: AuthConfig
) -> None:
    response.set_cookie(
        key="qwenpaw_data_access",
        value=access_token,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
        max_age=cfg.access_token_ttl,
        domain=cfg.cookie_domain,
    )
    response.set_cookie(
        key="qwenpaw_data_refresh",
        value=refresh_token,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/api/auth/refresh",
        max_age=cfg.refresh_token_ttl,
        domain=cfg.cookie_domain,
    )


def _clear_auth_cookies(response: Response, cfg: AuthConfig) -> None:
    response.delete_cookie("qwenpaw_data_access", path="/", domain=cfg.cookie_domain)
    response.delete_cookie(
        "qwenpaw_data_refresh", path="/api/auth/refresh", domain=cfg.cookie_domain
    )


def _get_config(request: Request) -> AuthConfig:
    return request.app.state.auth_config


def _get_issuer(request: Request) -> TokenIssuer:
    cfg = _get_config(request)
    return TokenIssuer(secret=cfg.secret_keys[0])


def _get_verifier(request: Request) -> TokenVerifier:
    cfg = _get_config(request)
    return TokenVerifier(secrets=cfg.secret_keys)


def _extract_token(request: Request) -> str | None:
    token = request.cookies.get("qwenpaw_data_access")
    if token:
        return token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def _store_refresh(request: Request, token: str) -> None:
    cfg = _get_config(request)
    payload = _get_verifier(request).verify(token)
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(
            insert(refresh_tokens).values(
                jti=payload["jti"],
                user_id=payload["sub"],
                expires_at=int(payload["exp"]),
            )
        )
        conn.commit()


def _issue_refresh(request: Request, user_id: str, tenant_id: str) -> str:
    cfg = _get_config(request)
    token = _get_issuer(request).issue_refresh(
        user_id, tenant_id, cfg.refresh_token_ttl
    )
    _store_refresh(request, token)
    return token


def _redirect_uri_allowed(cfg: AuthConfig, redirect_uri: str) -> bool:
    try:
        parsed = urlsplit(redirect_uri)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return False
        # Userinfo never belongs in a redirect target, and IPv6 loopback
        # origins are configured in their bracketed form.
        if parsed.username is not None or parsed.password is not None:
            return False
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        origin = f"{parsed.scheme}://{host}"
        if parsed.port is not None:
            origin += f":{parsed.port}"
    except ValueError:
        return False
    return origin.rstrip("/") in cfg.authorization_redirect_origins


def _role_for_workspace(conn, user_id: str, tenant_id: str, workspace_id: str) -> str:
    bindings = query_all(
        conn,
        select(
            role_bindings.c.role,
            role_bindings.c.scope_type,
            role_bindings.c.scope_id,
        ).where(role_bindings.c.user_id == user_id),
    )
    if any(
        b["role"] == "tenant_admin"
        and b["scope_type"] == "tenant"
        and b["scope_id"] == tenant_id
        for b in bindings
    ):
        return "tenant_admin"
    if any(
        b["role"] == "ws_admin"
        and b["scope_type"] == "workspace"
        and b["scope_id"] == workspace_id
        for b in bindings
    ):
        return "ws_admin"
    return "member"


@router.get("/status")
async def auth_status(request: Request):
    cfg = _get_config(request)
    # Local mode counts its bootstrapped account too, so clients can tell a
    # ready single-user runtime from an empty control plane.
    with get_db(cfg.control_plane_dsn) as conn:
        row = query_one(conn, select(func.count().label("cnt")).select_from(users))
        has_users = (row["cnt"] if row else 0) > 0
        platform_row = query_one(
            conn,
            select(func.count().label("cnt")).select_from(platform_admins),
        )
        has_platform_admin = (platform_row["cnt"] if platform_row else 0) > 0
    return {
        "enabled": cfg.auth_enabled,
        "mode": cfg.mode,
        "has_users": has_users,
        "has_platform_admin": has_platform_admin,
        "platform_bootstrap_required": cfg.auth_enabled and not has_platform_admin,
        "legacy_registration_enabled": cfg.allow_legacy_first_user_registration,
        # This build has no single sign-on; the sign-in screen always shows the
        # local form. The fields stay for clients that read them.
        "sso_enabled": False,
        "sso_login_path": "",
    }


@router.post("/register")
async def auth_register(request: Request, body: LoginRequest):
    cfg = _get_config(request)
    if not cfg.auth_enabled:
        return JSONResponse(status_code=403, content={"detail": "Auth not enabled"})
    if not cfg.allow_legacy_first_user_registration:
        return JSONResponse(
            status_code=403,
            content={
                "detail": "Public registration disabled; use platform tenant onboarding"
            },
        )

    ip = _client_ip(request)

    with get_db(cfg.control_plane_dsn) as conn:
        row = query_one(conn, select(func.count().label("cnt")).select_from(users))
        if row and row["cnt"] > 0:
            return JSONResponse(
                status_code=403,
                content={"detail": "Registration closed. Contact admin."},
            )

        violations = validate_password_strength(body.password)
        if violations:
            return JSONResponse(
                status_code=400, content={"detail": "; ".join(violations)}
            )

        user_id = new_id()
        pwd_hash = hash_password(body.password)

        try:
            conn.execute(
                insert(users).values(
                    id=user_id,
                    tenant_id="default",
                    username=body.username,
                    password_hash=pwd_hash,
                    status="active",
                    password_changed_at=utcnow_iso(),
                )
            )
            conn.execute(
                insert(role_bindings).values(
                    id=new_id(),
                    user_id=user_id,
                    role="tenant_admin",
                    scope_type="tenant",
                    scope_id="default",
                )
            )
            conn.commit()
        except Exception:
            return JSONResponse(
                status_code=409, content={"detail": "Username already exists"}
            )

    log_event(
        "register",
        actor_id=user_id,
        actor_ip=ip,
        target_type="user",
        target_id=user_id,
        dsn=cfg.control_plane_dsn,
    )

    issuer = _get_issuer(request)
    access = issuer.issue_access(
        user_id, "default", "default", "tenant_admin", cfg.access_token_ttl
    )
    refresh = _issue_refresh(request, user_id, "default")

    resp = JSONResponse(
        status_code=201,
        content={
            "tenant_id": "default",
            "role": "tenant_admin",
            "workspaces": [{"id": "default", "name": "Default Workspace"}],
            "active_workspace": {"id": "default", "name": "Default Workspace"},
        },
    )
    _set_auth_cookies(resp, access, refresh, cfg)
    return resp


@router.post("/login")
async def auth_login(request: Request, body: LoginRequest):
    cfg = _get_config(request)
    # Local mode accepts password sign-in too: the local administrator may set
    # a password (see change-password) and use it from another client.
    ip = _client_ip(request)
    account_key = body.username

    rate_resp = _check_rate_limit(ip, account_key)
    if rate_resp:
        return rate_resp

    with get_db(cfg.control_plane_dsn) as conn:
        # Usernames are unique per tenant, and globally unique only among rows
        # that carry a password. Restricting the lookup to those rows keeps this
        # tenant-free query single-valued: a passwordless account in another
        # tenant that happens to share the name cannot shadow the real password
        # account.
        user = query_one(
            conn,
            select(users).where(
                and_(users.c.username == body.username, users.c.password_hash != "")
            ),
        )

        if not user or not verify_password(body.password, user["password_hash"]):
            login_fails_by_ip[ip].append(time.time())
            login_fails_by_user[account_key].append(time.time())
            log_event(
                "login_failed",
                actor_ip=ip,
                target_type="user",
                target_id=body.username,
                dsn=cfg.control_plane_dsn,
            )
            return JSONResponse(
                status_code=401, content={"detail": "Invalid username or password"}
            )

        if user["status"] != "active":
            return JSONResponse(
                status_code=401, content={"detail": "Account is disabled"}
            )

        if user.get("locked_until"):
            from datetime import datetime

            locked = datetime.fromisoformat(user["locked_until"].replace("Z", "+00:00"))
            if locked.timestamp() > time.time():
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Account is temporarily locked",
                        "retry_after": int(locked.timestamp() - time.time()),
                    },
                )

        conn.execute(
            update(users)
            .where(users.c.id == user["id"])
            .values(failed_login_count=0, locked_until=None)
        )
        conn.commit()

        login_fails_by_ip[ip] = [
            t for t in login_fails_by_ip[ip] if t > time.time() - RATE_WINDOW
        ]
        login_fails_by_user[account_key] = []

        bindings = query_all(
            conn,
            select(role_bindings).where(role_bindings.c.user_id == user["id"]),
        )
        reader = ControlPlaneReader(cfg.control_plane_dsn)
        ws_ids = reader.get_user_workspace_ids(user["id"], user["tenant_id"])
        if not ws_ids:
            return JSONResponse(
                status_code=403, content={"detail": "No active workspace"}
            )
        active_ws_id = (
            user.get("active_workspace_id")
            if user.get("active_workspace_id") in ws_ids
            else ws_ids[0]
        )
        conn.execute(
            update(users)
            .where(users.c.id == user["id"])
            .values(active_workspace_id=active_ws_id)
        )
        conn.commit()
        tenant_super = any(
            binding["role"] == "tenant_admin"
            and binding["scope_type"] == "tenant"
            and binding["scope_id"] == user["tenant_id"]
            for binding in bindings
        )
        role = (
            "tenant_admin"
            if tenant_super
            else "ws_admin"
            if any(
                binding["role"] == "ws_admin"
                and binding["scope_type"] == "workspace"
                and binding["scope_id"] == active_ws_id
                for binding in bindings
            )
            else "member"
        )

        workspace_list = []
        for ws_id in ws_ids:
            ws = query_one(conn, select(workspaces).where(workspaces.c.id == ws_id))
            if ws:
                workspace_list.append({"id": ws["id"], "name": ws["name"]})

        active_ws = next(
            (w for w in workspace_list if w["id"] == active_ws_id),
            {"id": active_ws_id, "name": active_ws_id},
        )

    log_event(
        "login_success",
        actor_id=user["id"],
        actor_ip=ip,
        target_type="user",
        target_id=user["id"],
        dsn=cfg.control_plane_dsn,
    )

    issuer = _get_issuer(request)
    access = issuer.issue_access(
        user["id"],
        user["tenant_id"],
        active_ws_id,
        role,
        cfg.access_token_ttl,
    )
    refresh = _issue_refresh(request, user["id"], user["tenant_id"])

    result: dict = {
        "tenant_id": user["tenant_id"],
        "role": role,
        "workspaces": workspace_list,
        "active_workspace": active_ws,
    }
    if user.get("force_password_change"):
        result["must_change_password"] = True

    resp = JSONResponse(content=result)
    _set_auth_cookies(resp, access, refresh, cfg)
    return resp


@router.get("/verify")
async def auth_verify(request: Request):
    try:
        ctx = authorize_request(request, allow_platform=True)
        return {
            "user_id": ctx.user_id,
            "tenant_id": ctx.tenant_id or "",
            "active_workspace_id": ctx.active_workspace_id or "",
            "role": ctx.active_workspace_role,
        }
    except Exception as exc:
        status = getattr(exc, "status_code", 401)
        return JSONResponse(
            status_code=status,
            content={"detail": getattr(exc, "detail", "Invalid token")},
        )


@router.get("/me")
async def auth_me(request: Request):
    cfg = _get_config(request)
    try:
        ctx = authorize_request(request)
    except Exception as exc:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 401),
            content={"detail": getattr(exc, "detail", "Invalid token")},
        )

    with get_db(cfg.control_plane_dsn) as conn:
        user = query_one(
            conn,
            select(
                users.c.username,
                users.c.display_name,
                users.c.force_password_change,
            ).where(users.c.id == ctx.user_id),
        )
        ws_name = ctx.active_workspace_id or ""
        ws_row = query_one(
            conn,
            select(workspaces.c.name).where(
                and_(
                    workspaces.c.id == ctx.active_workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                )
            ),
        )
        if ws_row:
            ws_name = ws_row["name"]

        workspace_list = []
        for wid in ctx.workspace_ids:
            w_row = query_one(
                conn,
                select(workspaces.c.id, workspaces.c.name).where(
                    workspaces.c.id == wid
                ),
            )
            if w_row:
                workspace_list.append({"id": w_row["id"], "name": w_row["name"]})
            else:
                workspace_list.append({"id": wid, "name": wid})
        active_workspace_role = _role_for_workspace(
            conn,
            ctx.user_id,
            ctx.tenant_id,
            ctx.active_workspace_id or "",
        )
        capabilities = sorted(
            resource
            for resource, allowed in PERMISSION_MATRIX.get(
                active_workspace_role, {}
            ).items()
            if allowed
        )

    return {
        "user_id": ctx.user_id,
        "username": user["username"] if user else "",
        # Display name; falls back to username when absent.
        "display_name": (user and user.get("display_name"))
        or (user["username"] if user else ""),
        "tenant_id": ctx.tenant_id,
        "role": ctx.role,
        "workspace_ids": sorted(ctx.workspace_ids),
        "workspaces": workspace_list,
        "active_workspace_id": ctx.active_workspace_id or "",
        "active_workspace_name": ws_name,
        "active_workspace_role": active_workspace_role,
        "capabilities": capabilities,
        "must_change_password": bool(user and user.get("force_password_change")),
    }


@router.post("/switch-workspace")
async def auth_switch_workspace(request: Request, body: SwitchWorkspaceRequest):
    cfg = _get_config(request)
    try:
        ctx = authorize_request(request)
    except Exception as exc:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 401),
            content={"detail": getattr(exc, "detail", "Invalid token")},
        )
    if body.workspace_id not in ctx.workspace_ids:
        return JSONResponse(
            status_code=403,
            content={"detail": "User does not have access to this workspace"},
        )

    issuer = _get_issuer(request)
    with get_db(cfg.control_plane_dsn) as conn:
        ws_row = query_one(
            conn,
            select(workspaces.c.name).where(
                and_(
                    workspaces.c.id == body.workspace_id,
                    workspaces.c.tenant_id == ctx.tenant_id,
                    workspaces.c.status == "active",
                )
            ),
        )
        if not ws_row:
            return JSONResponse(
                status_code=404, content={"detail": "Workspace not found"}
            )
        target_role = _role_for_workspace(
            conn,
            ctx.user_id,
            ctx.tenant_id,
            body.workspace_id,
        )
        conn.execute(
            update(users)
            .where(users.c.id == ctx.user_id)
            .values(active_workspace_id=body.workspace_id)
        )
        conn.commit()
        ws_name = ws_row["name"] if ws_row else body.workspace_id

    access = issuer.issue_access(
        ctx.user_id,
        ctx.tenant_id,
        body.workspace_id,
        target_role,
        cfg.access_token_ttl,
    )
    resp = JSONResponse(
        content={"active_workspace": {"id": body.workspace_id, "name": ws_name}}
    )
    resp.set_cookie(
        key="qwenpaw_data_access",
        value=access,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="lax",
        path="/",
        max_age=cfg.access_token_ttl,
        domain=cfg.cookie_domain,
    )
    return resp


def _service_secret_ok(request: Request, cfg: AuthConfig) -> bool:
    """Constant-time check of the machine-to-machine header credential.

    A repeated header is rejected rather than resolved to one of its values.
    """
    return service_secret_ok(request, cfg)


@router.post("/temporary-access")
async def auth_temporary_access(request: Request, body: TemporaryAccessRequest):
    """Issue an access token for a service acting as a user.

    Accepts an identity triple; Auth resolves role from current membership.
    No refresh token, no cookie, ``active_workspace_id`` unchanged.
    ``ttl`` defaults to ``access_token_ttl``; values outside ``1 .. access_token_ttl`` are rejected.
    """
    cfg = _get_config(request)
    ip = _client_ip(request)

    if not cfg.service_tokens_enabled:
        return JSONResponse(
            status_code=503, content={"detail": "Service token issuance not enabled"}
        )
    if not _service_secret_ok(request, cfg):
        log_event(
            "service_access_denied",
            actor_ip=ip,
            detail={"reason": "bad_secret"},
            dsn=cfg.control_plane_dsn,
        )
        return JSONResponse(
            status_code=403, content={"detail": "Valid service secret required"}
        )

    user_id = body.user_id.strip()
    tenant_id = body.tenant_id.strip()
    workspace_id = body.workspace_id.strip()
    if not (user_id and tenant_id and workspace_id):
        return JSONResponse(
            status_code=400,
            content={"detail": "user_id, tenant_id and workspace_id are required"},
        )
    ttl = cfg.access_token_ttl if body.ttl is None else body.ttl
    if not 0 < ttl <= cfg.access_token_ttl:
        return JSONResponse(
            status_code=400,
            content={"detail": f"ttl must be between 1 and {cfg.access_token_ttl}"},
        )

    ws_ids = ControlPlaneReader(cfg.control_plane_dsn).get_user_workspace_ids(
        user_id, tenant_id
    )
    with get_db(cfg.control_plane_dsn) as conn:
        user = query_one(conn, "SELECT * FROM users WHERE id = ?", (user_id,))
        if not user or user["status"] != "active":
            return JSONResponse(
                status_code=404, content={"detail": "User not found or disabled"}
            )
        if user["tenant_id"] != tenant_id:
            return JSONResponse(
                status_code=403,
                content={"detail": "User does not belong to this tenant"},
            )
        tenant = query_one(
            conn, "SELECT status FROM tenants WHERE id = ?", (tenant_id,)
        )
        if not tenant or tenant["status"] != "active":
            return JSONResponse(
                status_code=403, content={"detail": "Tenant unavailable"}
            )
        workspace = query_one(
            conn,
            "SELECT id FROM workspaces WHERE id = ? AND tenant_id = ? AND status = 'active'",
            (workspace_id, tenant_id),
        )
        if not workspace or workspace_id not in ws_ids:
            return JSONResponse(
                status_code=403, content={"detail": "No access to this workspace"}
            )
        role = _role_for_workspace(conn, user_id, tenant_id, workspace_id)

    access = _get_issuer(request).issue_access(
        user_id,
        tenant_id,
        workspace_id,
        role,
        ttl,
    )
    log_event(
        "service_access_issued",
        actor_id=user_id,
        actor_ip=ip,
        target_type="workspace",
        target_id=workspace_id,
        detail={"role": role, "ttl": ttl},
        dsn=cfg.control_plane_dsn,
    )
    return JSONResponse(content={"access_token": access})


@router.post("/logout")
async def auth_logout(request: Request):
    cfg = _get_config(request)
    token = _extract_token(request)
    ip = _client_ip(request)

    if token and cfg.auth_enabled:
        try:
            verifier = _get_verifier(request)
            payload = verifier.verify(token)
            user_id = payload["sub"]
            with get_db(cfg.control_plane_dsn) as conn:
                conn.execute(
                    update(users)
                    .where(users.c.id == user_id)
                    .values(token_revoked_before=utcnow_iso())
                )
                conn.commit()
            log_event(
                "logout", actor_id=user_id, actor_ip=ip, dsn=cfg.control_plane_dsn
            )
        except TokenVerifyError:
            pass

    resp = JSONResponse(content={"detail": "Logged out successfully"})
    _clear_auth_cookies(resp, cfg)
    return resp


@router.post("/refresh")
async def auth_refresh(request: Request):
    cfg = _get_config(request)
    refresh_token_val = request.cookies.get("qwenpaw_data_refresh")
    if not refresh_token_val:
        return JSONResponse(status_code=401, content={"detail": "No refresh token"})

    try:
        verifier = _get_verifier(request)
        payload = verifier.verify(refresh_token_val)
    except TokenVerifyError:
        resp = JSONResponse(
            status_code=401, content={"detail": "Invalid refresh token"}
        )
        _clear_auth_cookies(resp, cfg)
        return resp

    if payload.get("typ") != "refresh":
        return JSONResponse(status_code=401, content={"detail": "Not a refresh token"})

    user_id = payload["sub"]
    tenant_id = payload.get("tenant_id", "default")

    with get_db(cfg.control_plane_dsn) as conn:
        stored = query_one(
            conn,
            select(refresh_tokens).where(
                refresh_tokens.c.jti == payload.get("jti", "")
            ),
        )
        if (
            not stored
            or stored["user_id"] != user_id
            or stored["consumed_at"] is not None
            or int(stored["expires_at"]) < int(time.time())
        ):
            conn.rollback()
            resp = JSONResponse(
                status_code=401, content={"detail": "Refresh token replayed or revoked"}
            )
            _clear_auth_cookies(resp, cfg)
            return resp
        user = query_one(
            conn,
            select(users).where(
                and_(users.c.id == user_id, users.c.status == "active")
            ),
        )
        if not user:
            conn.rollback()
            resp = JSONResponse(
                status_code=401, content={"detail": "User not found or disabled"}
            )
            _clear_auth_cookies(resp, cfg)
            return resp

        if user.get("token_revoked_before"):
            from datetime import datetime

            revoked_ts = datetime.fromisoformat(
                user["token_revoked_before"].replace("Z", "+00:00")
            ).timestamp()
            if payload.get("iat", 0) < revoked_ts:
                resp = JSONResponse(
                    status_code=401, content={"detail": "Token revoked"}
                )
                _clear_auth_cookies(resp, cfg)
                return resp
        reader = ControlPlaneReader(cfg.control_plane_dsn)
        ws_ids = reader.get_user_workspace_ids(user_id, tenant_id)
        if not ws_ids:
            conn.rollback()
            return JSONResponse(
                status_code=403, content={"detail": "No active workspace"}
            )
        active_ws = (
            user.get("active_workspace_id")
            if user.get("active_workspace_id") in ws_ids
            else ws_ids[0]
        )
        bindings = query_all(
            conn,
            select(
                role_bindings.c.role,
                role_bindings.c.scope_type,
                role_bindings.c.scope_id,
            ).where(role_bindings.c.user_id == user_id),
        )
        role = (
            "tenant_admin"
            if any(
                b["role"] == "tenant_admin"
                and b["scope_type"] == "tenant"
                and b["scope_id"] == tenant_id
                for b in bindings
            )
            else "ws_admin"
            if any(
                b["role"] == "ws_admin"
                and b["scope_type"] == "workspace"
                and b["scope_id"] == active_ws
                for b in bindings
            )
            else "member"
        )
        conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(active_workspace_id=active_ws)
        )
        issuer = _get_issuer(request)
        new_access = issuer.issue_access(
            user_id,
            tenant_id,
            active_ws,
            role,
            cfg.access_token_ttl,
        )
        new_refresh = issuer.issue_refresh(user_id, tenant_id, cfg.refresh_token_ttl)
        new_payload = verifier.verify(new_refresh)
        consumed = conn.execute(
            update(refresh_tokens)
            .where(
                and_(
                    refresh_tokens.c.jti == payload["jti"],
                    refresh_tokens.c.consumed_at.is_(None),
                )
            )
            .values(consumed_at=utcnow_iso(), replaced_by=new_payload["jti"])
        )
        if consumed.rowcount != 1:
            conn.rollback()
            return JSONResponse(
                status_code=401, content={"detail": "Refresh token replayed"}
            )
        conn.execute(
            insert(refresh_tokens).values(
                jti=new_payload["jti"],
                user_id=user_id,
                expires_at=int(new_payload["exp"]),
            )
        )
        conn.commit()

    resp = JSONResponse(content={"detail": "Token refreshed"})
    _set_auth_cookies(resp, new_access, new_refresh, cfg)
    return resp


@router.post("/exchange")
async def auth_exchange(request: Request, body: ExchangeCodeRequest):
    cfg = _get_config(request)
    if not cfg.auth_enabled:
        return JSONResponse(status_code=403, content={"detail": "Auth not enabled"})

    try:
        verifier = _get_verifier(request)
        payload = verifier.verify(body.code)
    except TokenVerifyError:
        return JSONResponse(
            status_code=400, content={"detail": "Invalid or expired authorization code"}
        )

    if payload.get("typ") != "authorization_code":
        return JSONResponse(
            status_code=400, content={"detail": "Not an authorization code"}
        )
    if not _redirect_uri_allowed(cfg, body.redirect_uri):
        return JSONResponse(
            status_code=400, content={"detail": "redirect_uri is not allowed"}
        )

    with get_db(cfg.control_plane_dsn) as conn:
        code_row = query_one(
            conn,
            select(authorization_codes).where(
                authorization_codes.c.code == payload.get("jti", "")
            ),
        )
        if not code_row:
            return JSONResponse(
                status_code=400, content={"detail": "Authorization code not found"}
            )
        if code_row["used"]:
            return JSONResponse(
                status_code=400, content={"detail": "Authorization code already used"}
            )
        if (
            body.redirect_uri != payload.get("redirect_uri")
            or body.redirect_uri != code_row["redirect_uri"]
        ):
            return JSONResponse(
                status_code=400, content={"detail": "redirect_uri mismatch"}
            )
        try:
            expires_at = datetime.fromisoformat(
                code_row["expires_at"].replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            expires_at = 0
        if expires_at < time.time():
            return JSONResponse(
                status_code=400, content={"detail": "Authorization code expired"}
            )
        updated = conn.execute(
            update(authorization_codes)
            .where(
                and_(
                    authorization_codes.c.code == payload["jti"],
                    authorization_codes.c.used == 0,
                )
            )
            .values(used=1)
        )
        if updated.rowcount != 1:
            conn.rollback()
            return JSONResponse(
                status_code=400, content={"detail": "Authorization code already used"}
            )
        conn.commit()

    user_id = payload["sub"]
    tenant_id = "default"

    with get_db(cfg.control_plane_dsn) as conn:
        user = query_one(
            conn,
            select(users).where(
                and_(users.c.id == user_id, users.c.status == "active")
            ),
        )
        if not user:
            return JSONResponse(status_code=401, content={"detail": "User not found"})
        if user.get("token_revoked_before"):
            revoked_at = datetime.fromisoformat(
                user["token_revoked_before"].replace("Z", "+00:00")
            ).timestamp()
            if payload.get("iat", 0) < revoked_at:
                return JSONResponse(
                    status_code=401, content={"detail": "Authorization code revoked"}
                )
        tenant_id = user["tenant_id"]

    reader = ControlPlaneReader(cfg.control_plane_dsn)
    ws_ids = reader.get_user_workspace_ids(user_id, tenant_id)
    if not ws_ids:
        return JSONResponse(status_code=403, content={"detail": "No active workspace"})
    active_ws = (
        user.get("active_workspace_id")
        if user.get("active_workspace_id") in ws_ids
        else ws_ids[0]
    )
    with get_db(cfg.control_plane_dsn) as conn:
        role = _role_for_workspace(conn, user_id, tenant_id, active_ws)
        conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(active_workspace_id=active_ws)
        )
        conn.commit()

    issuer = _get_issuer(request)
    access = issuer.issue_access(
        user_id,
        tenant_id,
        active_ws,
        role,
        cfg.access_token_ttl,
    )
    refresh = _issue_refresh(request, user_id, tenant_id)

    resp = JSONResponse(content={"detail": "Token issued"})
    _set_auth_cookies(resp, access, refresh, cfg)
    return resp


@router.post("/change-password")
async def auth_change_password(request: Request, body: ChangePasswordRequest):
    cfg = _get_config(request)

    try:
        ctx = authorize_request(request)
    except Exception as exc:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 401),
            content={"detail": getattr(exc, "detail", "Invalid token")},
        )

    user_id = ctx.user_id
    actor_ip = request.client.host if request.client else ""
    # The anonymous loopback session of local mode already acts as the local
    # administrator, so it may set that account's password without the old one.
    # Machine credentials and real tokens still have to prove the old password.
    local_setup = bool(
        cfg.local_mode
        and ctx.payload.get("local")
        and not ctx.payload.get("machine")
        and is_safe_loopback_request(request)
    )

    with get_db(cfg.control_plane_dsn) as conn:
        user = query_one(
            conn,
            select(users).where(
                and_(users.c.id == user_id, users.c.status == "active")
            ),
        )
        if not user:
            return JSONResponse(status_code=404, content={"detail": "User not found"})

        is_forced_change = bool(user.get("force_password_change")) or local_setup
        if not is_forced_change and not body.current_password:
            return JSONResponse(
                status_code=400,
                content={"detail": "Current password is required"},
            )
        if not is_forced_change and not verify_password(
            body.current_password or "", user["password_hash"]
        ):
            log_event(
                "login_failed",
                actor_id=user_id,
                actor_ip=actor_ip,
                target_type="user",
                target_id=user_id,
                detail={"reason": "wrong_current_password"},
                dsn=cfg.control_plane_dsn,
            )
            return JSONResponse(
                status_code=400, content={"detail": "Current password is incorrect"}
            )

        violations = validate_password_strength(body.new_password)
        if violations:
            return JSONResponse(
                status_code=400,
                content={"detail": "Password too weak", "violations": violations},
            )

        now = utcnow_iso()
        new_hash = hash_password(body.new_password)
        conn.execute(
            update(users)
            .where(users.c.id == user_id)
            .values(
                password_hash=new_hash,
                password_changed_at=now,
                force_password_change=0,
                token_revoked_before=now,
            )
        )
        conn.commit()

    log_event(
        "password_changed",
        actor_id=user_id,
        actor_ip=actor_ip,
        target_type="user",
        target_id=user_id,
        dsn=cfg.control_plane_dsn,
    )

    active_ws = ctx.active_workspace_id
    if not active_ws:
        return JSONResponse(status_code=403, content={"detail": "No active workspace"})
    issuer = _get_issuer(request)
    active_role = ctx.role
    with get_db(cfg.control_plane_dsn) as conn:
        active_role = _role_for_workspace(
            conn,
            user_id,
            ctx.tenant_id,
            active_ws,
        )
    new_access = issuer.issue_access(
        user_id,
        ctx.tenant_id,
        active_ws,
        active_role,
        cfg.access_token_ttl,
    )
    new_refresh = _issue_refresh(request, user_id, ctx.tenant_id)

    resp = JSONResponse(content={"detail": "Password changed"})
    _set_auth_cookies(resp, new_access, new_refresh, cfg)
    return resp


@router.post("/authorize")
async def auth_authorize(request: Request):
    """Generate a one-time authorization code for multi-origin deployment.

    The logged-in user (identified by cookie) gets a short-lived code
    that another origin can exchange for its own cookies.
    """
    cfg = _get_config(request)
    if not cfg.auth_enabled:
        return JSONResponse(status_code=403, content={"detail": "Auth not enabled"})

    try:
        ctx = authorize_request(request)
    except Exception as exc:
        return JSONResponse(
            status_code=getattr(exc, "status_code", 401),
            content={"detail": getattr(exc, "detail", "Invalid token")},
        )

    user_id = ctx.user_id
    redirect_uri = (await request.json()).get("redirect_uri", "")
    if not redirect_uri:
        return JSONResponse(
            status_code=400, content={"detail": "redirect_uri is required"}
        )
    if not _redirect_uri_allowed(cfg, redirect_uri):
        return JSONResponse(
            status_code=400, content={"detail": "redirect_uri is not allowed"}
        )

    issuer = _get_issuer(request)
    code_token = issuer.issue_authorization_code(user_id, redirect_uri)

    verifier = _get_verifier(request)
    code_payload = verifier.verify(code_token)
    jti = code_payload["jti"]

    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(
            insert(authorization_codes).values(
                code=jti,
                user_id=user_id,
                redirect_uri=redirect_uri,
                expires_at=datetime.fromtimestamp(
                    code_payload["exp"], timezone.utc
                ).isoformat(),
                used=0,
            )
        )
        conn.commit()

    return JSONResponse(content={"code": code_token})

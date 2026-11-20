"""Platform control-plane bootstrap and tenant onboarding APIs."""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, exists, func, insert, or_, select, update

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.provision_backend import (
    configured_provision_backends,
    parse_provision_backend,
    queue_workspace_values,
)
from qwenpaw_data_auth.unique_names import tenant_name_taken, username_conflict
from qwenpaw_data_auth.authorization import (
    require_platform_admin,
    require_platform_admin_verified,
)
from qwenpaw_data_auth.breakglass import (
    issue_raw_cypher_grant,
    raw_query_digest,
    revoke_raw_cypher_grant,
)
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.database import (
    get_db,
    new_id,
    platform_admins,
    query_all,
    query_one,
    raw_cypher_grants,
    role_bindings,
    tenants,
    users,
    utcnow_iso,
    workspaces,
)
from qwenpaw_data_auth.password import (
    hash_password,
    validate_password_strength,
    verify_password,
)
from qwenpaw_data_auth.token import TokenIssuer

router = APIRouter(prefix="/api/platform", tags=["platform"])


def _valid_instance_refs(cfg: AuthConfig, endpoint_ref: str, secret_ref: str) -> bool:
    if not secret_ref.startswith(("env://", "env:", "file://")):
        return False
    if endpoint_ref.startswith(("env://", "env:")):
        return True
    try:
        parsed = urlsplit(endpoint_ref)
        # Credentials belong in secret_ref; endpoint_ref is listed back to
        # platform admins.
        return (
            parsed.scheme in {"bolt", "bolt+s", "neo4j", "neo4j+s"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and parsed.hostname.lower() in cfg.workspace_endpoint_allowed_hosts
        )
    except ValueError:
        return False


def _platform_token(request: Request, admin_id: str) -> str:
    cfg: AuthConfig = request.app.state.auth_config
    return TokenIssuer(cfg.secret_keys[0]).issue_platform_access(
        admin_id, cfg.access_token_ttl
    )


def set_platform_access_cookie(response, token: str, cfg: AuthConfig) -> None:
    response.set_cookie(
        key="qwenpaw_data_platform_access",
        value=token,
        httponly=True,
        secure=cfg.cookie_secure,
        samesite="strict",
        path="/api/platform",
        max_age=cfg.access_token_ttl,
        domain=cfg.cookie_domain,
    )


def clear_platform_access_cookie(response, cfg: AuthConfig) -> None:
    response.delete_cookie(
        "qwenpaw_data_platform_access", path="/api/platform", domain=cfg.cookie_domain
    )


def _platform_login_response(request: Request, admin_id: str, status_code: int = 200):
    cfg: AuthConfig = request.app.state.auth_config
    response = JSONResponse(
        status_code=status_code,
        content={"role": "platform_admin"},
    )
    set_platform_access_cookie(response, _platform_token(request, admin_id), cfg)
    return response


@router.post("/bootstrap")
async def bootstrap(request: Request):
    """Create the one bootstrap platform administrator exactly once."""
    cfg: AuthConfig = request.app.state.auth_config
    configured_token = cfg.platform_bootstrap_token
    supplied_token = request.headers.get("X-QwenPaw-Data-Bootstrap-Token", "")
    if not configured_token or not secrets.compare_digest(
        configured_token.encode(), supplied_token.encode()
    ):
        return JSONResponse(
            status_code=403,
            content={"detail": "Valid bootstrap token required"},
        )
    body = await request.json()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    if not username:
        return JSONResponse(status_code=400, content={"detail": "username is required"})
    violations = validate_password_strength(password)
    if violations:
        return JSONResponse(status_code=400, content={"detail": "; ".join(violations)})
    admin_id = new_id()
    with get_db(cfg.control_plane_dsn) as conn:
        count = query_one(
            conn, select(func.count().label("cnt")).select_from(platform_admins)
        )
        if count and count["cnt"]:
            conn.rollback()
            return JSONResponse(status_code=403, content={"detail": "Bootstrap closed"})
        conn.execute(
            insert(platform_admins).values(
                id=admin_id,
                username=username,
                password_hash=hash_password(password),
            )
        )
        conn.commit()
    log_event(
        "platform_bootstrap",
        actor_id=admin_id,
        actor_ip=request.client.host if request.client else "",
        target_type="platform_admin",
        target_id=admin_id,
        dsn=cfg.control_plane_dsn,
    )
    return _platform_login_response(request, admin_id, status_code=201)


@router.post("/login")
async def platform_login(request: Request):
    from qwenpaw_data_auth.auth_api import (
        _check_rate_limit,
        _client_ip,
        login_fails_by_ip,
        login_fails_by_user,
    )

    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()

    ip = _client_ip(request)
    username = str(body.get("username", "")).strip()
    rate_response = _check_rate_limit(ip, f"platform:{username}")
    if rate_response:
        return rate_response
    with get_db(cfg.control_plane_dsn) as conn:
        admin = query_one(
            conn,
            select(platform_admins).where(platform_admins.c.username == username),
        )
    if (
        not admin
        or admin["status"] != "active"
        or not verify_password(str(body.get("password", "")), admin["password_hash"])
    ):
        import time

        login_fails_by_ip[ip].append(time.time())
        login_fails_by_user[f"platform:{username}"].append(time.time())
        log_event(
            "platform_login_failed",
            actor_ip=request.client.host if request.client else "",
            target_type="platform_admin",
            target_id=str(body.get("username", "")).strip(),
            dsn=cfg.control_plane_dsn,
        )
        return JSONResponse(status_code=401, content={"detail": "Invalid credentials"})
    login_fails_by_user[f"platform:{username}"] = []
    log_event(
        "platform_login_success",
        actor_id=admin["id"],
        actor_ip=request.client.host if request.client else "",
        target_type="platform_admin",
        target_id=admin["id"],
        dsn=cfg.control_plane_dsn,
    )
    return _platform_login_response(request, admin["id"])


@router.get("/me")
async def platform_me(request: Request):
    actor = require_platform_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        admin = query_one(
            conn,
            select(
                platform_admins.c.id,
                platform_admins.c.username,
                platform_admins.c.display_name,
                platform_admins.c.status,
            ).where(platform_admins.c.id == actor.user_id),
        )
    if not admin:
        return JSONResponse(
            status_code=401, content={"detail": "Platform account missing"}
        )
    return {
        "user_id": admin["id"],
        "username": admin["username"],
        # 花名 for display; falls back to username when absent.
        "display_name": admin.get("display_name") or admin["username"],
        "tenant_id": "",
        "role": "platform_admin",
        "workspace_ids": [],
        "workspaces": [],
        "active_workspace_id": "",
        "active_workspace_name": "",
        "capabilities": [],
        "must_change_password": False,
    }


@router.post("/logout")
async def platform_logout(request: Request):
    actor = require_platform_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        conn.execute(
            update(platform_admins)
            .where(platform_admins.c.id == actor.user_id)
            .values(token_revoked_before=utcnow_iso())
        )
        conn.commit()
    response = JSONResponse(content={"detail": "Logged out successfully"})
    clear_platform_access_cookie(response, cfg)
    return response


@router.get("/tenants")
async def platform_list_tenants(request: Request):
    require_platform_admin(request)
    cfg: AuthConfig = request.app.state.auth_config

    def _ws_field(column, label: str):
        return (
            select(column)
            .where(workspaces.c.tenant_id == tenants.c.id)
            .order_by(workspaces.c.created_at)
            .limit(1)
            .scalar_subquery()
            .label(label)
        )

    instance_status_subq = _ws_field(workspaces.c.instance_status, "instance_status")
    instance_version_subq = _ws_field(workspaces.c.instance_version, "instance_version")
    endpoint_ref_subq = _ws_field(workspaces.c.endpoint_ref, "endpoint_ref")
    with get_db(cfg.control_plane_dsn) as conn:
        return {
            "tenants": query_all(
                conn,
                select(
                    tenants,
                    instance_status_subq,
                    instance_version_subq,
                    endpoint_ref_subq,
                ).order_by(tenants.c.created_at),
            ),
            "provision_backends": configured_provision_backends(),
        }


@router.post("/tenants")
@router.post("/tenants/onboard")
async def onboard_tenant(request: Request):
    """Atomically create tenant, default workspace and first tenant_admin."""
    actor = require_platform_admin_verified(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    name = str(body.get("name", "")).strip()
    username = str(
        body.get("admin_username") or body.get("tenant_admin_username") or ""
    ).strip()
    password = str(
        body.get("admin_password") or body.get("tenant_admin_password") or ""
    )

    if not name or not username:
        return JSONResponse(
            status_code=400,
            content={"detail": "name and admin_username are required"},
        )
    violations = validate_password_strength(password)
    if violations:
        return JSONResponse(
            status_code=400, content={"detail": "; ".join(violations)}
        )
    pwd_hash = hash_password(password)

    tenant_id = str(body.get("id", "")).strip() or new_id()
    workspace_id = str(body.get("workspace_id", "")).strip() or new_id()
    user_id = new_id()
    try:
        with get_db(cfg.control_plane_dsn) as conn:
            if tenant_name_taken(conn, name):
                conn.rollback()
                return JSONResponse(
                    status_code=409,
                    content={"detail": "Tenant name already exists"},
                )
            # A password account signs in by username alone, so that name has
            # to be free across every tenant.
            conflict = username_conflict(
                conn, tenant_id, username, has_password=bool(pwd_hash)
            )
            if conflict:
                conn.rollback()
                return JSONResponse(status_code=409, content={"detail": conflict})
            conn.execute(
                insert(tenants).values(
                    id=tenant_id,
                    name=name,
                    neo4j_database=(
                        str(body.get("neo4j_database", "")).strip()
                        or f"tenant_{tenant_id}"
                    ),
                    description=str(body.get("description", "")),
                    status="provisioning",
                    onboarding_state="pending",
                )
            )
            conn.execute(
                insert(workspaces).values(
                    id=workspace_id,
                    tenant_id=tenant_id,
                    name=(
                        str(
                            body.get("workspace_name")
                            or body.get("default_workspace_name")
                            or ""
                        ).strip()
                        or "Default Workspace"
                    ),
                    description="Tenant default workspace",
                    status="provisioning",
                    endpoint_ref=None,
                    secret_ref=None,
                    instance_status="pending",
                    instance_version=None,
                )
            )
            conn.execute(
                insert(users).values(
                    id=user_id,
                    tenant_id=tenant_id,
                    username=username,
                    password_hash=pwd_hash,
                    status="pending",
                    password_changed_at=utcnow_iso() if pwd_hash else None,
                )
            )
            conn.execute(
                insert(role_bindings).values(
                    id=new_id(),
                    user_id=user_id,
                    role="tenant_admin",
                    scope_type="tenant",
                    scope_id=tenant_id,
                )
            )
            conn.commit()
    except Exception:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "Tenant onboarding conflict; no partial data was committed"
            },
        )
    log_event(
        "tenant_onboarding_started",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="tenant",
        target_id=tenant_id,
        detail={"workspace_id": workspace_id, "tenant_admin_user_id": user_id},
        dsn=cfg.control_plane_dsn,
    )
    return JSONResponse(
        status_code=201,
        content={
            "tenant_id": tenant_id,
            "default_workspace_id": workspace_id,
            "tenant_admin_user_id": user_id,
            "onboarding_state": "pending",
            "created_by": actor.user_id,
        },
    )


@router.post("/tenants/{tenant_id}/provision")
async def request_tenant_provisioning(request: Request, tenant_id: str):
    """Queue local infrastructure provisioning without exposing Docker to Auth."""
    actor = require_platform_admin_verified(request)
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
        tenant = query_one(conn, select(tenants).where(tenants.c.id == tenant_id))
        if not tenant:
            conn.rollback()
            return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
        workspace = query_one(
            conn,
            select(workspaces.c.id, workspaces.c.instance_status)
            .where(workspaces.c.tenant_id == tenant_id)
            .order_by(workspaces.c.created_at)
            .limit(1),
        )
        if not workspace:
            conn.rollback()
            return JSONResponse(
                status_code=409, content={"detail": "Workspace missing"}
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
                    workspaces.c.id == workspace["id"],
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
        conn.execute(
            update(tenants)
            .where(tenants.c.id == tenant_id)
            .values(
                status="provisioning",
                onboarding_state="pending",
                onboarding_error=None,
            )
        )
        conn.commit()

    log_event(
        "tenant_provisioning_queued",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="tenant",
        target_id=tenant_id,
        detail={"workspace_id": workspace["id"], "backend": backend},
        dsn=cfg.control_plane_dsn,
    )
    return JSONResponse(
        status_code=202,
        content={
            "tenant_id": tenant_id,
            "workspace_id": workspace["id"],
            "instance_status": "queued",
            "backend": backend,
        },
    )


@router.put("/tenants/{tenant_id}")
async def update_tenant(request: Request, tenant_id: str):
    actor = require_platform_admin_verified(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    values: dict[str, str | int] = {}
    for field in ("name", "description"):
        if field in body:
            values[field] = str(body[field])
    if "join_open" in body:
        values["join_open"] = 1 if body["join_open"] else 0
    if not values:
        return JSONResponse(status_code=400, content={"detail": "No fields to update"})
    with get_db(cfg.control_plane_dsn) as conn:
        existing = query_one(
            conn, select(tenants.c.id).where(tenants.c.id == tenant_id)
        )
        if not existing:
            return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
        new_name = str(values.get("name", "")).strip()
        if "name" in values:
            if not new_name:
                return JSONResponse(
                    status_code=400, content={"detail": "name is required"}
                )
            values["name"] = new_name
            if tenant_name_taken(conn, new_name, exclude_id=tenant_id):
                return JSONResponse(
                    status_code=409,
                    content={"detail": "Tenant name already exists"},
                )
        conn.execute(update(tenants).where(tenants.c.id == tenant_id).values(**values))
        conn.commit()
        tenant = query_one(conn, select(tenants).where(tenants.c.id == tenant_id))
    log_event(
        "tenant_updated",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="tenant",
        target_id=tenant_id,
        dsn=cfg.control_plane_dsn,
    )
    if "join_open" in values:
        log_event(
            "join_open_changed",
            actor_id=actor.user_id,
            actor_ip=request.client.host if request.client else "",
            target_type="tenant",
            target_id=tenant_id,
            detail={"join_open": values["join_open"]},
            dsn=cfg.control_plane_dsn,
        )
    return {"tenant": tenant}


@router.post("/tenants/{tenant_id}/suspend")
async def suspend_tenant(request: Request, tenant_id: str):
    actor = require_platform_admin_verified(request)
    cfg: AuthConfig = request.app.state.auth_config
    now = utcnow_iso()
    with get_db(cfg.control_plane_dsn) as conn:
        tenant = query_one(conn, select(tenants.c.id).where(tenants.c.id == tenant_id))
        if not tenant:
            conn.rollback()
            return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
        conn.execute(
            update(tenants).where(tenants.c.id == tenant_id).values(status="suspended")
        )
        conn.execute(
            update(workspaces)
            .where(workspaces.c.tenant_id == tenant_id)
            .values(status="suspended")
        )
        conn.execute(
            update(users)
            .where(users.c.tenant_id == tenant_id)
            .values(token_revoked_before=now)
        )
        conn.commit()
    log_event(
        "tenant_suspended",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="tenant",
        target_id=tenant_id,
        dsn=cfg.control_plane_dsn,
    )
    return {"tenant_id": tenant_id, "status": "suspended", "actor_id": actor.user_id}


@router.post("/tenants/{tenant_id}/resume")
async def resume_onboarding(request: Request, tenant_id: str):
    """Activate metadata after an external provisioner reports a ready instance."""
    actor = require_platform_admin_verified(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    endpoint_ref = str(body.get("endpoint_ref", "")).strip()
    secret_ref = str(body.get("secret_ref", "")).strip()
    instance_version = str(body.get("instance_version", "")).strip()
    if not endpoint_ref or not secret_ref or not instance_version:
        return JSONResponse(
            status_code=400,
            content={
                "detail": "endpoint_ref, secret_ref and instance_version are required"
            },
        )
    if not _valid_instance_refs(cfg, endpoint_ref, secret_ref):
        return JSONResponse(
            status_code=400,
            content={"detail": "Workspace instance references are not allowed"},
        )
    with get_db(cfg.control_plane_dsn) as conn:
        tenant = query_one(conn, select(tenants).where(tenants.c.id == tenant_id))
        if not tenant:
            return JSONResponse(status_code=404, content={"detail": "Tenant not found"})
        workspace = query_one(
            conn,
            select(workspaces.c.id)
            .where(workspaces.c.tenant_id == tenant_id)
            .order_by(workspaces.c.created_at)
            .limit(1),
        )
        if not workspace:
            return JSONResponse(
                status_code=409, content={"detail": "Workspace missing"}
            )
        conn.execute(
            update(workspaces)
            .where(workspaces.c.id == workspace["id"])
            .values(
                endpoint_ref=endpoint_ref,
                secret_ref=secret_ref,
                instance_status="ready",
                instance_version=instance_version,
                status="active",
            )
        )
        conn.execute(
            update(tenants)
            .where(tenants.c.id == tenant_id)
            .values(
                status="active",
                onboarding_state="ready",
                onboarding_error=None,
            )
        )
        conn.execute(
            update(users)
            .where(
                and_(
                    users.c.tenant_id == tenant_id,
                    users.c.status == "pending",
                )
            )
            .values(status="active")
        )
        conn.commit()
    log_event(
        "tenant_onboarding_completed",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="tenant",
        target_id=tenant_id,
        detail={"workspace_id": workspace["id"], "instance_version": instance_version},
        dsn=cfg.control_plane_dsn,
    )
    return {"tenant_id": tenant_id, "onboarding_state": "ready"}


@router.post("/raw-cypher-grants")
async def create_raw_cypher_grant(request: Request):
    """Platform operator issues a query-bound, one-time Raw Cypher grant."""
    actor = require_platform_admin_verified(request)
    cfg: AuthConfig = request.app.state.auth_config
    body = await request.json()
    actor_user_id = str(body.get("actor_user_id", "")).strip()
    tenant_id = str(body.get("tenant_id", "")).strip()
    workspace_id = str(body.get("workspace_id", "")).strip()
    cypher = str(body.get("cypher", ""))
    params = body.get("params", {})
    try:
        ttl_seconds = int(body.get("ttl_seconds", 300))
    except (TypeError, ValueError):
        return JSONResponse(status_code=400, content={"detail": "Invalid grant scope"})
    if (
        not actor_user_id
        or not tenant_id
        or not workspace_id
        or not cypher.strip()
        or not isinstance(params, dict)
    ):
        return JSONResponse(status_code=400, content={"detail": "Invalid grant scope"})
    query_digest = raw_query_digest(cypher, params)

    rb = role_bindings.alias("rb")
    # Move SQLite's ``strftime(..., '-10 minutes')`` into Python so the query is
    # dialect-independent; ``created_at`` is a second-precision ISO ``Z`` string,
    # so match that format for a correct lexical comparison.
    recent_cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_db(cfg.control_plane_dsn) as conn:
        target = query_one(
            conn,
            select(users.c.id)
            .select_from(
                users.join(
                    workspaces,
                    and_(
                        workspaces.c.id == workspace_id,
                        workspaces.c.tenant_id == users.c.tenant_id,
                    ),
                )
            )
            .where(
                and_(
                    users.c.id == actor_user_id,
                    users.c.tenant_id == tenant_id,
                    users.c.status == "active",
                    workspaces.c.status == "active",
                    workspaces.c.instance_status.in_(["ready", "active", "running"]),
                    exists(
                        select(1)
                        .select_from(rb)
                        .where(
                            and_(
                                rb.c.user_id == users.c.id,
                                or_(
                                    and_(
                                        rb.c.role == "tenant_admin",
                                        rb.c.scope_type == "tenant",
                                        rb.c.scope_id == users.c.tenant_id,
                                    ),
                                    and_(
                                        rb.c.role == "ws_admin",
                                        rb.c.scope_type == "workspace",
                                        rb.c.scope_id == workspaces.c.id,
                                    ),
                                ),
                            )
                        )
                    ),
                )
            ),
        )
        recent = query_one(
            conn,
            select(func.count().label("cnt"))
            .select_from(raw_cypher_grants)
            .where(
                and_(
                    raw_cypher_grants.c.actor_user_id == actor_user_id,
                    raw_cypher_grants.c.created_at > recent_cutoff,
                )
            ),
        )
    if not target:
        return JSONResponse(
            status_code=404, content={"detail": "Grant target not found"}
        )
    if recent and int(recent["cnt"]) >= 5:
        return JSONResponse(
            status_code=429,
            content={"detail": "Raw Cypher grant rate limit exceeded"},
        )

    token, grant = issue_raw_cypher_grant(
        cfg.control_plane_dsn,
        platform_admin_id=actor.user_id,
        actor_user_id=actor_user_id,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        query_digest=query_digest,
        ttl_seconds=ttl_seconds,
    )
    log_event(
        "raw_cypher_grant_issued",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="raw_cypher_grant",
        target_id=grant["id"],
        detail={
            "actor_user_id": actor_user_id,
            "tenant_id": tenant_id,
            "workspace_id": workspace_id,
            "query_digest": query_digest,
            "expires_at": grant["expires_at"],
        },
        dsn=cfg.control_plane_dsn,
    )
    return JSONResponse(
        status_code=201,
        content={
            "grant_id": grant["id"],
            "grant_token": token,
            "expires_at": grant["expires_at"],
        },
    )


@router.delete("/raw-cypher-grants/{grant_id}")
async def revoke_raw_cypher_grant_api(request: Request, grant_id: str):
    actor = require_platform_admin_verified(request)
    cfg: AuthConfig = request.app.state.auth_config
    revoked = revoke_raw_cypher_grant(
        cfg.control_plane_dsn,
        grant_id=grant_id,
        platform_admin_id=actor.user_id,
    )
    if not revoked:
        return JSONResponse(
            status_code=404, content={"detail": "Active grant not found"}
        )
    log_event(
        "raw_cypher_grant_revoked",
        actor_id=actor.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="raw_cypher_grant",
        target_id=grant_id,
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}

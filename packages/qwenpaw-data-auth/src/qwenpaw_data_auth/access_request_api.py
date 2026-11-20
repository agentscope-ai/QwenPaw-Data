"""Submission and review API for self-service join requests."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import and_, select
from starlette.background import BackgroundTask

from qwenpaw_data_auth.audit import log_event
from qwenpaw_data_auth.authorization import (
    AuthorizationContext,
    authorize_request,
    require_admin,
)
from qwenpaw_data_auth.config import AuthConfig
from qwenpaw_data_auth.control_plane import ControlPlaneReader
from qwenpaw_data_auth.database import (
    access_requests,
    get_db,
    new_id,
    query_all,
    query_one,
    tenants,
    users,
    utcnow_iso,
    workspaces,
)
from qwenpaw_data_auth.mg_sync import sync_user_workspaces
from qwenpaw_data_auth.user_api import _default_workspace_id, _ensure_role_binding

router = APIRouter(prefix="/api/access-requests", tags=["access-requests"])

MAX_PENDING_REQUESTS = 5


def _open_target(conn, target_type: str, target_id: str) -> dict | None:
    """One open, active request target resolved to ``(tenant_id, name)``.

    Re-run at approval time as well: an admin may have closed the switch or
    archived the workspace while the request sat in the queue.
    """
    if target_type == "workspace":
        row = query_one(
            conn,
            select(workspaces.c.id, workspaces.c.name, workspaces.c.tenant_id).where(
                and_(
                    workspaces.c.id == target_id,
                    workspaces.c.join_open == 1,
                    workspaces.c.status == "active",
                )
            ),
        )
        if not row:
            return None
        tenant = query_one(
            conn,
            select(tenants.c.id).where(
                and_(tenants.c.id == row["tenant_id"], tenants.c.status == "active")
            ),
        )
        if not tenant:
            return None
        return {"tenant_id": row["tenant_id"], "name": row["name"]}
    if target_type == "tenant":
        row = query_one(
            conn,
            select(tenants.c.id, tenants.c.name).where(
                and_(
                    tenants.c.id == target_id,
                    tenants.c.join_open == 1,
                    tenants.c.status == "active",
                )
            ),
        )
        if not row:
            return None
        return {"tenant_id": row["id"], "name": row["name"]}
    return None


def _visible(ctx: AuthorizationContext, row: dict) -> bool:
    """A tenant_admin reviews the whole tenant; a ws_admin only their own.

    Tenant-level requests are therefore invisible to a ws_admin: granting one
    means choosing which workspace the applicant lands in, which is a decision
    outside their scope.
    """
    if row["tenant_id"] != ctx.tenant_id:
        return False
    if ctx.role == "tenant_admin":
        return True
    return (
        row["target_type"] == "workspace"
        and row["target_id"] in ctx.admin_workspace_ids
    )


def _serialize(conn, row: dict) -> dict:
    table = workspaces if row["target_type"] == "workspace" else tenants
    target = query_one(conn, select(table.c.name).where(table.c.id == row["target_id"]))
    return {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name_snapshot"],
        "target_type": row["target_type"],
        "target_id": row["target_id"],
        "target_name": (target or {}).get("name") or row["target_id"],
        "requested_role": row["requested_role"],
        "message": row["message"],
        "status": row["status"],
        "review_comment": row["review_comment"],
        "created_at": row["created_at"],
        "reviewed_at": row["reviewed_at"],
    }


def _load_for_review(conn, ctx: AuthorizationContext, request_id: str) -> dict | None:
    row = query_one(
        conn, select(access_requests).where(access_requests.c.id == request_id)
    )
    if not row or not _visible(ctx, row):
        return None
    return row


@router.get("")
async def list_access_requests(request: Request):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    status = request.query_params.get("status", "pending")

    with get_db(cfg.control_plane_dsn) as conn:
        rows = query_all(
            conn,
            select(access_requests)
            .where(
                and_(
                    access_requests.c.tenant_id == ctx.tenant_id,
                    access_requests.c.status == status,
                )
            )
            .order_by(access_requests.c.created_at.desc()),
        )
        visible = [_serialize(conn, row) for row in rows if _visible(ctx, row)]
    return {"requests": visible}


@router.post("/{request_id}/approve")
async def approve_access_request(request: Request, request_id: str):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — an empty body means "use the defaults"
        body = {}
    role = str(body.get("role") or "member")
    if role != "member":
        return JSONResponse(
            status_code=400, content={"detail": "Only the member role can be granted"}
        )

    with get_db(cfg.control_plane_dsn) as conn:
        row = _load_for_review(conn, ctx, request_id)
        if not row:
            return JSONResponse(
                status_code=404, content={"detail": "Request not found"}
            )
        if row["status"] != "pending":
            return JSONResponse(
                status_code=409, content={"detail": "Request has already been reviewed"}
            )

        # Re-check 1: the applicant's account may have been disabled since
        # submission.
        applicant = query_one(
            conn,
            select(users.c.id).where(
                and_(
                    users.c.id == row["user_id"],
                    users.c.tenant_id == row["tenant_id"],
                    users.c.status == "active",
                )
            ),
        )
        if not applicant:
            return JSONResponse(
                status_code=403,
                content={"detail": "Applicant is no longer active in this tenant"},
            )

        # Re-check 2 and 3: the target must still be open and active, and so
        # must its tenant — ``_open_target`` covers both.
        target = _open_target(conn, row["target_type"], row["target_id"])
        if not target:
            return JSONResponse(
                status_code=409,
                content={"detail": "Target is no longer open for requests"},
            )

        if row["target_type"] == "workspace":
            workspace_id = row["target_id"]
        else:
            workspace_id = str(body.get("workspace_id") or "").strip()
            if not workspace_id:
                workspace_id = _default_workspace_id(conn, row["tenant_id"]) or ""
            if not workspace_id:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Tenant has no active workspace to grant"},
                )
            landing = query_one(
                conn,
                select(workspaces.c.id).where(
                    and_(
                        workspaces.c.id == workspace_id,
                        workspaces.c.tenant_id == row["tenant_id"],
                        workspaces.c.status == "active",
                    )
                ),
            )
            if not landing:
                return JSONResponse(
                    status_code=404, content={"detail": "Workspace not found"}
                )

        # Re-check 4: the approver's own management of the landing workspace.
        if not ctx.can_manage_workspace(workspace_id):
            return JSONResponse(
                status_code=403, content={"detail": "No access to this workspace"}
            )

        reader = ControlPlaneReader(cfg.control_plane_dsn)
        user_id = applicant["id"]
        previous = set(reader.get_user_workspace_ids(user_id, row["tenant_id"]))
        _ensure_role_binding(conn, user_id, "member", "workspace", workspace_id)
        reviewed = conn.execute(
            """
            UPDATE access_requests
            SET status = 'approved', reviewer_user_id = ?, reviewed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (ctx.user_id, utcnow_iso(), request_id),
        )
        if reviewed.rowcount != 1:
            conn.rollback()
            return JSONResponse(
                status_code=409, content={"detail": "Request has already been reviewed"}
            )
        conn.commit()

    log_event(
        "access_request_approved",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="access_request",
        target_id=request_id,
        detail={
            "user_id": user_id,
            "workspace_id": workspace_id,
            "role": "member",
        },
        dsn=cfg.control_plane_dsn,
    )
    joined = set(reader.get_user_workspace_ids(user_id, row["tenant_id"])) - previous
    return JSONResponse(
        content={"detail": "ok", "user_id": user_id, "workspace_id": workspace_id},
        background=BackgroundTask(
            sync_user_workspaces, cfg, row["tenant_id"], user_id, joined
        ),
    )


@router.post("/{request_id}/reject")
async def reject_access_request(request: Request, request_id: str):
    ctx = require_admin(request)
    cfg: AuthConfig = request.app.state.auth_config
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a comment is optional
        body = {}
    comment = str(body.get("comment") or "").strip()[:500]

    with get_db(cfg.control_plane_dsn) as conn:
        row = _load_for_review(conn, ctx, request_id)
        if not row:
            return JSONResponse(
                status_code=404, content={"detail": "Request not found"}
            )
        reviewed = conn.execute(
            """
            UPDATE access_requests
            SET status = 'rejected', reviewer_user_id = ?, review_comment = ?,
                reviewed_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (ctx.user_id, comment, utcnow_iso(), request_id),
        )
        if reviewed.rowcount != 1:
            conn.rollback()
            return JSONResponse(
                status_code=409, content={"detail": "Request has already been reviewed"}
            )
        conn.commit()

    log_event(
        "access_request_rejected",
        actor_id=ctx.user_id,
        actor_ip=request.headers.get("X-Forwarded-For", ""),
        target_type="access_request",
        target_id=request_id,
        detail={"user_id": row["user_id"], "comment": comment},
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}


@router.post("")
async def create_access_request(request: Request):
    """File a request for a member seat in an open tenant or workspace.

    The applicant is the signed-in account, so a request never leaves the
    caller's own tenant.
    """
    ctx = authorize_request(request)
    cfg: AuthConfig = request.app.state.auth_config
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — a malformed body is a bad request
        body = {}
    if not isinstance(body, dict):
        body = {}
    target_type = str(body.get("target_type") or "").strip()
    target_id = str(body.get("target_id") or "").strip()
    message = str(body.get("message") or "").strip()[:500]
    if target_type not in {"tenant", "workspace"} or not target_id:
        return JSONResponse(
            status_code=400,
            content={"detail": "target_type and target_id are required"},
        )

    with get_db(cfg.control_plane_dsn) as conn:
        target = _open_target(conn, target_type, target_id)
        if not target or target["tenant_id"] != ctx.tenant_id:
            return JSONResponse(
                status_code=404, content={"detail": "Target is not open for requests"}
            )
        applicant = query_one(
            conn,
            select(users.c.username, users.c.display_name).where(
                and_(
                    users.c.id == ctx.user_id,
                    users.c.tenant_id == ctx.tenant_id,
                    users.c.status == "active",
                )
            ),
        )
        if not applicant:
            return JSONResponse(
                status_code=403, content={"detail": "An active account is required"}
            )
        member_ws = ControlPlaneReader(cfg.control_plane_dsn).get_user_workspace_ids(
            ctx.user_id, ctx.tenant_id
        )
        # A seat that reaches no workspace is not a member yet — that is
        # precisely the applicant this flow exists for.
        already = (
            target_id in member_ws if target_type == "workspace" else bool(member_ws)
        )
        if already:
            return JSONResponse(
                status_code=409,
                content={"detail": f"Already a member of this {target_type}"},
            )
        # Insert first, then re-read the applicant's pending set in the same
        # transaction: Hologres carries no unique index, so a check-then-insert
        # order lets a double submit land two rows for the same target.
        request_id = new_id()
        conn.execute(
            """
            INSERT INTO access_requests
                (id, user_id, name_snapshot, target_type, target_id, tenant_id,
                 requested_role, message, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'member', ?, 'pending', ?)
            """,
            (
                request_id,
                ctx.user_id,
                applicant["display_name"] or applicant["username"],
                target_type,
                target_id,
                target["tenant_id"],
                message,
                utcnow_iso(),
            ),
        )
        pending = query_all(
            conn,
            select(access_requests.c.id, access_requests.c.target_id).where(
                and_(
                    access_requests.c.user_id == ctx.user_id,
                    access_requests.c.status == "pending",
                )
            ),
        )
        if any(
            row["target_id"] == target_id and row["id"] != request_id for row in pending
        ):
            conn.rollback()
            return JSONResponse(
                status_code=409,
                content={"detail": "A request for this target is pending"},
            )
        if len(pending) > MAX_PENDING_REQUESTS:
            conn.rollback()
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many pending requests; wait for a review"},
            )
        conn.commit()

    log_event(
        "access_request_submitted",
        actor_id=ctx.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type=target_type,
        target_id=target_id,
        detail={"user_id": ctx.user_id, "request_id": request_id},
        dsn=cfg.control_plane_dsn,
    )
    return JSONResponse(
        status_code=201, content={"id": request_id, "status": "pending"}
    )


@router.post("/{request_id}/cancel")
async def cancel_access_request(request: Request, request_id: str):
    """Withdraw one of the caller's own pending requests."""
    ctx = authorize_request(request)
    cfg: AuthConfig = request.app.state.auth_config
    with get_db(cfg.control_plane_dsn) as conn:
        updated = conn.execute(
            """
            UPDATE access_requests SET status = 'canceled', reviewed_at = ?
            WHERE id = ? AND user_id = ? AND tenant_id = ? AND status = 'pending'
            """,
            (utcnow_iso(), request_id, ctx.user_id, ctx.tenant_id),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return JSONResponse(
                status_code=404, content={"detail": "No pending request to cancel"}
            )
        conn.commit()

    log_event(
        "access_request_canceled",
        actor_id=ctx.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="access_request",
        target_id=request_id,
        detail={"user_id": ctx.user_id},
        dsn=cfg.control_plane_dsn,
    )
    return {"detail": "ok"}

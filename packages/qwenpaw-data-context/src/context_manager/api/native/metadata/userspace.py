"""Expose self-scoped Metadata pulls, conflict decisions and personal additions."""

import asyncio
from contextlib import contextmanager
from typing import Literal

from qwenpaw_data_auth.audit import log_event
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ....auth import ScopeBinding, WriteOwner
from ....metadata import MetadataSyncConflict
from ....store import StoreError
from ...dependencies import AuthorizationDependency, RuntimeDependency


class ResolutionItem(BaseModel):
    kind: Literal["node"] = "node"
    label: str = ""
    key: str
    decision: Literal["global", "user"]


class ResolveRequest(BaseModel):
    global_digest: str
    resolutions: list[ResolutionItem] = Field(default_factory=list, max_length=1000)


class AppendNodeRequest(BaseModel):
    label: str
    key: str | None = Field(default=None, min_length=1, max_length=1000)
    props: dict[str, object] = Field(default_factory=dict)


class AppendEdgeRequest(BaseModel):
    from_key: str = Field(min_length=1, max_length=1000)
    to_key: str = Field(min_length=1, max_length=1000)
    rel_type: str = Field(
        min_length=1, max_length=100, pattern=r"^[A-Za-z][A-Za-z0-9_]*$"
    )
    props: dict[str, object] = Field(default_factory=dict)


router = APIRouter(prefix="/api/v1/mg/userspace", tags=["mg-userspace"])


@contextmanager
def _http_errors():
    try:
        yield
    except MetadataSyncConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, "Metadata userspace operation is forbidden") from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except StoreError as exc:
        if exc.code in {"user_space_uninitialized", "user_space_initializing"}:
            raise HTTPException(
                409, {"error": {"code": exc.code, "message": str(exc)}}
            ) from exc
        raise HTTPException(
            503 if exc.retryable else 500, "Metadata store operation failed"
        ) from exc
    except BlockingIOError as exc:
        raise HTTPException(503, "Metadata execution is busy") from exc


def _response(data):
    return {"ok": True, "data": data, "error": None, "meta": None}


def _audit(
    request: Request, scope: ScopeBinding, action: str, detail: dict[str, object]
) -> None:
    log_event(
        action,
        actor_id=scope.user_id,
        actor_ip=request.client.host if request.client else "",
        target_type="mg_userspace",
        target_id=scope.workspace_id,
        detail={
            "tenant_id": scope.tenant_id,
            "workspace_id": scope.workspace_id,
            **detail,
        },
        dsn=request.app.state.auth_config.control_plane_dsn,
    )


@router.get("/sync-status")
async def sync_status(runtime: RuntimeDependency, context: AuthorizationDependency):
    with _http_errors():
        return _response(await runtime.metadata_sync_status(context))


@router.post("/sync")
async def sync(
    request: Request, runtime: RuntimeDependency, context: AuthorizationDependency
):
    with _http_errors():
        # Sync also reconciles incident cross-graph edges in this user's graph.
        scope, result = await runtime.metadata_sync_pull(context)
    await asyncio.to_thread(
        _audit,
        request,
        scope,
        "mg_userspace_synced",
        {
            name: result[name]
            for name in (
                "created",
                "overwritten",
                "deleted",
                "auto_adopted",
                "conflict_count",
            )
        },
    )
    return _response(result)


@router.post("/sync/resolve")
async def resolve(
    body: ResolveRequest,
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    with _http_errors():
        scope, result = await runtime.metadata_sync_resolve(
            context,
            body.global_digest,
            [item.model_dump() for item in body.resolutions],
        )
    await asyncio.to_thread(
        _audit,
        request,
        scope,
        "mg_userspace_conflicts_resolved",
        {
            **result,
            "decisions": [
                {"key": item.key, "decision": item.decision}
                for item in body.resolutions
            ],
        },
    )
    return _response(result)


@router.post("/nodes")
async def append_node(
    body: AppendNodeRequest,
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def write():
        with _http_errors():
            scope = runtime.issue_graph_write_scope(context, owner=WriteOwner.USER)
            key = runtime.metadata_sync(scope).append_node(
                body.label, body.props, scope, key=body.key
            )
        result = {"label": body.label, "key": key}
        _audit(request, scope, "mg_userspace_node_appended", result)
        return _response(result)

    try:
        return await runtime.run_background(write)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph write execution is busy") from exc


@router.post("/edges")
async def append_edge(
    body: AppendEdgeRequest,
    request: Request,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def write():
        with _http_errors():
            scope = runtime.issue_graph_write_scope(context, owner=WriteOwner.USER)
            edge = runtime.metadata_sync(scope).append_edge(
                body.from_key,
                body.rel_type,
                body.to_key,
                body.props,
                scope,
            )
        result = {
            "from_key": edge.source_key,
            "rel_type": edge.spec.relationship,
            "to_key": edge.target_key,
        }
        _audit(request, scope, "mg_userspace_edge_appended", result)
        return _response(result)

    try:
        return await runtime.run_background(write)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph write execution is busy") from exc

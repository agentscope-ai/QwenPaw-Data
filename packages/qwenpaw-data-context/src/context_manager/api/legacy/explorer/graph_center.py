"""Serve the develop Graph Center contract with current Runtime and Scope."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ....auth import GLOBAL_OWNER_ID
from ....compatibility.explorer.journey import prepare_exploration
from ....store import StoreError
from ...dependencies import AuthorizationDependency, RuntimeDependency


class GlobalGraphRequest(BaseModel):
    max_edges: int = Field(200, ge=1, le=20_000)
    max_nodes: int = Field(120, ge=1, le=50_000)
    domain_roots_only: bool = True
    skeleton: bool = Field(
        True,
        description="Deprecated compatibility field; does not affect the graph.",
        deprecated=True,
    )
    zone_mode: Literal["all", "metadata", "knowledge"] = "all"
    datasource_id: str = ""
    owner_id: str = ""


class DomainGraphRequest(BaseModel):
    domain: str
    datasource_id: str = ""
    owner_id: str = ""


class ExpandNodeRequest(BaseModel):
    key: str
    limit: int = Field(50, ge=1, le=2000)
    zone: Literal["metadata", "knowledge", "user"] | None = None
    label: str | None = None
    owner_id: str = ""


class ExpandLayerRequest(BaseModel):
    key: str
    direction: Literal["in", "out", "up", "down"] = "out"
    limit: int = Field(50, ge=1, le=2000)
    owner_id: str = ""


class SearchNodesRequest(BaseModel):
    query: str = Field("", max_length=500)
    limit: int = Field(50, ge=1, le=100)
    owner_id: str = ""


class SearchSubgraphRequest(BaseModel):
    query: str
    scope: list[Literal["metadata", "knowledge", "user"]] = Field(
        default_factory=lambda: ["metadata", "knowledge"]
    )
    match_mode: Literal["exact", "fuzzy"] = "fuzzy"
    hops: int = Field(1, ge=1, le=3)
    limit: int = Field(50, ge=1, le=200)
    owner_id: str = ""


class EdgeDetailRequest(BaseModel):
    source_key: str
    target_key: str
    rel_type: str
    owner_id: str = ""


class TopicsRequest(BaseModel):
    owner_id: str = ""
    recompute: bool = False
    wait: bool = True
    lang: str | None = Field(default=None, max_length=16)


class ExploreRequest(BaseModel):
    query: str = Field(min_length=1)
    max_rounds: int = Field(default=8, ge=1, le=16)
    lang: str | None = Field(default=None, max_length=16)


def success(data):
    return {"ok": True, "data": data, "error": None, "meta": None}


def failure(code: str, message: str):
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message, "detail": {}},
        "meta": None,
    }


@contextmanager
def graph_errors():
    try:
        yield
    except HTTPException:
        raise
    except StoreError as exc:
        if exc.code in {"user_space_uninitialized", "user_space_initializing"}:
            raise HTTPException(409, failure(exc.code, str(exc))) from exc
        raise HTTPException(500, failure("INTERNAL_ERROR", str(exc))) from exc
    except BlockingIOError as exc:
        raise HTTPException(503, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, "graph read is forbidden") from exc
    except LookupError as exc:
        raise HTTPException(404, failure("NOT_FOUND", str(exc))) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, failure("INTERNAL_ERROR", str(exc))) from exc


def viewpoint(runtime, context, owner_id: str = ""):
    """Select develop's global-admin / personal-member canvas intent.

    Authority still decides whether the selected owner is readable. Explicit
    'my graph' never becomes a workspace-wide read, including for admins.
    """
    owner = owner_id.strip()
    if not owner:
        owner = (
            GLOBAL_OWNER_ID
            if context.active_workspace_role in {"ws_admin", "tenant_admin"}
            else context.user_id
        )
    return runtime.issue_graph_read_scope(
        context, owner_id=owner, zone="metadata" if owner == GLOBAL_OWNER_ID else None
    )


router = APIRouter(prefix="/api/v1/admin/explorer", tags=["explorer"])
explore_router = APIRouter(prefix="/api/v3/cm", tags=["explorer"])


@router.post("/global-graph")
async def global_graph(
    body: GlobalGraphRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        params = body.model_dump(exclude={"owner_id", "skeleton"})
        if scope.zone == "metadata":
            params.update(zone_mode="metadata")
        return success(runtime.explorer(scope).global_graph(scope, **params))

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/domain-graph")
async def domain_graph(
    body: DomainGraphRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        return success(
            runtime.explorer(scope).domain_graph(
                scope, body.domain.strip(), body.datasource_id.strip()
            )
        )

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/expand-node")
async def expand_node(
    body: ExpandNodeRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        return success(
            runtime.explorer(scope).expand(
                scope,
                body.key.strip(),
                limit=body.limit,
                zone=body.zone,
                label=body.label,
            )
        )

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/expand-layer")
async def expand_layer(
    body: ExpandLayerRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        return success(
            runtime.explorer(scope).expand(
                scope, body.key.strip(), limit=body.limit, direction=body.direction
            )
        )

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/search-nodes")
async def search_nodes(
    body: SearchNodesRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        return success(
            runtime.explorer(scope).search_nodes(scope, body.query, body.limit)
        )

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/search-subgraph")
async def search_subgraph(
    body: SearchSubgraphRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        query = body.query.strip()
        if not query:
            return success({"hit_nodes": [], "nodes": [], "edges": []})
        return success(
            runtime.explorer(scope).search_subgraph(
                scope,
                query,
                zones=tuple(body.scope or ["metadata", "knowledge"]),
                exact=body.match_mode == "exact",
                hops=body.hops,
                limit=body.limit,
            )
        )

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.get("/schema")
async def schema_summary(
    runtime: RuntimeDependency, context: AuthorizationDependency, owner_id: str = ""
):
    def read():
        scope = viewpoint(runtime, context, owner_id)
        return success(runtime.explorer(scope).schema_summary(scope))

    with graph_errors():
        return await runtime.run_graph_read(read)


# Specific suffix before the greedy key path, matching develop's route order.
@router.get("/nodes/{key:path}/cross-graph")
async def cross_graph(
    key: str,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    limit: int = Query(50, ge=1, le=500),
    owner_id: str = "",
):
    def read():
        scope = viewpoint(runtime, context, owner_id)
        return success(runtime.explorer(scope).cross_graph(scope, key, limit))

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.get("/nodes/{key:path}")
async def node_detail(
    key: str,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    owner_id: str = "",
):
    def read():
        scope = viewpoint(runtime, context, owner_id)
        return success(runtime.explorer(scope).node_detail(scope, key))

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/edge-detail")
async def edge_detail(
    body: EdgeDetailRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
):
    def read():
        scope = viewpoint(runtime, context, body.owner_id)
        return success(
            runtime.explorer(scope).edge_detail(
                scope, body.source_key, body.target_key, body.rel_type
            )
        )

    with graph_errors():
        return await runtime.run_graph_read(read)


@router.post("/topics")
async def topics(
    body: TopicsRequest, runtime: RuntimeDependency, context: AuthorizationDependency
):
    def prepare():
        scope = viewpoint(runtime, context, body.owner_id)
        return scope, runtime.explorer_topics(scope), runtime.models(context)

    with graph_errors():
        scope, jobs, models = await runtime.run_graph_read(prepare)
        result = await jobs.compute(
            scope,
            models,
            recompute=body.recompute,
            wait=body.wait,
            lang=body.lang,
        )
        if result["status"] == "error":
            raise HTTPException(500, failure("TOPICS_COMPUTE_FAILED", result["error"]))
        return success(result)


@router.get("/topics")
def topics_status(
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
    owner_id: str = "",
    lang: str = "",
):
    with graph_errors():
        scope = viewpoint(runtime, context, owner_id)
        return success(runtime.explorer_topics(scope).status(scope, lang))


@explore_router.post("/explore")
async def explore(
    body: ExploreRequest, runtime: RuntimeDependency, context: AuthorizationDependency
):
    query = body.query.strip()
    if not query:
        raise HTTPException(422, "query is required")

    def prepare():
        # The agent explores its caller's graph, not an administrator's default
        # global canvas. Resolve once before streaming to pin this operation.
        scope = runtime.issue_graph_read_scope(context, owner_id=context.user_id)
        service = runtime.explorer(scope)
        config = runtime.models(context)
        return prepare_exploration(
            service, scope, config, query, max_rounds=body.max_rounds, lang=body.lang
        )

    with graph_errors():
        stream = await runtime.run_graph_read(prepare)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

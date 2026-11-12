"""Preserve App 0.3 tool schemas through authenticated CM REST projections."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

import httpx
from fastapi import FastAPI, Request
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from ..utils.redaction import redact_credentials
from .graph import ResolveContext

LEGACY_TOOL_NAMES = frozenset({
    "search_context", "search_event", "explore_entity", "execute_sql", "recall_experience",
    "list_domains", "get_domain_overview", "list_metrics", "get_metric", "get_north_star_metrics",
    "search_metrics", "list_dimensions", "get_dimension", "get_dimension_hierarchy",
    "get_dimension_values", "list_dimensions_of_metric", "list_metrics_of_dimension",
    "list_datasets", "get_dataset", "get_dataset_columns",
})
_FORWARDED_HEADERS = frozenset({
    b"authorization", b"cookie", b"origin", b"referer", b"sec-fetch-site",
    b"x-qwenpaw-data-api-token", b"x-qwenpaw-data-service-secret", b"x-user-id",
    b"x-qwenpaw-model-binding", b"x-cm-host-session-id", b"x-cm-host-chat-id",
})


def _object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _params(metadata, session_ref: str = "", **selection) -> dict:
    result = dict(selection)
    meta = _object(metadata)
    ref = session_ref.strip() or str(meta.get("session_ref") or "").strip()
    datasource_id = str(meta.get("datasource_id") or meta.get("ds_id") or "").strip()
    if ref:
        result["session_ref"] = ref
    if datasource_id:
        result["datasource_id"] = datasource_id
    return result


def register_legacy_tools(mcp: FastMCP, resolve_context: ResolveContext, application: FastAPI) -> None:
    async def dispatch(ctx: Context, tool_name: str, method: str, path: str, payload: dict) -> str:
        resolve_context(ctx, tool_name)
        request = ctx.request_context.request
        if not isinstance(request, Request):
            raise ToolError("An authenticated HTTP request is required")
        headers = [(key.decode("latin-1"), value.decode("latin-1"))
                   for key, value in request.headers.raw if key.lower() in _FORWARDED_HEADERS]
        transport = httpx.ASGITransport(app=application, client=request.scope.get("client"), raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url=str(request.base_url), headers=headers, timeout=60,
        ) as client:
            options = {"json": payload} if method == "POST" else {"params": payload}
            operation = asyncio.create_task(client.request(method, f"/api/v1/cm/{path}", **options))
            try:
                while True:
                    done, _ = await asyncio.wait({operation}, timeout=20)
                    if done:
                        response = await operation
                        break
                    await ctx.report_progress(progress=0, total=None, message=f"{tool_name} is running")
            finally:
                if not operation.done():
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
        if not response.is_success:
            try:
                detail = response.json().get("detail", f"HTTP {response.status_code}")
            except (ValueError, AttributeError):
                detail = f"HTTP {response.status_code}"
            raise ToolError(redact_credentials(f"CM {path} [{response.status_code}]: {detail}"))
        return json.dumps(response.json(), ensure_ascii=False, default=str)

    @mcp.tool()
    async def search_context(
        query: str, ctx: Context, session_ref: str = "", scope: str = "{}", domain: str = "",
        include_operation: bool = False, include_debug: bool = False, metadata: str = "{}",
        relevance_threshold: float = 0.0,
    ) -> str:
        """Resolve a question into semantic metrics, dimensions, datasets and relevant experience."""
        body = _params(metadata, session_ref, query=query, stream=False)
        selected = _object(scope)
        if domain:
            selected["domain"] = domain
        if selected:
            body["scope"] = selected
        if include_operation:
            body["include_operation"] = True
        if include_debug:
            body["include_debug"] = True
        if relevance_threshold > 0:
            body["relevance_threshold"] = relevance_threshold
        return await dispatch(ctx, "search_context", "POST", "search_context", body)

    @mcp.tool()
    async def search_event(
        query: str, ctx: Context, limit: int = 10, relevance_threshold: float = 0.4, metadata: str = "{}",
    ) -> str:
        """Retrieve relevant knowledge-graph events."""
        return await dispatch(ctx, "search_event", "POST", "search_event", {
            "query": query, "limit": limit, "relevance_threshold": relevance_threshold,
        })

    @mcp.tool()
    async def explore_entity(
        entity_name: str, ctx: Context, session_ref: str = "", domain: str = "",
        relevance_threshold: float = 0.4, metadata: str = "{}",
    ) -> str:
        """Explore an entity's semantic, knowledge and user-memory context."""
        body = _params(metadata, session_ref, entity_name=entity_name, relevance_threshold=relevance_threshold)
        if domain:
            body["domain"] = domain
        return await dispatch(ctx, "explore_entity", "POST", "explore_entity", body)

    @mcp.tool()
    async def execute_sql(
        sql: str, ctx: Context, session_ref: str = "", domain: str = "", max_rows: int = 2000,
        slow_ms_threshold: float = 8000.0, metadata: str = "{}", dataset_names: list[str] | None = None,
    ) -> str:
        """Execute a read-only query with a bounded preview, truncation flag and authenticated CSV download."""
        body = _params(metadata, session_ref, sql=sql, max_rows=max_rows, slow_ms_threshold=slow_ms_threshold)
        if dataset_names is not None:
            body["dataset_names"] = dataset_names
        elif "dataset_names" in _object(metadata):
            body["dataset_names"] = _object(metadata)["dataset_names"]
        return await dispatch(ctx, "execute_sql", "POST", "execute_sql", body)

    @mcp.tool()
    async def recall_experience(ctx: Context, session_ref: str = "", focus: str = "{}", metadata: str = "{}") -> str:
        """Retrieve relevant experience from anchored session context."""
        body = _params(metadata, session_ref)
        if _object(focus):
            body["focus"] = _object(focus)
        return await dispatch(ctx, "recall_experience", "POST", "recall_experience", body)

    @mcp.tool()
    async def list_domains(ctx: Context, metadata: str = "{}") -> str:
        """List the authorized business domains."""
        return await dispatch(ctx, "list_domains", "GET", "domains", _params(metadata))

    @mcp.tool()
    async def get_domain_overview(domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a domain overview of metrics, dimensions and datasets."""
        params = _params(metadata, session_ref)
        if domain:
            params["domain"] = domain
        return await dispatch(ctx, "get_domain_overview", "GET", "domain-overview", params)

    @mcp.tool()
    async def list_metrics(domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """List metric summaries in a domain."""
        return await dispatch(ctx, "list_metrics", "GET", "metrics", _params(metadata, session_ref, domain=domain))

    @mcp.tool()
    async def get_metric(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get metric details, including ambiguity candidates."""
        return await dispatch(ctx, "get_metric", "GET", "metrics", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def get_north_star_metrics(domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a domain's north-star metrics."""
        return await dispatch(ctx, "get_north_star_metrics", "GET", "north-star-metrics", _params(metadata, session_ref, domain=domain))

    @mcp.tool()
    async def search_metrics(
        query: str, domain: str, ctx: Context, session_ref: str = "", k: int = 10, metadata: str = "{}",
    ) -> str:
        """Retrieve matching metrics, or an explicit no-match/low-confidence result."""
        return await dispatch(ctx, "search_metrics", "GET", "search-metrics", _params(metadata, session_ref, query=query, domain=domain, k=k))

    @mcp.tool()
    async def list_dimensions(domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """List dimension summaries in a domain."""
        return await dispatch(ctx, "list_dimensions", "GET", "dimensions", _params(metadata, session_ref, domain=domain))

    @mcp.tool()
    async def get_dimension(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get dimension details."""
        return await dispatch(ctx, "get_dimension", "GET", "dimensions", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def get_dimension_hierarchy(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a dimension's parent/child hierarchy."""
        return await dispatch(ctx, "get_dimension_hierarchy", "GET", "dimension-hierarchy", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def get_dimension_values(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a dimension's declared values."""
        return await dispatch(ctx, "get_dimension_values", "GET", "dimension-values", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def list_dimensions_of_metric(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a metric's dimension bindings."""
        return await dispatch(ctx, "list_dimensions_of_metric", "GET", "metric-dimensions", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def list_metrics_of_dimension(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get metrics that can be analyzed by a dimension."""
        return await dispatch(ctx, "list_metrics_of_dimension", "GET", "dimension-metrics", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def list_datasets(domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """List dataset summaries in a domain."""
        return await dispatch(ctx, "list_datasets", "GET", "datasets", _params(metadata, session_ref, domain=domain))

    @mcp.tool()
    async def get_dataset(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a dataset schema and description."""
        return await dispatch(ctx, "get_dataset", "GET", "datasets", _params(metadata, session_ref, name=name, domain=domain))

    @mcp.tool()
    async def get_dataset_columns(name: str, domain: str, ctx: Context, session_ref: str = "", metadata: str = "{}") -> str:
        """Get a dataset's column definitions."""
        return await dispatch(ctx, "get_dataset_columns", "GET", f"datasets/{quote(name, safe='')}/columns", _params(metadata, session_ref, domain=domain))

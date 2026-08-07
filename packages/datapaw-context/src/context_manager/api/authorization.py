"""Fail-closed route authorization policy.

Every protected API route is listed explicitly. Runtime matching supports only
the path parameters present in those registered templates; a newly added route
therefore receives 403 until it is deliberately classified. Tests validate the
application's complete route inventory against this table.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from .auth import SCOPE_CREDENTIALS, SCOPE_MANAGE, SCOPE_QUERY, SCOPE_WRITE

ScopeSet = frozenset[str]
RouteKey = tuple[str, str]


def _normalize_method(method: str) -> str:
    value = method.upper()
    return "GET" if value == "HEAD" else value


def _normalize_path(path: str) -> str:
    value = path or "/"
    return value if value == "/" else value.rstrip("/")


ROUTE_SCOPE_POLICIES: dict[RouteKey, ScopeSet] = {}


def _register(scopes: Iterable[str], *routes: RouteKey) -> None:
    required = frozenset(scopes)
    for method, path in routes:
        key = (_normalize_method(method), _normalize_path(path))
        if key in ROUTE_SCOPE_POLICIES:
            raise RuntimeError(f"duplicate authorization policy: {key}")
        ROUTE_SCOPE_POLICIES[key] = required


_register(
    (),
    ("GET", "/api/auth/check"),
)

_register(
    (SCOPE_QUERY,),
    ("GET", "/api/rds_import_pipeline"),
    ("GET", "/api/domains"),
    ("POST", "/api/resolve"),
    ("POST", "/api/expand"),
    ("POST", "/api/execute_sql"),
    ("GET", "/api/v1/semantic/domains"),
    ("GET", "/api/v1/semantic/metrics"),
    ("GET", "/api/v1/semantic/metrics/search"),
    ("GET", "/api/v1/semantic/metrics/north-star"),
    ("GET", "/api/v1/semantic/metrics/for-dimension"),
    ("GET", "/api/v1/semantic/metrics/{metric_name}"),
    ("GET", "/api/v1/semantic/dimensions"),
    ("GET", "/api/v1/semantic/dimensions/for-metric"),
    ("GET", "/api/v1/semantic/dimensions/{dim_name}"),
    ("GET", "/api/v1/semantic/dimensions/{dim_name}/hierarchy"),
    ("GET", "/api/v1/semantic/dimensions/{dim_name}/values"),
    ("GET", "/api/v1/semantic/datasets"),
    ("GET", "/api/v1/semantic/datasets/{name}/columns"),
    ("GET", "/api/v1/semantic/datasets/{name}/schema"),
    ("GET", "/api/v1/cm/mcp/tools"),
    ("GET", "/api/v1/cm/domains"),
    ("GET", "/api/v1/cm/domain-overview"),
    ("GET", "/api/v1/cm/metrics"),
    ("GET", "/api/v1/cm/north-star-metrics"),
    ("GET", "/api/v1/cm/search-metrics"),
    ("GET", "/api/v1/cm/dimensions"),
    ("GET", "/api/v1/cm/dimension-hierarchy"),
    ("GET", "/api/v1/cm/dimension-values"),
    ("GET", "/api/v1/cm/metric-dimensions"),
    ("GET", "/api/v1/cm/dimension-metrics"),
    ("GET", "/api/v1/cm/datasets"),
    ("POST", "/api/v1/cm/search_context"),
    ("POST", "/api/v1/cm/search_event"),
    ("POST", "/api/v1/cm/explore_entity"),
    ("GET", "/api/v1/cm/downloads/{filename}"),
    ("POST", "/api/v1/cm/execute_sql"),
    ("POST", "/api/v1/cm/execute_sql_stream"),
    ("POST", "/api/v1/cm/recall_experience"),
    ("GET", "/api/v1/cm/datasources"),
    ("GET", "/api/v1/connect/status/{task_id}"),
    ("GET", "/api/v1/connect/tasks"),
    ("GET", "/api/v1/docs"),
    ("GET", "/api/v1/docs/{doc_id:path}/download"),
    ("GET", "/api/v1/traces"),
    ("GET", "/api/v1/traces/{task_key:path}/graph"),
    ("GET", "/api/v1/traces/{task_key:path}/download"),
    ("GET", "/api/v1/semantic/export"),
    ("POST", "/api/v1/semantic/import/preview"),
    ("GET", "/api/v1/admin/mg/domains"),
    ("GET", "/api/v1/admin/mg/domains/{name}"),
    ("GET", "/api/v1/admin/mg/metrics"),
    ("GET", "/api/v1/admin/mg/metrics/{metric_name}"),
    ("GET", "/api/v1/admin/mg/dimensions"),
    ("GET", "/api/v1/admin/mg/dimensions/{dim_name}"),
    ("GET", "/api/v1/admin/mg/datasets"),
    ("GET", "/api/v1/admin/mg/datasets/{dataset_name}"),
    ("GET", "/api/v1/admin/mg/nodes/{key:path}/edges"),
    ("GET", "/api/v1/admin/kg/entities"),
    ("GET", "/api/v1/admin/kg/entities/{key:path}"),
    ("GET", "/api/v1/admin/kg/events"),
    ("GET", "/api/v1/admin/kg/events/{key:path}"),
    ("GET", "/api/v1/admin/kg/edges/rel-types"),
    ("GET", "/api/v1/admin/tg/tasks"),
    ("GET", "/api/v1/admin/tg/tasks/{key:path}/graph"),
    ("GET", "/api/v1/admin/tg/claims"),
    ("GET", "/api/v1/admin/tg/tasks/{key:path}/claims"),
    ("GET", "/api/v1/admin/tg/strategies"),
    ("GET", "/api/v1/admin/tg/strategies/{key:path}"),
    ("GET", "/api/v1/admin/tg/experiences"),
    ("GET", "/api/v1/admin/tg/experiences/{key:path}"),
    ("GET", "/api/v1/admin/tg/tags"),
    ("GET", "/api/v1/users/{user_id}/context/{section}"),
    ("POST", "/api/v1/admin/explorer/global-graph"),
    ("POST", "/api/v1/admin/explorer/domain-graph"),
    ("POST", "/api/v1/admin/explorer/expand-node"),
    ("POST", "/api/v1/admin/explorer/expand-layer"),
    ("POST", "/api/v1/admin/explorer/search-nodes"),
    ("GET", "/api/v1/admin/explorer/schema"),
    ("GET", "/api/v1/admin/explorer/nodes/{key:path}/cross-graph"),
    ("GET", "/api/v1/admin/explorer/nodes/{key:path}"),
    ("POST", "/api/v1/admin/explorer/edge-detail"),
    ("POST", "/api/v1/admin/explorer/search-subgraph"),
    ("GET", "/api/semantic-config/biz-domain"),
    ("GET", "/api/semantic-config/biz-domain/{domain_id}"),
    ("GET", "/api/semantic-config/dataset-meta"),
    ("GET", "/api/semantic-config/dataset-meta/{dataset_id}"),
    ("GET", "/api/semantic-config/dataset-column-meta"),
    ("GET", "/api/semantic-config/dataset-column-meta/dataset/{dataset_id}"),
    ("GET", "/api/semantic-config/dataset-column-meta/{col_id}"),
    ("GET", "/api/semantic-config/dimension"),
    ("GET", "/api/semantic-config/dimension/{dim_id}"),
    ("GET", "/api/semantic-config/dataset-dimension"),
    ("GET", "/api/semantic-config/dataset-dimension/dataset/{dataset_id}"),
    ("GET", "/api/semantic-config/dataset-dimension/{dd_id}"),
    ("GET", "/api/semantic-config/metric-lib"),
    ("GET", "/api/semantic-config/metric-lib/{metric_id}"),
    ("GET", "/api/semantic-config/metric-formula-lib"),
    ("GET", "/api/semantic-config/metric-formula-lib/dataset/{dataset_id}"),
    ("GET", "/api/semantic-config/metric-formula-lib/{fid}"),
    ("GET", "/api/semantic-config/weave-task"),
    ("GET", "/mcp/v1/cm"),
    ("POST", "/mcp/v1/cm"),
    ("DELETE", "/mcp/v1/cm"),
)

_register(
    (SCOPE_WRITE,),
    ("POST", "/api/feedback"),
    ("POST", "/api/v1/semantic/import"),
    ("POST", "/api/v1/docs/upload"),
    ("DELETE", "/api/v1/docs/{doc_id:path}"),
    ("POST", "/api/v1/traces/ingest"),
    ("POST", "/api/v1/traces/ingest_file"),
    ("POST", "/api/v1/trace/submit_trace"),
    ("POST", "/api/v1/semantic/import/confirm"),
    ("POST", "/api/v1/semantic/import/from-excel"),
    ("POST", "/api/v1/semantic/import/from-excel/apply"),
    ("POST", "/api/v1/users/{user_id}/context"),
    ("PUT", "/api/v1/users/{user_id}/context/{section}"),
    ("DELETE", "/api/v1/users/{user_id}/context/{section}"),
    ("POST", "/api/semantic-config/weave-task/submit"),
    ("POST", "/api/semantic-config/weave-task/callback"),
)

_register(
    (SCOPE_MANAGE,),
    ("GET", "/api/monitor_export_log"),
    ("POST", "/api/monitor_export_log"),
    ("POST", "/api/admin/reset_memory"),
    ("GET", "/api/admin/logs/access"),
    ("POST", "/api/v1/semantic/admin/dataset"),
    ("POST", "/api/v1/semantic/admin/dimension-values"),
    ("POST", "/api/v1/semantic/admin/dimension"),
    ("POST", "/api/v1/semantic/admin/metric"),
    ("POST", "/api/v1/semantic/admin/domain"),
    ("POST", "/api/v1/semantic/admin/import/yaml"),
    ("POST", "/api/v1/semantic/admin/import/excel"),
    ("DELETE", "/api/v1/semantic/admin/domain/{name}"),
    ("DELETE", "/api/v1/semantic/admin/dataset/{name}"),
    ("DELETE", "/api/v1/semantic/admin/dimension-values/{dim_name}"),
    ("DELETE", "/api/v1/semantic/admin/dimension/{name}"),
    ("DELETE", "/api/v1/semantic/admin/metric/{name}"),
    ("PUT", "/api/v1/cm/datasources/active"),
    ("PUT", "/api/datasources/active"),
    ("GET", "/api/system/model-config/embedding/jobs/latest"),
    ("GET", "/api/system/model-config/embedding/jobs/{job_id}"),
    ("POST", "/api/system/model-config/embedding/jobs/{job_id}/retry"),
    ("POST", "/api/v1/admin/kg/entities/batch-delete"),
    ("PUT", "/api/v1/admin/kg/entities/{key:path}"),
    ("DELETE", "/api/v1/admin/kg/entities/{key:path}"),
    ("PUT", "/api/v1/admin/kg/events/{key:path}"),
    ("DELETE", "/api/v1/admin/kg/events/{key:path}"),
    ("POST", "/api/v1/admin/kg/edges/related-to"),
    ("DELETE", "/api/v1/admin/kg/edges/related-to"),
    ("POST", "/api/v1/admin/kg/edges/about"),
    ("POST", "/api/v1/admin/kg/edges/cross-graph"),
    ("DELETE", "/api/v1/admin/kg/edges/cross-graph"),
    ("PATCH", "/api/v1/admin/kg/edges/properties"),
    ("DELETE", "/api/v1/admin/kg/edges/adjacent"),
    ("DELETE", "/api/v1/admin/kg/edges/by-type"),
    ("POST", "/api/v1/admin/kg/edges/purge-type-global"),
    ("PATCH", "/api/v1/admin/tg/tasks/{key:path}/status"),
    ("POST", "/api/v1/admin/tg/tasks/batch-archive"),
    ("DELETE", "/api/v1/admin/tg/tasks/{key:path}"),
    ("POST", "/api/v1/admin/tg/tasks/batch-delete"),
    ("PATCH", "/api/v1/admin/tg/claims/{key:path}"),
    ("POST", "/api/v1/admin/tg/claims/{key:path}/invalidate"),
    ("PATCH", "/api/v1/admin/tg/strategies/{key:path}"),
    ("POST", "/api/v1/admin/tg/strategies/{key:path}/invalidate"),
    ("POST", "/api/v1/admin/cypher"),
    ("POST", "/api/semantic-config/biz-domain"),
    ("PUT", "/api/semantic-config/biz-domain/{domain_id}"),
    ("DELETE", "/api/semantic-config/biz-domain/{domain_id}"),
    ("POST", "/api/semantic-config/dataset-meta"),
    ("PUT", "/api/semantic-config/dataset-meta/{dataset_id}"),
    ("DELETE", "/api/semantic-config/dataset-meta/{dataset_id}"),
    ("POST", "/api/semantic-config/dataset-column-meta"),
    ("PUT", "/api/semantic-config/dataset-column-meta/{col_id}"),
    ("DELETE", "/api/semantic-config/dataset-column-meta/{col_id}"),
    ("POST", "/api/semantic-config/dimension"),
    ("PUT", "/api/semantic-config/dimension/{dim_id}"),
    ("DELETE", "/api/semantic-config/dimension/{dim_id}"),
    ("POST", "/api/semantic-config/dataset-dimension"),
    ("PUT", "/api/semantic-config/dataset-dimension/{dd_id}"),
    ("DELETE", "/api/semantic-config/dataset-dimension/dataset/{dataset_id}"),
    ("DELETE", "/api/semantic-config/dataset-dimension/{dd_id}"),
    ("POST", "/api/semantic-config/metric-lib"),
    ("PUT", "/api/semantic-config/metric-lib/{metric_id}"),
    ("DELETE", "/api/semantic-config/metric-lib/{metric_id}"),
    ("POST", "/api/semantic-config/metric-formula-lib"),
    ("PUT", "/api/semantic-config/metric-formula-lib/{fid}"),
    ("DELETE", "/api/semantic-config/metric-formula-lib/dataset/{dataset_id}"),
    ("DELETE", "/api/semantic-config/metric-formula-lib/{fid}"),
    ("POST", "/api/semantic-config/import/excel"),
    ("POST", "/api/semantic-config/weave-task/{task_id}/kill"),
)

_register(
    (SCOPE_CREDENTIALS,),
    ("GET", "/api/system/model-config"),
    ("PUT", "/api/system/model-config/llm"),
    ("POST", "/api/system/model-config/llm/test"),
    ("PUT", "/api/system/model-config/embedding"),
    ("POST", "/api/system/model-config/embedding/test"),
    ("GET", "/api/semantic-config/datasource"),
    ("POST", "/api/semantic-config/datasource"),
    ("POST", "/api/semantic-config/datasource/test-connection"),
    ("GET", "/api/semantic-config/datasource/{datasource_id}"),
    ("PUT", "/api/semantic-config/datasource/{datasource_id}"),
    ("POST", "/api/semantic-config/datasource/{datasource_id}/test-connection"),
    ("DELETE", "/api/semantic-config/datasource/{datasource_id}"),
    ("POST", "/api/v1/connect/test-connection"),
)

_register(
    (SCOPE_WRITE, SCOPE_CREDENTIALS),
    ("POST", "/api/v1/connect"),
)


_PARAM_RE = re.compile(r"\{[^{}:]+(?::(path))?\}")


def _compile_template(template: str) -> re.Pattern[str]:
    parts: list[str] = []
    cursor = 0
    for match in _PARAM_RE.finditer(template):
        parts.append(re.escape(template[cursor : match.start()]))
        parts.append(r".+" if match.group(1) == "path" else r"[^/]+")
        cursor = match.end()
    parts.append(re.escape(template[cursor:]))
    return re.compile("^" + "".join(parts) + "$")


_RUNTIME_POLICIES = tuple(
    (method, _compile_template(template), scopes)
    for (method, template), scopes in ROUTE_SCOPE_POLICIES.items()
)


def required_scopes_for_request(method: str, path: str) -> ScopeSet | None:
    """Return required scopes, or ``None`` when the route is unclassified."""
    normalized_method = _normalize_method(method)
    normalized_path = _normalize_path(path)
    exact = ROUTE_SCOPE_POLICIES.get((normalized_method, normalized_path))
    if exact is not None:
        return exact
    for policy_method, pattern, scopes in _RUNTIME_POLICIES:
        if policy_method == normalized_method and pattern.fullmatch(normalized_path):
            return scopes
    return None


def _walk_route_templates(routes: Iterable[Any], prefix: str = ""):
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            child_prefix = prefix + (getattr(context, "prefix", "") or "")
            yield from _walk_route_templates(included.routes, child_prefix)
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path and methods:
            yield prefix + path, methods


def unclassified_app_routes(app: Any) -> list[str]:
    """List API method/templates missing a policy (used by CI and reviews)."""
    missing: list[str] = []
    from .auth import AUTH_EXEMPT_PATHS

    for path, methods in _walk_route_templates(app.routes):
        normalized_path = _normalize_path(path)
        if not (normalized_path.startswith("/api/") or normalized_path.startswith("/mcp/")):
            continue
        if normalized_path in AUTH_EXEMPT_PATHS:
            continue
        for method in methods:
            if method == "OPTIONS":
                continue
            key = (_normalize_method(method), normalized_path)
            if key not in ROUTE_SCOPE_POLICIES:
                missing.append(f"{method} {path}")
    return sorted(set(missing))


__all__ = [
    "ROUTE_SCOPE_POLICIES",
    "required_scopes_for_request",
    "unclassified_app_routes",
]

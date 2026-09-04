"""MCP server for the unified CM API.

Environment:
    CM_API_BASE_URL — default ``http://127.0.0.1:8100``
    CM_API_TIMEOUT — seconds, default 60
    CM_KEEPALIVE_SECONDS — MCP progress heartbeat interval, default 10
    NEO4J_DATABASE_MCP — MCP 专用逻辑库（如 ``appdata``）；回退 ``NEO4J_DATABASE``
    NEO4J_DATABASE — 与上项二选一；未设 ``NEO4J_DATABASE_MCP`` 时作为 MCP 库

Stdio:
    python -m context_manager.mcp.cm_server

Streamable HTTP (standalone):
    CM_MCP_TRANSPORT=streamable-http python -m context_manager.mcp.cm_server

When embedded in the topology web app, ``create_app()`` mounts at ``/mcp/v1/cm``.

Streamable HTTP is **stateless**: ``tools/list`` and ``tools/call`` need no
``initialize`` or ``Mcp-Session-Id`` (one POST per JSON-RPC call).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Awaitable, Callable, Optional, TypeVar
from urllib.parse import quote

import httpx

try:
    from mcp.server.fastmcp import Context, FastMCP
    from mcp.server.auth.middleware.auth_context import get_access_token
except ImportError as exc:
    raise ImportError("MCP package not installed. Run: pip install mcp") from exc

log = logging.getLogger("mcp.cm_server")

from context_manager.config import resolve_neo4j_database
from context_manager.mcp.http_common import (
    create_streamable_mcp,
    is_loopback_bind_host,
    mount_streamable_http as _mount,
)
from context_manager.mcp.harness_tool import patch_mcp_tool_manager

BASE_URL = os.environ.get("CM_API_BASE_URL", "http://127.0.0.1:8100").rstrip("/")
TIMEOUT = float(os.environ.get("CM_API_TIMEOUT", "60"))
KEEPALIVE_SECONDS = float(os.environ.get("CM_KEEPALIVE_SECONDS", "20"))
PREFIX = "/api/v1/cm"

mcp = create_streamable_mcp("context_manager")
patch_mcp_tool_manager(mcp)

_T = TypeVar("_T")


def _headers() -> dict[str, str]:
    from context_manager.api.auth import get_client_api_token

    h: dict[str, str] = {}
    access_token = get_access_token()
    token = (
        (access_token.token if access_token is not None else "")
        or get_client_api_token()
    )
    if token:
        h["Authorization"] = f"Bearer {token}"
    db = (
        resolve_neo4j_database(role="mcp")
        or (os.environ.get("X_NEO4J_DATABASE") or "").strip()
        or None
    )
    if db:
        h["x-neo4j-database"] = db
    return h


def _async_client() -> httpx.AsyncClient:
    """Return an async client — uses ASGI transport if app is set, else HTTP."""
    app = getattr(mcp, "_cm_app", None)
    if app is not None:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        return httpx.AsyncClient(
            transport=transport,
            # Keep in-process requests inside the same strict Host allowlist as
            # network requests; a synthetic hostname would be rejected.
            base_url="http://127.0.0.1",
            timeout=TIMEOUT,
            headers=_headers(),
        )
    return httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=TIMEOUT,
        headers=_headers(),
    )


def _raise_upstream(path: str, resp: httpx.Response) -> None:
    """Surface upstream {"detail": "..."} bodies instead of bare HTTP status codes."""
    if resp.is_success:
        return
    try:
        body = resp.json()
        detail = body.get("detail") if isinstance(body, dict) else body
    except Exception:
        detail = resp.text[:500] if resp.text else f"HTTP {resp.status_code}"
    raise RuntimeError(f"{path} [{resp.status_code}]: {detail}")


async def _post(path: str, body: dict[str, Any]) -> Any:
    async with _async_client() as client:
        resp = await client.post(f"{PREFIX}/{path.lstrip('/')}", json=body)
        _raise_upstream(path, resp)
        return resp.json()


async def _get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    async with _async_client() as client:
        resp = await client.get(f"{PREFIX}/{path.lstrip('/')}", params=clean)
        _raise_upstream(path, resp)
        return resp.json()


# ---------------------------------------------------------------------- #
# Keepalive: periodic MCP progress heartbeat during long sync calls
# ---------------------------------------------------------------------- #

async def _with_keepalive(
    coro: Awaitable[_T],
    ctx: Context,
    *,
    label: str = "CM",
    interval: float = KEEPALIVE_SECONDS,
) -> _T:
    """Run *coro* while periodically sending MCP progress keep-alive.

    MCP clients receive ``notifications/progress`` and can use
    them to reset timeouts or show a "still working" indicator.
    """
    task = asyncio.ensure_future(coro)
    seq = 0

    async def _heartbeat() -> None:
        nonlocal seq
        while not task.done():
            await asyncio.sleep(interval)
            if task.done():
                break
            seq += 1
            try:
                await ctx.report_progress(
                    progress=seq,
                    message=f"{label} 执行中…",
                )
            except Exception:
                log.debug("keepalive report_progress failed", exc_info=True)

    hb_task = asyncio.ensure_future(_heartbeat())
    try:
        return await task
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------- #
# Harness-injected out-of-band metadata
# ---------------------------------------------------------------------- #
# 所有 tool 统一接受可选 ``metadata``（JSON 对象字符串或对象），由 agent harness
# 在每次调用时注入（LLM 无需理解/填写），用于 dialog 级别的上下文绑定。
# 未识别的 key 忽略。显式 tool 参数优先于 metadata 中的同名值。
# metadata 约定字段：datasource_id、session_ref（均由 harness 注入，不出现在 tools/list schema）。

def _parse_json_object(value: Any) -> dict[str, Any]:
    """Accept JSON object as str (harness) or dict (JSON-RPC callers)."""
    if isinstance(value, dict):
        return value
    if not value or value == "{}":
        return {}
    if isinstance(value, str):
        try:
            obj = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return obj if isinstance(obj, dict) else {}
    return {}


def _parse_json_array(value: Any, *, comma_fallback: bool = False) -> list[Any]:
    """Accept JSON array as str or list."""
    if isinstance(value, list):
        return value
    if not value:
        return []
    if isinstance(value, str):
        try:
            obj = json.loads(value)
        except json.JSONDecodeError:
            if comma_fallback:
                return [x.strip() for x in value.split(",") if x.strip()]
            return []
        return obj if isinstance(obj, list) else []
    return []


def _parse_metadata(metadata: Any) -> dict[str, Any]:
    """Parse harness-injected metadata; tolerate empty / malformed input."""
    return _parse_json_object(metadata)


def _meta_datasource_id(metadata: Any, explicit: str = "") -> str:
    """Resolve the datasource to its canonical ``datasource_id``.

    Priority: explicit tool arg > metadata (``datasource_id`` > ``ds_id``).
    Normalize to the id at this single MCP boundary — the CM backend filters
    graph nodes by id and no longer maps int→code, so a raw numeric id would
    match no node. ``resolve_datasource_id`` is idempotent for ids and returns
    the input unchanged on failure.
    """
    ds = (explicit or "").strip()
    if not ds:
        meta = _parse_metadata(metadata)
        ds = str(
            meta.get("datasource_id")
            or meta.get("ds_id")
            or ""
        ).strip()
    if not ds:
        return ""
    try:
        from context_manager.api.datasource_active_api import resolve_datasource_id

        return resolve_datasource_id(ds) or ds
    except Exception:
        return ds


def _meta_session_ref(metadata: Any, explicit: str = "") -> str:
    """Resolve session_ref: explicit tool arg > metadata.session_ref."""
    ref = (explicit or "").strip()
    if ref:
        return ref
    return str(_parse_metadata(metadata).get("session_ref") or "").strip()


# ---------------------------------------------------------------------- #
# L1
# ---------------------------------------------------------------------- #

@mcp.tool()
async def search_context(
    query: str,
    ctx: Context,
    session_ref: str = "",
    scope: str = "{}",
    domain: str = "",
    include_operation: bool = False,
    include_debug: bool = False,
    metadata: str = "{}",
    relevance_threshold: float = 0.0,
) -> str:
    """将自然语言问句解析为结构化语义上下文（指标、维度、数据集、策略、相似经验）。

    执行期间通过 MCP progress 定期发送 keep-alive 心跳。

    返回的 ``relevance`` 字段用于判断 query 是否与知识库匹配：
    - ``status="relevant"``: 高置信度命中，可正常使用
    - ``status="low_confidence"``: 匹配度较低，建议拦截或提示用户
    - ``status="no_match"``: 完全无命中，应拦截并返回"没有相关信息"

    Args:
        query: 自然语言问句
        session_ref: 会话引用；通常由 harness 经 metadata.session_ref 注入（LLM 无需填写），显式 tool 参数优先于 metadata。
        scope: 业务范围限定，JSON 对象字符串，如 {"domain": "ChatApp", "as_of_date": "2024-03"}
        domain: 业务域名称（如 "ChatApp"）；与 scope 中的 domain 等价，二选一即可，同时传入时此参数优先
        include_operation: 为 true 时在结果中附带 operation（pipeline / assembly 步骤摘要）
        include_debug: 为 true 时在结果中附带 debug 诊断字段
        metadata: 由 harness 注入的带外上下文，JSON 对象字符串。支持 datasource_id、session_ref；显式 tool 参数优先于 metadata。LLM 无需填写。
        relevance_threshold: 相关度门槛（0~1，默认 0.40）。低于此值时 relevance.status 变为 low_confidence 或 no_match。传 0 表示使用默认值。
    """
    body: dict[str, Any] = {"query": query, "stream": False}
    ref = _meta_session_ref(metadata, session_ref)
    if ref:
        body["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        body["datasource_id"] = ds_id
    scope_dict = _parse_json_object(scope) if scope and scope != "{}" else {}
    if domain:
        scope_dict["domain"] = domain
    if scope_dict:
        body["scope"] = scope_dict
    if include_operation:
        body["include_operation"] = True
    if include_debug:
        body["include_debug"] = True
    if relevance_threshold > 0:
        body["relevance_threshold"] = relevance_threshold
    data = await _with_keepalive(_post("search_context", body), ctx, label="search_context")
    return _json(data)


# ---------------------------------------------------------------------- #
# L2
# ---------------------------------------------------------------------- #

@mcp.tool()
async def search_event(
    query: str,
    limit: int = 10,
    relevance_threshold: float = 0.4,
    metadata: str = "{}",
) -> str:
    """自然语言检索知识图 Event（节假日、发布、促销、缺陷等），默认返回 10 条。

    Args:
        query: 自然语言检索关键词，如 "春节促销"、"模型上线"
        limit: 返回条数上限，默认 10
        relevance_threshold: 相关度门槛（0~1，默认 0.4），低于此值的结果被过滤
    """
    body: dict[str, Any] = {
        "query": query,
        "limit": limit,
        "relevance_threshold": relevance_threshold,
    }
    return _json(await _post("search_event", body))


@mcp.tool()
async def explore_entity(
    entity_name: str,
    ctx: Context,
    session_ref: str = "",
    domain: str = "",
    relevance_threshold: float = 0.4,
    metadata: str = "{}",
) -> str:
    """跨 KG/MG/TG 探索实体全量上下文。

    执行期间通过 MCP progress 定期发送 keep-alive 心跳。
    """
    body: dict[str, Any] = {
        "entity_name": entity_name,
        "relevance_threshold": relevance_threshold,
    }
    ref = _meta_session_ref(metadata, session_ref)
    if ref:
        body["session_ref"] = ref
    if domain:
        body["domain"] = domain
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        body["datasource_id"] = ds_id
    data = await _with_keepalive(_post("explore_entity", body), ctx, label="explore_entity")
    return _json(data)


@mcp.tool()
async def execute_sql(
    sql: str,
    ctx: Context,
    session_ref: str = "",
    domain: str = "",
    max_rows: int = 2000,
    slow_ms_threshold: float = 8000.0,
    metadata: str = "{}",
) -> str:
    """在物理库上执行只读 SELECT 并返回结果。

    返回说明：
    - rows: 仅包含前 20 行预览，完整数据请通过 download_url 下载 CSV
    - truncated: 重要标识符，true 表示查询结果超过 max_rows（默认 2000），数据被截断。
      此时即使下载也无法获取完整结果，应当：
      (1) 添加更精确的 WHERE 条件缩小范围
      (2) 使用 LIMIT/OFFSET 分页查询
      (3) 使用聚合函数减少返回行数
    - download_url: 所有非空结果均提供此链接，用于下载完整 CSV

    执行后端由所选数据源（metadata.datasource_id）路由，无需手动指定连接信息。

    Args:
        sql: 要执行的 SELECT 语句
        session_ref: 会话引用；通常由 harness 经 metadata.session_ref 注入（LLM 无需填写），显式 tool 参数优先于 metadata
        domain: 业务域名称（可选，仅作上下文提示；数据源不再由 domain 推断）
        max_rows: 行数上限，默认 2000
        slow_ms_threshold: 慢查询阈值（毫秒），默认 8000
        metadata: 由 harness 注入的带外上下文，JSON 对象字符串。支持 datasource_id、session_ref；显式 tool 参数优先于 metadata。LLM 无需填写
    """
    body: dict[str, Any] = {
        "sql": sql,
        "max_rows": max_rows,
        "slow_ms_threshold": slow_ms_threshold,
    }
    ref = _meta_session_ref(metadata, session_ref)
    if ref:
        body["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        body["datasource_id"] = ds_id

    data = await _with_keepalive(_post("execute_sql", body), ctx, label="execute_sql")
    return _json(data)


@mcp.tool()
async def recall_experience(session_ref: str = "", focus: str = "{}", metadata: str = "{}") -> str:
    """基于已锚定上下文补捞历史经验。

    """
    body: dict[str, Any] = {}
    ref = _meta_session_ref(metadata, session_ref)
    if ref:
        body["session_ref"] = ref
    if focus and focus != "{}":
        focus_obj = _parse_json_object(focus)
        if focus_obj:
            body["focus"] = focus_obj
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        body["datasource_id"] = ds_id
    return _json(await _post("recall_experience", body))


# ---------------------------------------------------------------------- #
# L3
# ---------------------------------------------------------------------- #

@mcp.tool()
async def list_domains(metadata: str = "{}") -> str:
    """列出所有业务域。

    """
    params: dict[str, Any] = {}
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("domains", params))


@mcp.tool()
async def get_domain_overview(domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """获取业务域全景概览。

    返回该域下指标、维度、数据集的总体摘要，适合在分析开始时快速了解域的全貌。

    Args:
        domain: 业务域名称（必填），如 "ChatApp"
        session_ref: 会话引用；通常由 harness 经 metadata.session_ref 注入（LLM 无需填写），显式 tool 参数优先于 metadata
    """
    params: dict[str, Any] = {}
    if domain:
        params["domain"] = domain
    ref = _meta_session_ref(metadata, session_ref)
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("domain-overview", params))


@mcp.tool()
async def list_metrics(domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """列出指定域的全量指标摘要。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("metrics", params))


@mcp.tool()
async def get_metric(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """查询指标详情（支持歧义候选）。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("metrics", params))


@mcp.tool()
async def get_north_star_metrics(domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """获取北极星指标列表。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("north-star-metrics", params))


@mcp.tool()
async def search_metrics(
    query: str, domain: str, session_ref: str = "", k: int = 10, metadata: str = "{}",
) -> str:
    """语义检索指标（支持 name/aliases/tags 模糊匹配）。

    命中时返回 MetricSummary 数组；未命中或置信度不足时返回
    ``{"status": "no_match"|"low_confidence", "message": "...", "suggestion": "..."}``，
    调用方应据此提示用户或换词重试。
    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"query": query, "domain": domain, "k": k}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("search-metrics", params))


@mcp.tool()
async def list_dimensions(domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """列出指定域的全量维度摘要。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("dimensions", params))


@mcp.tool()
async def get_dimension(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """查询维度详情。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("dimensions", params))


@mcp.tool()
async def get_dimension_hierarchy(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """获取维度父子层级。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("dimension-hierarchy", params))


@mcp.tool()
async def get_dimension_values(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """获取维度枚举值。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("dimension-values", params))


@mcp.tool()
async def list_dimensions_of_metric(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """查询指标可拆解维度绑定。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("metric-dimensions", params))


@mcp.tool()
async def list_metrics_of_dimension(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """反向查询：根据维度名称获取可分析的指标列表。

    """
    ref = _meta_session_ref(metadata, session_ref)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    ds_id = _meta_datasource_id(metadata)
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("dimension-metrics", params))


@mcp.tool()
async def list_datasets(domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """列出指定域的数据集摘要。

    """
    ref = _meta_session_ref(metadata, session_ref)
    ds_id = _meta_datasource_id(metadata)
    params: dict[str, Any] = {"domain": domain}
    if ref:
        params["session_ref"] = ref
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("datasets", params))


@mcp.tool()
async def get_dataset(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """查询数据集 Schema。

    返回指定数据集的描述、类型（OLAP/维度表/事实表等）以及列定义（列名、类型、描述）等结构信息。

    Args:
        name: 数据集名称（必填）
        domain: 业务域名称（必填），如 "ChatApp"
        session_ref: 会话引用；通常由 harness 经 metadata.session_ref 注入（LLM 无需填写），显式 tool 参数优先于 metadata
    """
    ref = _meta_session_ref(metadata, session_ref)
    ds_id = _meta_datasource_id(metadata)
    params: dict[str, Any] = {"name": name, "domain": domain}
    if ref:
        params["session_ref"] = ref
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get("datasets", params))


@mcp.tool()
async def get_dataset_columns(name: str, domain: str, session_ref: str = "", metadata: str = "{}") -> str:
    """查询数据集的列定义列表。

    返回指定数据集的列元数据（列名、类型、描述等），比 get_dataset 更轻量，
    适用于只需要列信息的场景。

    Args:
        name: 数据集名称（必填）
        domain: 业务域名称（必填），如 "ChatApp"
        session_ref: 会话引用；通常由 harness 经 metadata.session_ref 注入（LLM 无需填写）
    """
    ref = _meta_session_ref(metadata, session_ref)
    ds_id = _meta_datasource_id(metadata)
    params: dict[str, Any] = {"domain": domain}
    if ref:
        params["session_ref"] = ref
    if ds_id:
        params["datasource_id"] = ds_id
    return _json(await _get(f"datasets/{quote(name, safe='')}/columns", params))



def mount_streamable_http(app: Any, *, path: str = "/mcp/v1/cm") -> Any:
    """Mount Streamable HTTP MCP on a FastAPI/Starlette app."""
    return _mount(app, mcp, path=path)


def standalone_http_app() -> Any:
    """Build standalone HTTP MCP with the same edge controls as the REST API."""
    from fastapi.middleware.cors import CORSMiddleware

    from context_manager.api.auth import install_api_token_auth
    from context_manager.api.security import configured_cors_origins

    origins = configured_cors_origins()
    app = mcp.streamable_http_app()
    install_api_token_auth(app, allowed_origins=origins)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "Mcp-Protocol-Version",
            "Mcp-Session-Id",
            "X-Qwenpaw-Data-Run",
            "X-Request-ID",
        ],
        expose_headers=[
            "Mcp-Session-Id",
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "Retry-After",
            "X-Request-ID",
        ],
    )
    return app


if __name__ == "__main__":
    transport = (os.environ.get("CM_MCP_TRANSPORT") or "stdio").strip().lower()
    if transport in ("streamable-http", "streamable_http", "http"):
        import uvicorn

        port = int(os.environ.get("CM_MCP_PORT", "8768"))
        mcp_host = (os.environ.get("QWENPAW_DATA_MCP_HOST") or "127.0.0.1").strip()
        from context_manager.api.auth import is_authentication_configured

        if not is_loopback_bind_host(mcp_host) and not is_authentication_configured():
            raise SystemExit(
                "Refusing non-loopback MCP bind without configured API credentials"
            )
        uvicorn.run(standalone_http_app(), host=mcp_host, port=port)
    else:
        mcp.run()

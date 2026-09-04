"""FastAPI 后端：Context Manager 服务与图谱运维 API。

路由：

- ``GET/POST /api/kg_admin/...``  知识库维护 API
- ``POST /api/resolve``      NL → metric 候选（同义词消歧）
- ``POST /api/expand``       metric_key → 子图
- ``POST /api/cypher``       原始 Cypher（只读）
- ``POST /api/execute_sql``  在配置的 Postgres 上执行只读 ``SELECT``
- ``POST /api/feedback``     用户语义反馈 → 策略卡写回
- ``POST /api/admin/reset_memory``  清除 self-refine 学到的经验记忆
- ``GET  /api/health``       存活检查
- ``GET/POST /api/monitor_export_log``  GET 为探测；POST 将监视台 bundle 写入项目根目录 ``log/``
- ``GET/POST /api/global_graph``  全局骨架（默认 ``domain_roots_only`` 仅 ``Domain``；也可 ``domain_roots_only=false`` 按边采样；``skeleton`` 排除列级/join 边）
- ``GET /api/domains``            业务域名称列表（Domain.name）
- ``POST /api/domain_graph``     仅 ``Domain-[:HAS_METRIC]->Metric`` 骨架
- ``POST /api/expand_node``     Topology UI 双击：单节点全邻域（进出边，有上限）
- ``POST /api/search_nodes``   Explorer「找节点」：当前逻辑库全库子串匹配（非仅画布）
- ``POST /api/expand_layer``    按方向展开一层（出边 / 入边）；可供脚本或其它客户端使用
- ``GET  /api/rds_import_pipeline``  Postgres/RDS：DDL→catalog→Neo4j 流水线说明（JSON，供 UI「RDS 导入」面板）
KG 文档管理：
- ``POST   /api/v1/docs/upload``     上传单个文档（multipart；txt/docx/pdf/md）；``doc_id`` 为 ``kg-docs/{filename}``
- ``GET    /api/v1/docs``            文档列表（``page`` / ``page_size``）；返回 ``download_url`` 预签名链接
- ``DELETE /api/v1/docs/{doc_id}``   删除文档（``doc_id`` 路径参数需 URL 编码）

请求头 ``X-Neo4j-Database``（可选）：指定 Neo4j 5 逻辑库；不设则使用 ``.env`` 中的 ``NEO4J_DATABASE``。
本地可拆分：``NEO4J_DATABASE_DEMO``（``make demo``）、``NEO4J_DATABASE_MCP``（MCP 子进程）。

CM API（``/api/v1/cm``）与 MCP（``/mcp/v1/cm``）见各 router 文档。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Optional

import asyncio
import json
import logging as _logging_mod
import os
import re
import time
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from neo4j import GraphDatabase
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from qwenpaw_data.context.async_logging import AsyncRotatingFileHandler
from qwenpaw_data.context.blocking_io import (
    BlockingIOGovernor,
    BlockingIOOverloaded,
    BlockingIOTimeout,
    BlockingPool,
)

from ..config import CFG
from ..utils import get_logger, neo4j_database_ctx, neo4j_session, pick_neo4j_database
from . import retrieval
from .executor import (
    ExecResult,
    classify_pg_exec_signal,
    execute_sql as topology_execute_sql,
    execute_sql_async,
)
from .rds_pipeline import build_rds_import_pipeline_payload


def _positive_float_env(name: str, default: float, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except ValueError:
        return default


def _positive_int_env(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


async def _event_loop_lag_monitor(app: FastAPI) -> None:
    """Continuously expose and warn on event-loop scheduling lag."""
    interval = _positive_float_env("QWENPAW_DATA_EVENT_LOOP_PROBE_SECONDS", 1.0, 0.01)
    warning = _positive_float_env("QWENPAW_DATA_EVENT_LOOP_LAG_WARN_SECONDS", 0.2, 0.001)
    loop = asyncio.get_running_loop()
    expected = loop.time() + interval
    app.state.event_loop_lag_ms = 0
    app.state.event_loop_max_lag_ms = 0
    while True:
        await asyncio.sleep(interval)
        now = loop.time()
        lag_ms = max(0, round((now - expected) * 1000))
        app.state.event_loop_lag_ms = lag_ms
        app.state.event_loop_max_lag_ms = max(
            app.state.event_loop_max_lag_ms,
            lag_ms,
        )
        if lag_ms >= warning * 1000:
            log.warning("event loop lag detected: %dms", lag_ms)
        expected = now + interval
from . import kg_admin as kg_admin_api
from .doc_api import router as doc_router

from qwenpaw_data.context.paths import access_log_path, data_bridge_logs_dir

log = get_logger("api.server")

# context_manager/api/server.py → 仓库根（dataagent/）
PROJECT_ROOT = Path(__file__).resolve().parents[3]
MONITOR_EXPORT_LOG_DIR = data_bridge_logs_dir()

# Access log: 写文件 + admin API 读取
_ACCESS_LOG_PATH = Path(
    os.getenv("DATAAGENT_ACCESS_LOG", str(access_log_path()))
)
_ACCESS_LOG_MAX_BYTES = int(os.getenv("DATAAGENT_ACCESS_LOG_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MB
_SAFE_MONITOR_FN = re.compile(r"[^\w.\-]+")


def _monitor_export_filename(payload: dict[str, Any]) -> str:
    db_raw = str(payload.get("database") or "na")
    safe_db = _SAFE_MONITOR_FN.sub("_", db_raw).strip("._") or "na"
    exported_at = payload.get("exported_at")
    if isinstance(exported_at, str) and exported_at.strip():
        stamp = exported_at.replace(":", "-").replace(".", "-")[:19]
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    return f"monitor-export-{safe_db}-{stamp}.json"


# 监视台导出 log 时节点 props 等会带长 float 向量，不必落盘
_MONITOR_EXPORT_EMBED_KEYS = frozenset(
    {
        "embedding",
        "signature_emb",
        "query_emb",
        "query_embedding",
        "strategy_vec",
    }
)


def _is_float_vector_list(val: Any) -> bool:
    """判定是否为向量序列（空列表视为占位，一并省略具体元素）。"""
    if not isinstance(val, list):
        return False
    if len(val) == 0:
        return True
    return all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in val)


def _strip_embedding_vectors_for_monitor_export(obj: Any) -> Any:
    """递归剔除监视台导出 JSON 中的 embedding 具体数值，保留维度占位。"""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _MONITOR_EXPORT_EMBED_KEYS and _is_float_vector_list(v):
                dim = len(v) if isinstance(v, list) else 0
                out[k] = {"_omitted": "float_vector", "dim": dim}
            else:
                out[k] = _strip_embedding_vectors_for_monitor_export(v)
        return out
    if isinstance(obj, list):
        return [_strip_embedding_vectors_for_monitor_export(x) for x in obj]
    return obj


# ---------------------------------------------------------------------- #
# 请求 / 响应 schema
# ---------------------------------------------------------------------- #
class ResolveRequest(BaseModel):
    query: str
    k: int = 5


class ExpandRequest(BaseModel):
    metric_key: str
    include_anomaly: bool = True
    include_drill: bool = True
    include_operators: bool = True
    include_calibers: bool = True
    include_derived: bool = True
    include_cross_graph: bool = True


class CypherRequest(BaseModel):
    cypher: str
    params: dict[str, Any] = Field(default_factory=dict)
    limit: int = 200


class ExecuteSqlRequest(BaseModel):
    """浏览器「结果」面板：对当前 ``PG_*`` 连接跑只读查询。"""

    sql: str = Field(..., description="SELECT / WITH …（仅只读）")
    max_rows: int = Field(200, ge=1, le=2000)
    # 可选执行上下文：完成后将报错 / 空结果 / 慢查询异步写入 Strategy（见 ``writeback_exec_signal``）
    exec_signal_enabled: bool = Field(
        False,
        description="为 True 且带有 question 或 reuse_card_key 时，根据执行结果写回经验库",
    )
    question: str = Field("", description="当前自然语言问题（与 agent 返回 question 对齐）")
    db_id: str = Field("", description="物理库 id，如 app_db")
    task_type: str = Field("", description="决策任务类型（decision_task_type）")
    reuse_card_key: str = Field("", description="本轮复用的 Strategy key")
    node_keys: list[str] = Field(default_factory=list, description="子图 node_keys")
    slow_ms_threshold: float = Field(
        8_000.0,
        ge=500.0,
        le=600_000.0,
        description="耗时 ≥ 该阈值（毫秒）视为慢查询信号（无错误且结果非空时）",
    )


class UserFeedbackRequest(BaseModel):
    """用户对某次查询结果的语义反馈。

    SQL 可能执行成功但结果不对（如聚合表缺了维度过滤）——这类错误只有用户才能发现。
    提交后触发异步写回：
      - 负反馈 → 写 avoid 卡 + 降级被复用的旧卡
      - 负反馈 + corrected_sql → 额外写高信任度 apply 卡供未来 ANN 复用
    """

    question: str = Field(..., description="原始用户问题")
    db_id: str = Field("", description="数据库 id（如 app_db）")
    task_type: str = Field("", description="决策任务类型（如 pure_lookup）")
    reuse_card_key: str = Field("", description="本次复用的卡 key（无则空字符串）")
    pred_sql: str = Field("", description="被判定为错误的 SQL")
    node_keys: list[str] = Field(default_factory=list, description="子图 node_keys（来自 agent_trace）")
    reason: str = Field("", description="用户描述错误原因，将成为 avoid 卡的 lesson")
    corrected_sql: str = Field("", description="可选：用户提供的正确 SQL，将创建高信任 apply 卡")


class GlobalGraphRequest(BaseModel):
    """``POST /api/global_graph`` 请求体；前端"全局图"按钮用。

    默认 ``domain_roots_only=True``，只返回顶级 ``Domain`` 节点（无边），
    后续靠 :func:`api_expand_layer` 双击展开。
    """
    max_edges: int = Field(
        default_factory=lambda: CFG.explore_global_graph_max_edges,
        ge=1,
        le=20_000,
    )
    max_nodes: int = Field(
        default_factory=lambda: CFG.explore_global_graph_max_nodes,
        ge=1,
        le=50_000,
    )
    skeleton: bool = True
    domain_roots_only: bool = True
    task_roots: bool = False
    max_task_roots: int = Field(10, ge=0, le=200)
    zone_mode: Literal["all", "metadata", "trace", "knowledge"] = Field(
        "all",
        description="按 Neo4j props.zone 从全库筛选骨架；与画布上已展开内容无关",
    )


class DomainGraphRequest(BaseModel):
    """``POST /api/domain_graph`` 请求体；选定一个 Domain 看其 ``HAS_METRIC`` 骨架。"""
    domain: str = Field(..., description="Domain.name，如 'ChatApp'")
    max_nodes: int = Field(
        default_factory=lambda: CFG.explore_domain_graph_max_nodes,
        ge=1,
        le=2000,
    )


class ExpandNodeRequest(BaseModel):
    """``POST /api/expand_node`` 请求体；单节点全部邻居（不区分方向）。"""
    node_key: str = Field(..., description="目标节点的 ``key``")
    max_edges: int = Field(
        default_factory=lambda: CFG.explore_expand_max_edges,
        ge=1,
        le=2000,
    )
    exclude_trace_knowledge: bool = False


class ExplorerNodeSearchRequest(BaseModel):
    """``POST /api/search_nodes`` 请求体；全库找节点（Explorer 工具栏）。"""
    query: str = Field("", max_length=500)
    limit: int = Field(
        default_factory=lambda: CFG.explore_search_nodes_limit,
        ge=1,
        le=100,
    )


class ExpandLayerRequest(BaseModel):
    """``POST /api/expand_layer`` 请求体；按方向展开一层。"""
    node_key: str = Field(..., description="目标节点的 ``key``")
    direction: str = Field("down", description="``down`` 出边 / ``up`` 入边")
    max_edges: int = Field(
        default_factory=lambda: CFG.explore_expand_max_edges,
        ge=1,
        le=2000,
    )
    fallback_neighbors: bool = True
    exclude_trace_knowledge: bool = False


class KgListRequest(BaseModel):
    """``POST /api/kg_admin/list`` — 按子串检索 Entity / Event。"""
    query: str = ""
    kind: Literal["both", "entity", "event"] = "both"
    limit: int = Field(80, ge=1, le=500)


class KgEntityUpsertRequest(BaseModel):
    key: str
    canonical_name: str = ""
    entity_type: str = ""
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    lifecycle_state: str = ""


class KgEventUpsertRequest(BaseModel):
    key: str
    name: str
    event_type: str = ""
    description: str = ""
    date_from: str = ""
    date_to: str = ""
    scope: str = "_global"


class KgDeleteRequest(BaseModel):
    key: str


class KgDeleteBatchRequest(BaseModel):
    keys: list[str] = Field(default_factory=list, description="待删除的 Entity/Event key 列表")


class KgEdgeDeleteRequest(BaseModel):
    """删除当前选中结点（须为 Entity/Event）与对端之间、指定类型的一条或多条同类型平行边。"""
    anchor_key: str = Field(..., description="当前选中的结点 key")
    other_key: str = Field(..., description="对端结点 key")
    rel_type: str = Field(..., description="关系类型，与 Neo4j type(r) 一致（如 RELATED_TO、SUPERSEDED_BY）")
    direction: Literal["in", "out"] = Field(
        "out",
        description="相对锚点：out=(anchor)-[r]->(other)，in=(other)-[r]->(anchor)",
    )


class KgEdgeDeleteByTypeRequest(BaseModel):
    """按关系类型批量删除锚点上全部该类型边（可选仅出/仅入/双向）。"""
    anchor_key: str
    rel_type: str
    direction_scope: Literal["both", "out", "in"] = "both"


class KgGlobalEdgePurgeRequest(BaseModel):
    """删除当前库中：至少一端为 Entity/Event 的、指定类型的全部有向边（``rel_type`` 须与 ``type(r)`` 一致）。"""
    rel_type: str = Field(..., description="关系类型，与 Neo4j type(r) 完全一致；可选自 global_purge_types 列表")


class KgRelatedToRequest(BaseModel):
    from_key: str
    to_key: str
    relation_subtype: str = "see_also"
    description: str = ""


class KgAboutRequest(BaseModel):
    event_key: str
    entity_key: str
    connect: bool = True


def create_app() -> FastAPI:
    from .ctx_session import SessionStore
    from .semantic_api import router as semantic_router
    from .cm_api import router as cm_router
    from .import_api import router as import_router
    from .datasource_active_api import router as datasource_active_router

    mcp_session_managers: list[Any] = []
    access_logger = _logging_mod.getLogger(f"api.access.{id(mcp_session_managers)}")
    access_logger.propagate = False
    access_logger.setLevel(_logging_mod.INFO)
    access_log_handler = AsyncRotatingFileHandler(
        _ACCESS_LOG_PATH,
        max_bytes=_ACCESS_LOG_MAX_BYTES,
        backup_count=2,
        queue_size=_positive_int_env("QWENPAW_DATA_ACCESS_LOG_QUEUE_SIZE", 4096),
    )
    access_log_handler.setFormatter(_logging_mod.Formatter("%(message)s"))
    access_logger.addHandler(access_log_handler)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from contextlib import AsyncExitStack
        from anyio.to_thread import current_default_thread_limiter

        sync_route_limiter = current_default_thread_limiter()
        previous_sync_route_tokens = sync_route_limiter.total_tokens
        sync_route_limiter.total_tokens = _positive_int_env(
            "QWENPAW_DATA_SYNC_ROUTE_WORKERS",
            40,
        )
        governor = BlockingIOGovernor()
        app.state.blocking_io = governor
        app.state.sync_route_workers = sync_route_limiter.total_tokens
        if access_log_handler not in access_logger.handlers:
            access_logger.addHandler(access_log_handler)
        access_log_handler.start()
        driver = None
        lag_monitor = None
        try:
            driver = GraphDatabase.driver(
                CFG.neo4j_uri,
                auth=(CFG.neo4j_user, CFG.neo4j_password),
                # OPTIONAL MATCH 上的 UnknownRelationshipType 是预期的（兼容性），全局抑制
                notifications_min_severity="OFF",
            )
            app.state.driver = driver
            # 图后端抽象：把 driver 包成 Neo4jBackend 注册为活跃后端
            # （复用 driver、不拥有其生命周期），graph_session(None) 即可路由。
            try:
                from context_manager.graph.backends.registry import init_backend

                init_backend(CFG, neo4j_driver=driver)
            except Exception as backend_exc:  # noqa: BLE001
                log.warning("graph backend registration failed: %s", backend_exc)
            app.state.session_store = SessionStore(
                sqlite_path=CFG.sessions_db_path if CFG.sessions_persist else None,
            )
            lag_monitor = asyncio.create_task(
                _event_loop_lag_monitor(app),
                name="qwenpaw-data-event-loop-lag-monitor",
            )
            log.info("Neo4j driver opened: %s", CFG.neo4j_uri)
            # semantic-config 编辑层：建 SQLite 表（幂等）。失败不阻断 CM 图层启动。
            try:
                from semantic_config.db import init_db as _sc_init_db
                await _sc_init_db()
                log.info("semantic-config SQLite initialized")
            except Exception as _sc_e:  # noqa: BLE001
                log.warning("semantic-config SQLite init failed: %s", _sc_e)
            try:
                from qwenpaw_data.context.job_store import get_job_store

                interrupted_jobs = await governor.run(
                    BlockingPool.FILE,
                    "jobs.recover_interrupted",
                    get_job_store().recover_interrupted,
                )
                if interrupted_jobs:
                    log.warning(
                        "marked %d interrupted jobs as failed after restart",
                        interrupted_jobs,
                    )
            except Exception as jobs_exc:  # noqa: BLE001
                log.warning("persistent job recovery failed: %s", jobs_exc)
            # 检查并恢复中断的 embedding rebuild 任务
            try:
                from context_manager.embedding_rebuild import get_rebuild_store, start_rebuild
                _rebuild_store = get_rebuild_store()
                _interrupted = _rebuild_store.get_latest_active()
                if _interrupted:
                    log.info("Resuming interrupted embedding rebuild: %s", _interrupted.job_id)
                    start_rebuild(driver, _interrupted.job_id)
            except Exception as _rb_e:  # noqa: BLE001
                log.warning("Failed to check/resume embedding rebuild: %s", _rb_e)
            async with AsyncExitStack() as stack:
                for mgr in mcp_session_managers:
                    await stack.enter_async_context(mgr.run())
                yield
        finally:
            if lag_monitor is not None:
                lag_monitor.cancel()
                try:
                    await lag_monitor
                except asyncio.CancelledError:
                    pass
            try:
                await governor.aclose()
            finally:
                try:
                    from context_manager.graph.backends.registry import (
                        get_manager as _get_backend_manager,
                    )

                    _get_backend_manager().close_all()
                except Exception as backend_exc:  # noqa: BLE001
                    log.warning("graph backend shutdown failed: %s", backend_exc)
                try:
                    if driver is not None:
                        await asyncio.to_thread(driver.close)
                finally:
                    try:
                        await asyncio.to_thread(access_log_handler.stop)
                    finally:
                        access_logger.removeHandler(access_log_handler)
                        sync_route_limiter.total_tokens = previous_sync_route_tokens
            log.info("Neo4j driver closed")

    app = FastAPI(title="QwenPaw Data Context Manager", version="0.1.0", lifespan=lifespan)
    from qwenpaw_data.context.errors import install_error_handlers

    install_error_handlers(app)

    @app.exception_handler(BlockingIOOverloaded)
    async def blocking_io_overloaded_handler(
        _request: Request,
        exc: BlockingIOOverloaded,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "1"},
            content={
                "code": "BLOCKING_IO_OVERLOADED",
                "detail": str(exc),
                "pool": exc.pool.value,
                "operation": exc.operation,
            },
        )

    @app.exception_handler(BlockingIOTimeout)
    async def blocking_io_timeout_handler(
        _request: Request,
        exc: BlockingIOTimeout,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=504,
            content={
                "code": "BLOCKING_IO_TIMEOUT",
                "detail": str(exc),
                "pool": exc.pool.value,
                "operation": exc.operation,
            },
        )

    async def _run_blocking(
        pool: BlockingPool,
        operation: str,
        func,
        *args,
        timeout_seconds: float | None = None,
        **kwargs,
    ):
        return await app.state.blocking_io.run(
            pool,
            operation,
            func,
            *args,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )

    # P1 凭证脱敏
    # CredentialRedactFilter 必须挂在 handler 上——logger-level filter
    # 不会处理子 logger propagate 来的 record。为了覆盖将来新增的
    # handler（如 uvicorn 动态添加的），monkey-patch root.addHandler。
    import logging as _logging
    from ..secrets.redact import CredentialRedactFilter
    _root = _logging.getLogger()
    _crf = CredentialRedactFilter()
    # 1) 挂在 root logger 自身（供测试发现）
    if not any(isinstance(f, CredentialRedactFilter) for f in _root.filters):
        _root.addFilter(_crf)
    # 2) 挂在所有已有 handler
    for _h in list(_root.handlers):
        if not any(isinstance(f, CredentialRedactFilter) for f in _h.filters):
            _h.addFilter(_crf)
    # 3) 自动挂在将来新增的 handler
    _orig_root_addHandler = _root.addHandler

    def _addHandler_with_redact(hdlr):
        _orig_root_addHandler(hdlr)
        if not any(isinstance(f, CredentialRedactFilter) for f in hdlr.filters):
            hdlr.addFilter(_crf)

    _root.addHandler = _addHandler_with_redact  # type: ignore[assignment]

    from .security import configured_cors_origins

    _cors_origins = configured_cors_origins()

    # ---- Embedding rebuild block middleware ----
    _REBUILD_EXEMPT_PREFIXES = (
        "/api/system/model-config/embedding/jobs",
        "/api/system/model-config",
        "/api/health",
        "/health",
        "/favicon.ico",
    )

    @app.middleware("http")
    async def embedding_rebuild_block_middleware(request: Request, call_next):
        from context_manager.embedding_rebuild import is_embedding_rebuild_active
        active, job_id = is_embedding_rebuild_active()
        if active:
            path = request.url.path
            if not any(path.startswith(p) for p in _REBUILD_EXEMPT_PREFIXES):
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Embedding rebuild in progress", "job_id": job_id},
                    headers={"Retry-After": "30"},
                )
        return await call_next(request)

    @app.middleware("http")
    async def neo4j_database_middleware(request: Request, call_next):
        try:
            chosen = pick_neo4j_database(request.headers.get("x-neo4j-database"))
        except ValueError as e:
            return JSONResponse({"detail": str(e)}, status_code=400)
        token = neo4j_database_ctx.set(chosen)
        try:
            return await call_next(request)
        finally:
            neo4j_database_ctx.reset(token)

    # ---- Access log middleware ----
    # 访问日志只记录元数据。请求体、查询参数和响应体可能包含数据源
    # 密码、STS token 或业务数据，不应在通用 access log 中落盘。
    _ACCESS_SKIP_PREFIXES = ("/health", "/favicon")

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next):
        path = request.url.path
        # 跳过静态资源和健康检查
        if any(path.startswith(p) for p in _ACCESS_SKIP_PREFIXES):
            return await call_next(request)

        method = request.method
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        t0 = time.monotonic()
        response = await call_next(request)
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        client_ip = request.client.host if request.client else "-"
        neo4j_db = request.headers.get("x-neo4j-database", "")

        line = (
            f"[{ts}] {method} {path}"
            f" → {response.status_code} "
            f"{elapsed_ms}ms client={client_ip}"
            f"{' db=' + neo4j_db if neo4j_db else ''}"
        )
        access_logger.info(line)
        return response

    # Security must wrap the rebuild/database middleware so authentication,
    # CSRF and throttling cannot be bypassed by their early responses. CORS is
    # added last and therefore remains outermost for 401/403/429 responses.
    from qwenpaw_data.context.resource_budget import ResourceBudgetMiddleware
    from qwenpaw_data.context.uploads import RequestBodyLimitMiddleware

    from .auth import install_api_token_auth

    # 先注册 body limit，再注册认证与 CORS；Starlette 后注册的中间件在外层，
    # 因此大请求仍须先通过认证，413 响应也会带正确的 CORS 头。
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(ResourceBudgetMiddleware)

    install_api_token_auth(
        app,
        enforce_scopes=True,
        allowed_origins=_cors_origins,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "Last-Event-ID",
            "Mcp-Protocol-Version",
            "Mcp-Session-Id",
            "X-Qwenpaw-Data-Run",
            "X-Neo4j-Database",
            "X-Request-ID",
        ],
        expose_headers=[
            "RateLimit-Limit",
            "RateLimit-Remaining",
            "Retry-After",
            "Mcp-Session-Id",
            "X-Request-ID",
        ],
    )

    app.include_router(semantic_router)
    app.include_router(cm_router)
    app.include_router(import_router)
    app.include_router(doc_router)
    app.include_router(datasource_active_router)

    from .model_config_api import router as model_config_router
    app.include_router(model_config_router)

    from .trace_api import router as trace_router
    app.include_router(trace_router)
    from .trace_api import callback_router as trace_callback_router
    app.include_router(trace_callback_router)

    # Semantic layer export + incremental import
    from .semantic_io_api import router as semantic_io_router
    app.include_router(semantic_io_router)

    # === MG/TG/KG 三图 CRUD router（/api/v1/*）===
    from .mg_readonly_api import router as mg_readonly_router
    from .kg_admin_api import router as kg_router
    from .tg_admin_api import router as tg_admin_router
    from .user_context_api import router as user_context_router
    from .cypher_api import router as cypher_router
    from .explorer_api import router as explorer_router
    app.include_router(mg_readonly_router)
    app.include_router(kg_router)
    app.include_router(tg_admin_router)
    app.include_router(user_context_router)
    app.include_router(cypher_router)
    app.include_router(explorer_router)


    try:
        from context_manager.mcp.cm_server import mcp as cm_mcp
        from context_manager.mcp.cm_server import mount_streamable_http as mount_cm_mcp

        mcp_session_managers.append(mount_cm_mcp(app, path="/mcp/v1/cm"))
        cm_mcp._cm_app = app
        log.info("Unified CM MCP (streamable HTTP) mounted at /mcp/v1/cm")
    except ImportError as exc:
        log.warning("MCP servers not mounted (pip install mcp): %s", exc)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/api/rds_import_pipeline")
    async def api_rds_import_pipeline() -> dict[str, Any]:
        """DDL 在 Postgres 上执行 → 反射 catalog → Neo4j；含 Default 拓扑与向量步骤。

        返回结构化步骤与可复制命令；不落盘、不执行 shell。
        """
        return build_rds_import_pipeline_payload()

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        governor = getattr(app.state, "blocking_io", None)
        return {
            "status": "ok",
            "event_loop_lag_ms": getattr(app.state, "event_loop_lag_ms", 0),
            "event_loop_max_lag_ms": getattr(app.state, "event_loop_max_lag_ms", 0),
            "sync_route_workers": getattr(app.state, "sync_route_workers", 0),
            "blocking_io": governor.snapshot() if governor is not None else {},
            "access_log": {
                "queued": access_log_handler.queued,
                "dropped": access_log_handler.dropped,
                "writer_failed": access_log_handler.writer_error is not None,
            },
        }

    @app.get("/api/auth/status")
    async def auth_status() -> dict[str, bool]:
        """Public bootstrap endpoint; never returns credential material."""
        from .auth import is_authentication_configured

        return {"required": is_authentication_configured()}

    @app.get("/api/auth/check")
    async def auth_check(request: Request) -> dict[str, Any]:
        """Protected endpoint used by clients to validate a supplied token."""
        principal = request.state.auth_principal
        return {
            "authenticated": True,
            "subject": principal.subject,
            "scopes": sorted(principal.scopes),
        }

    @app.get("/api/monitor_export_log")
    async def api_monitor_export_log_probe() -> dict[str, str]:
        """探测路由是否已加载；浏览器打开此 URL 应见 JSON（若 404 则需重启 ``scripts/serve.py``）。"""
        return {
            "ok": True,
            "hint": "POST JSON（监视台 bundle）到此路径以写入项目根目录 log/monitor-export-*.json",
        }

    @app.post("/api/monitor_export_log")
    async def api_monitor_export_log(request: Request) -> dict[str, Any]:
        """将监视台 bundle 写入 ``<项目根>/log``（本地开发用；需服务端可写磁盘）。"""
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="Body must be a JSON object")

        def _write_export() -> tuple[str, Path]:
            MONITOR_EXPORT_LOG_DIR.mkdir(parents=True, exist_ok=True)
            name = _monitor_export_filename(payload)
            out_path = MONITOR_EXPORT_LOG_DIR / name
            to_save = _strip_embedding_vectors_for_monitor_export(payload)
            text = json.dumps(to_save, ensure_ascii=False, indent=2)
            out_path.write_text(text, encoding="utf-8")
            return name, out_path

        try:
            name, out_path = await _run_blocking(
                BlockingPool.FILE,
                "monitor_export.write",
                _write_export,
            )
        except OSError as exc:
            log.exception("monitor_export_log write failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        rel = f"log/{name}"
        log.info("monitor export saved: %s", out_path)
        return {
            "ok": True,
            "path": rel,
            "absolute_path": str(out_path),
            "filename": name,
        }

    def _global_graph_payload(
        max_edges: int,
        max_nodes: int,
        *,
        skeleton: bool,
        domain_roots_only: bool,
        task_roots: bool,
        max_task_roots: int,
        zone_mode: str = "all",
    ) -> dict[str, Any]:
        try:
            return retrieval.global_graph_snapshot(
                app.state.driver,
                max_edges=max_edges,
                max_nodes=max_nodes,
                skeleton=skeleton,
                domain_roots_only=domain_roots_only,
                task_roots=task_roots,
                max_task_roots=max_task_roots,
                zone_mode=zone_mode,
            )
        except Exception as exc:
            log.exception("global_graph_snapshot failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc


    @app.get("/api/domains")
    async def api_domains() -> dict[str, Any]:
        names = await _run_blocking(
            BlockingPool.GRAPH,
            "domains.list",
            retrieval.list_domains,
            app.state.driver,
        )
        return {"domains": names}


    @app.post("/api/resolve")
    async def api_resolve(req: ResolveRequest) -> dict[str, Any]:
        candidates = await _run_blocking(
            BlockingPool.GRAPH,
            "metric.resolve",
            retrieval.resolve_metric,
            app.state.driver,
            req.query,
            k=req.k,
        )
        return {"candidates": candidates}

    @app.post("/api/expand")
    async def api_expand(req: ExpandRequest) -> dict[str, Any]:
        return await _run_blocking(
            BlockingPool.GRAPH,
            "subgraph.expand",
            retrieval.expand_subgraph,
            app.state.driver,
            req.metric_key,
            include_anomaly=req.include_anomaly,
            include_drill=req.include_drill,
            include_calibers=req.include_calibers,
            include_derived=req.include_derived,
            include_cross_graph=req.include_cross_graph,
        )

    @app.post("/api/admin/reset_memory")
    def api_reset_memory() -> dict[str, Any]:
        """一键清除 self-refine 学到的"经验记忆"——用于回到 demo 起点。

        删除范围（**只删自动生成**，保留 metrics_dict / trace_tasks 里的种子）：

        - ``Caliber`` 节点 ``WHERE source = 'self_refine'``
        - ``Experience`` 节点（迁移前遗留）``WHERE key =~ 'exp:[0-9a-f]+:cal:.*'``
        - ``Strategy`` 节点 ``WHERE key =~ 'card:[0-9a-f]{12}'``
          （与 ``card_key()`` 生成的自动化策略 key 一致；手工种子请避免该格式）
        - ``Task / Step / ToolCall / Observation / Claim``，``WHERE Task.key =~ 'task:\\d{8}T\\d{6}-.*'``
          （时间戳形式 ``task:20260429T154322-deadbeef``，由 trace_writer 生成；
           种子的 key 是 ``task:abc123def4567890`` 这种 hex 直白形式）

        前端"清除记忆"按钮调这个；调完图谱里只剩 metrics_dict 里的硬性种子。
        """
        with neo4j_session(app.state.driver) as s:
            cal = s.run("""
                MATCH (c:Caliber {source: 'self_refine'})
                WITH collect(c) AS cs
                FOREACH (x IN cs | DETACH DELETE x)
                RETURN size(cs) AS n
            """).single()
            exp = s.run("""
                MATCH (e:Experience)
                WHERE e.key =~ 'exp:[0-9a-f]+:cal:.*'
                WITH collect(e) AS es
                FOREACH (x IN es | DETACH DELETE x)
                RETURN size(es) AS n
            """).single()
            strat = s.run("""
                MATCH (c:Strategy)
                WHERE c.key =~ 'card:[0-9a-f]{12}'
                WITH collect(c) AS cs
                FOREACH (x IN cs | DETACH DELETE x)
                RETURN size(cs) AS n
            """).single()
            tk = s.run("""
                MATCH (t:Task)
                WHERE t.key =~ 'task:\\d{8}T\\d{6}-.*'
                OPTIONAL MATCH (t)-[:DECOMPOSES_INTO]->(p:Step)
                OPTIONAL MATCH (p)-[:EXECUTED_BY]->(tc:ToolCall)
                OPTIONAL MATCH (tc)-[:PRODUCES]->(cl:Claim)
                WITH collect(DISTINCT t) AS tasks,
                     collect(DISTINCT p) AS plans,
                     collect(DISTINCT tc) AS tcs,
                     collect(DISTINCT cl) AS cls
                FOREACH (x IN tasks | DETACH DELETE x)
                FOREACH (x IN plans | DETACH DELETE x)
                FOREACH (x IN tcs   | DETACH DELETE x)
                FOREACH (x IN cls   | DETACH DELETE x)
                RETURN size(tasks) AS task_n, size(plans) AS plan_n,
                       size(tcs) AS toolcall_n, 0 AS obs_n,
                       size(cls) AS claim_n
            """).single()
        deleted = {
            "caliber": int(cal["n"] if cal else 0),
            "experience": int(exp["n"] if exp else 0),
            "strategy_card": int(strat["n"] if strat else 0),
            "task": int(tk["task_n"] if tk else 0),
            "plan": int(tk["plan_n"] if tk else 0),
            "toolcall": int(tk["toolcall_n"] if tk else 0),
            "observation": int(tk["obs_n"] if tk else 0),
            "claim": int(tk["claim_n"] if tk else 0),
        }
        log.info("reset_memory: %s", deleted)
        return {"ok": True, "deleted": deleted}

    @app.post("/api/execute_sql")
    async def api_execute_sql(req: ExecuteSqlRequest) -> dict[str, Any]:
        """与 ReAct post-emit 相同的 PG 执行器；拓扑管线返回 ``pred_sql`` 后由前端调用来填「结果」tab。"""
        raw = await execute_sql_async(
            req.sql,
            max_rows=req.max_rows,
            governor=app.state.blocking_io,
        )
        sig_written: Optional[str] = None
        if req.exec_signal_enabled:
            q_ok = bool((req.question or "").strip())
            card_ok = bool((req.reuse_card_key or "").strip())
            if q_ok or card_ok:
                kind, detail = classify_pg_exec_signal(raw, float(req.slow_ms_threshold))
                if kind:
                    try:
                        from context_manager.runtime.writeback import writeback_exec_signal

                        neo_logical = neo4j_database_ctx.get() or CFG.neo4j_database
                        app.state.blocking_io.submit(
                            BlockingPool.GRAPH,
                            "writeback.exec_signal",
                            writeback_exec_signal,
                            app.state.driver,
                            question=(req.question or "").strip(),
                            db_id=req.db_id or "",
                            task_type=req.task_type or "",
                            reuse_card_key=(req.reuse_card_key or "").strip(),
                            node_keys=list(req.node_keys or []),
                            signal=kind,
                            detail=detail,
                            elapsed_ms=float(raw.elapsed_ms or 0.0),
                            neo4j_database=neo_logical,
                            async_mode=False,
                        )
                        sig_written = kind
                        log.info(
                            "execute_sql: exec_signal writeback queued kind=%s elapsed_ms=%.1f",
                            kind,
                            raw.elapsed_ms or 0.0,
                        )
                    except Exception as exc:
                        log.warning("execute_sql: exec_signal writeback failed: %s", exc)
            else:
                log.debug(
                    "execute_sql: exec_signal_enabled but no question/reuse_card_key; skip writeback"
                )
        out = raw.to_dict()
        if sig_written:
            out["exec_signal_queued"] = sig_written
        return out

    @app.post("/api/feedback")
    async def api_feedback(req: UserFeedbackRequest) -> dict[str, Any]:
        """用户语义反馈：SQL 执行正常但结果有误。

        写回逻辑（异步，不阻塞响应）：
        1. 若 reuse_card_key 非空 → record_hit(outcome='fail') 降级该卡的 success_rate
        2. 始终写一张 avoid 卡，lesson = req.reason，供后续查询作 negative_hint
        3. 若 corrected_sql 非空 → 额外写高信任 apply 卡（source_trust=0.9）
        """
        q = (req.question or "").strip()
        if not q:
            raise HTTPException(status_code=400, detail="question is required")

        from context_manager.runtime.writeback import writeback_user_feedback

        neo_logical = neo4j_database_ctx.get() or CFG.neo4j_database
        app.state.blocking_io.submit(
            BlockingPool.GRAPH,
            "writeback.user_feedback",
            writeback_user_feedback,
            app.state.driver,
            q,
            req.db_id or "",
            req.task_type or "pure_lookup",
            req.reuse_card_key or "",
            req.pred_sql or "",
            req.node_keys or [],
            req.reason or "",
            req.corrected_sql or "",
            neo4j_database=neo_logical,
            async_mode=False,
            thinking_context="explorer",
        )
        log.info(
            "feedback received: question='%s...' reuse_card=%s reason='%s...'",
            q[:40],
            req.reuse_card_key or "none",
            (req.reason or "")[:60],
        )
        return {"status": "accepted", "message": "反馈已记录，将在后台写入经验库"}

    # ---- Admin: 读取 access log ----
    @app.get("/api/admin/logs/access")
    async def read_access_log(
        lines: int = Query(100, ge=1, le=2000, description="返回最后 N 行"),
        grep: str = Query("", description="按关键字过滤"),
    ):
        """读取 access log 最后 N 行，供远端调试。"""
        def _read_tail() -> dict[str, Any]:
            if not _ACCESS_LOG_PATH.is_file():
                return {
                    "lines": [],
                    "total": 0,
                    "path": str(_ACCESS_LOG_PATH),
                    "note": "log file not yet created",
                }
            try:
                with _ACCESS_LOG_PATH.open("r", encoding="utf-8") as f:
                    all_lines = f.readlines()
            except OSError as exc:
                return {"error": str(exc)}

            if grep:
                all_lines = [line for line in all_lines if grep in line]

            tail = all_lines[-lines:]
            return {
                "path": str(_ACCESS_LOG_PATH),
                "total_lines": len(all_lines),
                "returned": len(tail),
                "lines": [line.rstrip("\n") for line in tail],
            }

        return await _run_blocking(
            BlockingPool.FILE,
            "access_log.read",
            _read_tail,
        )

    # 全局异常兜底：脱敏后返回 500
    from ..secrets.redact import _redact_str

    @app.exception_handler(Exception)
    async def safe_error_handler(req: Request, exc: Exception):
        _logging.getLogger("api.server").error("unhandled error", exc_info=exc)
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "detail": _redact_str(str(exc))[:200],
            },
        )

    # semantic-config（编辑层）挂载：路由 + 按路径分派的异常处理器。
    # 必须在 CM 自身路由与兜底异常处理器注册完成后调用（以便正确回退 CM 既有逻辑）。
    from semantic_config.integration import mount_semantic_config
    mount_semantic_config(app)

    return app


app = create_app()

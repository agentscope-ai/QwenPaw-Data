"""纯语义层导入业务逻辑（HTTP 端点与进程内调用共用）。

本模块把原 ``api/semantic_api.semantic_import`` 端点体里的图构建逻辑抽出来，
成为 ``driver`` 显式传入的纯函数。这样：

* HTTP 路由层（``api/semantic_api.py``）继续负责 ``request.app.state.driver``
  解析、HTTP 回调通知、异常 → HTTPException 转换；
* 进程内调用方（合并仓库后的 ``semantic_config`` weave 侧）可以直接 import
  :func:`run_semantic_import_sync` / :func:`run_semantic_import_async`，
  无需走 loopback HTTP。

进程内调用方式
--------------

合并仓库后，weave 侧（``semantic_config``）可以这样调用：

.. code-block:: python

    from context_manager.graph.semantic_import_service import (
        run_semantic_import_sync,
    )
    from context_manager.contracts.import_models import (
        SemanticImportRequest, SemanticPayload,
    )

    req = SemanticImportRequest(
        datasource_id="demo_ds",
        semantic=SemanticPayload(domains=[...]),
        drop_semantic_first=True,
        task_id="TASK_20260702_001",
    )
    # driver 从合并后 app.state.driver 取，或自行 GraphDatabase.driver(...) 构造
    result = run_semantic_import_sync(driver=app.state.driver, request=req)
    # result.task_id / result.status / result.errors — 与 HTTP 端点返回结构一致

回调说明
--------

HTTP 端点在写完图后会 loopback POST ``{task_id, status, error_msg}`` 回调用方；
**进程内函数不走 HTTP 回调**，直接返回 :class:`SemanticImportResult`，
调用方从 ``result.status`` / ``result.errors`` 自行决定后续动作。
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from qwenpaw_data.context.blocking_io import BlockingIOGovernor, BlockingPool

from ..contracts.import_models import (
    ImportErrorItem,
    ImportErrorLevel,
    ImportStats,
    ImportStatus,
    SemanticImportRequest,
    SemanticImportResult,
)
from ..utils import get_logger, graph_session

log = get_logger("graph.semantic_import_service")


# ---------------------------------------------------------------------- #
# drop_semantic_for_datasource — per-datasource 语义清空
# ---------------------------------------------------------------------- #
def drop_semantic_for_datasource(driver: Any, datasource_name: str) -> int:
    """清空指定 datasource 下的全部语义节点，返回删除节点数。

    删除范围（与 ``SemanticImportRequest.drop_semantic_first=True`` 对齐）：
    Domain / Metric / Dimension / DimensionValue / Formula / Caliber /
    Dataset / DatasetColumn —— 凡 ``datasource_id == datasource_name``
    或 legacy 无 scope 的同名节点。

    物理层（Database / Schema / Table / Column）**不删**，
    与 :func:`schema_init.drop_topology` 默认行为一致。
    """
    ds = (datasource_name or "").strip()
    if not ds:
        raise ValueError("datasource_name is required for drop_semantic_for_datasource")

    def _do_delete(tx) -> int:
        # 1. 叶子：DimensionValue / Formula / Caliber（挂在 Metric/Dimension 之下）
        tx.run(
            """
            MATCH (parent)-[:HAS_VALUE|HAS_FORMULA|HAS_CALIBER]->(leaf)
            WHERE (
                parent.datasource_id IS NULL OR parent.datasource_id = '' OR parent.datasource_id = $ds
            )
            AND (
                leaf.datasource_id IS NULL OR leaf.datasource_id = '' OR leaf.datasource_id = $ds
            )
            DETACH DELETE leaf
            """,
            ds=ds,
        )
        # 2. 中层：Metric / Dimension / Dataset（挂在 Domain 之下或独立）
        tx.run(
            """
            MATCH (child)
            WHERE (child:Metric OR child:Dimension OR child:Dataset)
            AND (
                child.datasource_id IS NULL OR child.datasource_id = '' OR child.datasource_id = $ds
            )
            DETACH DELETE child
            """,
            ds=ds,
        )
        # 3. 顶层 Domain + DataSource→HAS_DOMAIN 边
        result = tx.run(
            """
            MATCH (d:Domain)
            WHERE (d.datasource_id IS NULL OR d.datasource_id = '' OR d.datasource_id = $ds)
            DETACH DELETE d
            RETURN count(d) AS n
            """,
            ds=ds,
        )
        return int(result.single()["n"] or 0)

    with graph_session(driver) as s:
        n = s.execute_write(_do_delete)
    log.info("drop_semantic_for_datasource(%s): deleted %d Domain roots", ds, n)
    return n


# ---------------------------------------------------------------------- #
# drop_physical_for_datasource — per-datasource 物理层清空
# ---------------------------------------------------------------------- #
def drop_physical_for_datasource(driver: Any, datasource_name: str) -> int:
    """清空指定 datasource 下的全部物理层节点，返回删除节点数。

    删除范围：Column / Table / Schema / Database —— 凡
    ``datasource_id == datasource_name`` 的节点。

    按 key 前缀 ``col:<ds>:`` / ``tbl:<ds>:`` / ``sch:<ds>:`` / ``db:<ds>:``
    匹配，与 :func:`write_physical` 的 key 生成规则一致。
    """
    ds = (datasource_name or "").strip()
    if not ds:
        raise ValueError("datasource_name is required for drop_physical_for_datasource")

    def _do_delete(tx) -> int:
        # 从叶子往上删，避免悬挂关系
        # 1. Column
        result = tx.run(
            """
            MATCH (c:Column)
            WHERE c.key STARTS WITH $prefix
            DETACH DELETE c
            RETURN count(c) AS n
            """,
            prefix=f"col:{ds}:",
        )
        col_n = int(result.single()["n"] or 0)
        # 2. Table
        result = tx.run(
            """
            MATCH (t:Table)
            WHERE t.key STARTS WITH $prefix
            DETACH DELETE t
            RETURN count(t) AS n
            """,
            prefix=f"tbl:{ds}:",
        )
        tbl_n = int(result.single()["n"] or 0)
        # 3. Schema
        result = tx.run(
            """
            MATCH (sch:Schema)
            WHERE sch.key STARTS WITH $prefix
            DETACH DELETE sch
            RETURN count(sch) AS n
            """,
            prefix=f"sch:{ds}:",
        )
        sch_n = int(result.single()["n"] or 0)
        # 4. Database
        result = tx.run(
            """
            MATCH (db:Database)
            WHERE db.key STARTS WITH $prefix
            DETACH DELETE db
            RETURN count(db) AS n
            """,
            prefix=f"db:{ds}:",
        )
        db_n = int(result.single()["n"] or 0)
        return col_n + tbl_n + sch_n + db_n

    with graph_session(driver) as s:
        n = s.execute_write(_do_delete)
    log.info(
        "drop_physical_for_datasource(%s): deleted %d nodes (Database/Schema/Table/Column)",
        ds, n,
    )
    return n


# ---------------------------------------------------------------------- #
# 核心业务逻辑
# ---------------------------------------------------------------------- #
def _resolve_db_id(request: SemanticImportRequest) -> str:
    """从 request.db_id 或 datasource_registry 推导物理库 id。"""
    db_id = (request.db_id or "").strip()
    if db_id:
        return db_id
    # 落到 datasource_registry 推导
    from .datasource_registry import try_resolve

    ds = try_resolve(request.datasource_id)
    if ds is not None and ds.primary_db_id:
        return ds.primary_db_id
    # 兜底：db_id 未传时用 datasource_id
    log.info(
        "db_id auto-resolved from datasource_id=%s → db_id=%s (fallback)",
        request.datasource_id,
        request.datasource_id,
    )
    return request.datasource_id


def _run_semantic_import_impl(
    driver: Any,
    request: SemanticImportRequest,
    *,
    task_id: str,
) -> SemanticImportResult:
    """同步执行语义导入核心逻辑（不含回调）。

    被 :func:`run_semantic_import_sync` 与 :func:`run_semantic_import_async`
    共用；``async`` 版本把 ``to_thread`` 包起来，``sync`` 版本直接跑。
    """
    t0 = time.monotonic()
    errors: list[ImportErrorItem] = []
    db_id = _resolve_db_id(request)
    schema = request.schema_name

    # drop_semantic_first：先清空该 datasource 下的语义节点
    if request.drop_semantic_first:
        try:
            dropped = drop_semantic_for_datasource(driver, request.datasource_id)
            log.info(
                "semantic_import [%s]: drop_semantic_first dropped %d domain roots",
                task_id, dropped,
            )
        except Exception as exc:
            log.warning(
                "semantic_import [%s]: drop_semantic_first failed (non-fatal): %s",
                task_id, exc,
            )
            errors.append(ImportErrorItem(
                level=ImportErrorLevel.warn,
                message=f"drop_semantic_first failed: {exc}",
            ))

    # 解析 SemanticPayload → metrics_dict 格式
    from .dict_parser import parse_nested_semantic_payload, save_as_temp_yaml

    try:
        payload_dict = request.semantic.model_dump()
        parsed = parse_nested_semantic_payload(payload_dict, db_id=db_id, schema=schema)
        metrics_dict_path = save_as_temp_yaml(parsed)
    except Exception as exc:
        log.exception("semantic_import [%s]: payload parse failed: %s", task_id, exc)
        return SemanticImportResult(
            task_id=task_id,
            status=ImportStatus.failed,
            errors=[ImportErrorItem(
                level=ImportErrorLevel.fatal,
                message=f"Semantic payload parse failed: {exc}",
            )],
            elapsed_seconds=round(time.monotonic() - t0, 3),
        )

    # 写入语义层
    from .semantic_pipeline import SemanticStageInput, run_semantic_stage
    from .profile import get_profile
    from . import schema_init

    try:
        schema_init.init_all(driver)
        profile = get_profile("generic")
        inp = SemanticStageInput(
            driver=driver,
            db_id=db_id,
            schema=schema,
            metrics_dict_path=metrics_dict_path,
            profile=profile,
            datasource_name=request.datasource_name,
            datasource_id=request.datasource_id,
        )
        run_semantic_stage("metrics_dict", inp)
    except Exception as exc:
        log.exception("semantic_import [%s]: semantic write failed: %s", task_id, exc)
        return SemanticImportResult(
            task_id=task_id,
            status=ImportStatus.failed,
            errors=[ImportErrorItem(
                level=ImportErrorLevel.fatal,
                message=f"Semantic layer write failed: {exc}",
            )],
            elapsed_seconds=round(time.monotonic() - t0, 3),
        )

    # embedding（idempotent：hash 未变的节点跳过；非致命）
    try:
        from .embeddings import index_embeddings

        embed_stats = index_embeddings(driver, scope="all")
        log.info(
            "semantic_import [%s]: embedding indexed: %s",
            task_id, [s.label for s in embed_stats],
        )
    except Exception as exc:
        log.warning(
            "semantic_import [%s]: embedding failed (non-fatal): %s", task_id, exc,
        )
        errors.append(ImportErrorItem(
            level=ImportErrorLevel.warn,
            message=f"Embedding indexing failed: {exc}",
        ))

    # 统计
    from .stats import count_semantic_nodes

    semantic_counts = count_semantic_nodes(driver)

    return SemanticImportResult(
        task_id=task_id,
        status=ImportStatus.success,
        errors=errors,
        stats=ImportStats(semantic_nodes=semantic_counts),
        elapsed_seconds=round(time.monotonic() - t0, 3),
    )


# ---------------------------------------------------------------------- #
# 公开入口
# ---------------------------------------------------------------------- #
def run_semantic_import_sync(
    driver: Any,
    request: SemanticImportRequest,
    *,
    task_id: Optional[str] = None,
) -> SemanticImportResult:
    """同步执行语义导入，返回 :class:`SemanticImportResult`。

    与 ``POST /api/v1/semantic/import`` 端点共享同一套图构建逻辑，
    但**不走 HTTP 回调**：调用方从返回值的 ``status`` / ``errors``
    自行决定后续动作（如更新 weave_task 状态）。

    :param driver: Neo4j driver。合并仓库后可从 ``app.state.driver`` 取，
                   或自行 ``GraphDatabase.driver(...)`` 构造。
    :param request: 语义导入请求；``task_id`` 不传时自动生成。
    """
    tid = (task_id or request.task_id or f"sem_{uuid.uuid4().hex[:12]}").strip()
    return _run_semantic_import_impl(driver, request, task_id=tid)


async def run_semantic_import_async(
    driver: Any,
    request: SemanticImportRequest,
    *,
    task_id: Optional[str] = None,
    governor: BlockingIOGovernor | None = None,
) -> SemanticImportResult:
    """异步执行语义导入（CPU 密集步骤 offload 到线程池）。

    与 :func:`run_semantic_import_sync` 行为一致；适用于已有事件循环的
    调用方（如 FastAPI 路由内、async 任务编排）。

    HTTP 调用方应传入应用的 blocking-I/O governor；进程内调用方未传入时
    回退到 ``asyncio.to_thread``，保持独立使用兼容性。
    """
    tid = (task_id or request.task_id or f"sem_{uuid.uuid4().hex[:12]}").strip()
    if governor is not None:
        return await governor.run(
            BlockingPool.GRAPH,
            "semantic.import",
            _run_semantic_import_impl,
            driver,
            request,
            task_id=tid,
        )
    return await asyncio.to_thread(
        _run_semantic_import_impl,
        driver,
        request,
        task_id=tid,
    )


__all__ = [
    "drop_semantic_for_datasource",
    "drop_physical_for_datasource",
    "run_semantic_import_sync",
    "run_semantic_import_async",
]

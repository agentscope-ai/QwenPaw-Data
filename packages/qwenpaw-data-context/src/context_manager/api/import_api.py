"""Connect API 路由：物理层数据源连接 + 导入。

路由：
- ``POST /api/v1/connect``                提交数据源连接请求
- ``POST /api/v1/connect/test-connection``  测试数据源连接
- ``GET  /api/v1/connect/status/{task_id}``  查询构建进度

所有端点通过 ``request.app.state.driver`` 获取 Neo4j 驱动。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from qwenpaw_data.context.blocking_io import BlockingIOError, BlockingPool
from qwenpaw_data.context.job_store import get_job_store

from ..config import CFG
from ..graph.import_runner import run_import
from ..net_guard import (
    CallbackUrlError,
    ensure_safe_callback_url,
    post_safe_callback,
)
from ..utils import get_logger
from .auth import internal_callback_auth_headers
from ..contracts.import_models import (
    ConnectionTestRequest,
    ConnectionTestResult,
    ImportErrorItem,
    ImportErrorLevel,
    ImportRequest,
    ImportResult,
    ImportStatus,
)

log = get_logger("api.import")

router = APIRouter(prefix="/api/v1", tags=["connect"])

async def _fire_callback(callback_url: str, result: ImportResult) -> None:
    """POST task result to the caller's callback URL (fire-and-forget)."""
    error_msg = None
    if result.errors:
        error_msg = "; ".join(e.message for e in result.errors)
    payload = {
        "task_id": result.task_id,
        "status": "SUCCESS" if result.status == ImportStatus.success else "FAILED",
        "error_msg": error_msg,
    }
    try:
        response = await post_safe_callback(
            callback_url,
            payload=payload,
            headers=internal_callback_auth_headers(callback_url),
        )
        log.info("callback %s responded %s", callback_url, response.status_code)
    except Exception as exc:
        log.warning("callback to %s failed: %s", callback_url, type(exc).__name__)


# ---------------------------------------------------------------------- #
# 持久化任务存储（SQLite WAL，多 worker 共享）
# ---------------------------------------------------------------------- #
_IMPORT_NAMESPACE = "physical-import"


def get_task_result(task_id: str) -> Optional[ImportResult]:
    record = get_job_store().get(_IMPORT_NAMESPACE, task_id)
    return ImportResult.model_validate(record.payload) if record else None


def get_task_by_idempotency_key(key: str) -> Optional[ImportResult]:
    record = get_job_store().find_by_idempotency_key(_IMPORT_NAMESPACE, key)
    return ImportResult.model_validate(record.payload) if record else None


def set_task_result(
    task_id: str,
    result: ImportResult,
    *,
    idempotency_key: str | None = None,
) -> None:
    status = (
        "failed"
        if result.status == ImportStatus.failed
        else "succeeded"
        if result.status in {ImportStatus.success, ImportStatus.degraded}
        else result.status.value
    )
    get_job_store().put(
        _IMPORT_NAMESPACE,
        task_id,
        status=status,
        payload=result.model_dump(mode="json"),
        idempotency_key=idempotency_key,
    )


def _list_task_records():
    return get_job_store().list(_IMPORT_NAMESPACE, limit=100)


# ---------------------------------------------------------------------- #
# POST /connect
# ---------------------------------------------------------------------- #
@router.post("/connect", response_model=ImportResult)
async def import_data(request: ImportRequest, req: Request) -> ImportResult:
    """提交 import 请求。

    对于快速数据源（DDL / CSV / 小量 PG 表）同步执行；
    大型数仓可改为 ``BackgroundTasks`` 异步执行。
    """
    driver = req.app.state.driver
    idempotency_key = (req.headers.get("idempotency-key") or "").strip() or None
    if idempotency_key:
        existing = await req.app.state.blocking_io.run(
            BlockingPool.FILE,
            "import.idempotency.lookup",
            get_task_by_idempotency_key,
            idempotency_key,
        )
        if existing is not None:
            return existing
    callback_url = (request.callback_url or "").strip() or None
    if callback_url:
        try:
            callback_url = ensure_safe_callback_url(callback_url)
        except CallbackUrlError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if CFG.import_dry_return:
        task_id = uuid.uuid4().hex[:12]
        result = ImportResult(task_id=task_id, status=ImportStatus.success)
        await req.app.state.blocking_io.run(
            BlockingPool.FILE,
            "import.job.succeed",
            set_task_result,
            task_id,
            result,
            idempotency_key=idempotency_key,
        )
        if callback_url:
            asyncio.create_task(_fire_callback(callback_url, result))
        return result

    task_id = uuid.uuid4().hex[:12]
    try:
        pending = ImportResult(task_id=task_id, status=ImportStatus.pending)
        await req.app.state.blocking_io.run(
            BlockingPool.FILE,
            "import.job.create",
            set_task_result,
            task_id,
            pending,
            idempotency_key=idempotency_key,
        )
        running = ImportResult(task_id=task_id, status=ImportStatus.running)
        await req.app.state.blocking_io.run(
            BlockingPool.FILE,
            "import.job.start",
            set_task_result,
            task_id,
            running,
        )
        result = await req.app.state.blocking_io.run(
            BlockingPool.GRAPH,
            "datasource.import",
            run_import,
            request,
            driver,
            task_id=task_id,
        )
        await req.app.state.blocking_io.run(
            BlockingPool.FILE,
            "import.job.finish",
            set_task_result,
            result.task_id,
            result,
        )
        if result.status in {ImportStatus.success, ImportStatus.degraded}:
            from .retrieval import invalidate_global_graph_snapshot_cache

            invalidate_global_graph_snapshot_cache()
        if callback_url:
            asyncio.create_task(_fire_callback(callback_url, result))
        return result
    except BlockingIOError:
        raise
    except Exception as exc:
        log.error("import endpoint error: %s", exc, exc_info=True)
        failed = ImportResult(
            task_id=task_id,
            status=ImportStatus.failed,
            errors=[ImportErrorItem(level=ImportErrorLevel.fatal, message=str(exc))],
        )
        await req.app.state.blocking_io.run(
            BlockingPool.FILE,
            "import.job.fail",
            set_task_result,
            failed.task_id,
            failed,
        )
        if callback_url:
            asyncio.create_task(_fire_callback(callback_url, failed))
        raise HTTPException(status_code=500, detail=str(exc))


# ---------------------------------------------------------------------- #
# POST /connect/test-connection
# ---------------------------------------------------------------------- #
@router.post("/connect/test-connection", response_model=ConnectionTestResult)
async def test_connection(
    request: ConnectionTestRequest,
    req: Request,
) -> ConnectionTestResult:
    """测试数据源连接可用性。"""
    from ..graph.adapters import get_adapter

    try:
        adapter = get_adapter(request.source, db_id="test")
        result = await req.app.state.blocking_io.run(
            BlockingPool.NETWORK,
            "datasource.test_connection",
            adapter.test_connection,
        )
        return ConnectionTestResult(
            success=result.success,
            message=result.message,
            tables_found=result.tables_found,
        )
    except BlockingIOError:
        raise
    except Exception as exc:
        return ConnectionTestResult(success=False, message=str(exc))


# ---------------------------------------------------------------------- #
# GET /connect/status/{task_id}
# ---------------------------------------------------------------------- #
@router.get("/connect/status/{task_id}", response_model=ImportResult)
async def import_status(task_id: str, request: Request) -> ImportResult:
    """查询 import 任务进度。"""
    # This route is async because the store is synchronous SQLite; use the
    # same bounded file pool as writes to preserve backpressure.
    result = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "import.job.status",
        get_task_result,
        task_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    return result


# ---------------------------------------------------------------------- #
# 便捷：列出所有任务（调试用）
# ---------------------------------------------------------------------- #
@router.get("/connect/tasks")
async def list_tasks(request: Request) -> list[dict]:
    records = await request.app.state.blocking_io.run(
        BlockingPool.FILE,
        "import.job.list",
        _list_task_records,
    )
    return [
        {
            "task_id": record.job_id,
            "status": record.payload.get("status", record.status),
            "elapsed": record.payload.get("elapsed_seconds", 0.0),
        }
        for record in records
    ]

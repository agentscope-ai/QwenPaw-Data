from __future__ import annotations

import json
import random
from datetime import datetime
from typing import Any

import aiosqlite

from datapaw.context.blocking_io import BlockingIOError, BlockingIOGovernor

from semantic_config.db import _tz
from semantic_config.errors import BadRequestError, NotFoundError
from semantic_config.models.weave_task import WeaveTaskCallback, WeaveTaskResponse, WeaveTaskSubmit
from semantic_config.pagination import Page, offset
from semantic_config.repositories import (
    biz_domain_repo,
    dataset_meta_repo,
    datasource_repo,
    dimension_repo,
    metric_lib_repo,
)
from semantic_config.repositories import weave_task_repo as repo
from semantic_config.services import weave_assembler

MODE_FULL = "FULL"
TERMINAL_STATUS = {"SUCCESS", "FAILED", "KILLED"}
_RAND_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _to_response(row: aiosqlite.Row) -> WeaveTaskResponse:
    return WeaveTaskResponse(**{k: row[k] for k in row.keys()})


def _generate_task_id() -> str:
    ts = datetime.now(_tz()).strftime("%Y%m%d%H%M%S")
    rand = "".join(random.choice(_RAND_CHARS) for _ in range(4))
    return f"TASK_{ts}_{rand}"


def _truncate(s: str | None) -> str | None:
    if s is None:
        return None
    return s if len(s) <= 1000 else s[:1000] + "..."


async def _has_weavable_objects(db: aiosqlite.Connection, datasource_id: str) -> bool:
    total = (
        await biz_domain_repo.count(db, datasource_id, None)
        + await dataset_meta_repo.count(db, datasource_id, None, None, None)
        + await dimension_repo.count(db, datasource_id, None, None)
        + await metric_lib_repo.count(db, datasource_id, None, None)
    )
    return total > 0


async def submit(
    db: aiosqlite.Connection,
    req: WeaveTaskSubmit,
    *,
    driver: Any = None,
    governor: BlockingIOGovernor | None = None,
) -> WeaveTaskResponse:
    if not req.task_name or not req.task_name.strip():
        raise BadRequestError("task_name 不能为空")
    weave_mode = (req.weave_mode or MODE_FULL).strip().upper()
    if weave_mode != MODE_FULL:
        raise BadRequestError("weave_mode 仅支持 FULL")
    if req.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if await datasource_repo.find_by_datasource_id(db, req.datasource_id) is None:
        raise BadRequestError(f"数据源不存在: datasource_id={req.datasource_id}")
    if not await _has_weavable_objects(db, req.datasource_id):
        raise BadRequestError(f"数据源 datasource_id={req.datasource_id} 下没有可编织的语义对象")

    task_id = _generate_task_id()
    payload = await weave_assembler.assemble(db, req.datasource_id, task_id)
    payload_json = json.dumps(payload, ensure_ascii=False)

    # 进程内直接调用 CM 语义导入（不再走 loopback HTTP）。CM 导入同步执行，
    # 直接据返回的 SemanticImportResult 定状态——不依赖异步回调的到达时序
    # （回调端点 /api/semantic-config/weave-task/callback 仍保留，供远程/兜底路径幂等更新）。
    # 调用失败不阻断留痕：记 FAILED + error_msg。
    status = "RUNNING"
    error_msg = None
    try:
        result = await _run_import_in_process(
            driver,
            payload,
            task_id,
            governor=governor,
        )
        status, error_msg = _map_import_result(result)
    except BlockingIOError:
        raise
    except Exception as e:  # noqa: BLE001
        status = "FAILED"
        error_msg = _truncate(str(e))

    await repo.insert(
        db, task_id, req.task_name, req.datasource_id, weave_mode, status, payload_json, error_msg
    )
    row = await repo.find_response_by_task_id(db, task_id)
    return _to_response(row)


async def _run_import_in_process(
    driver: Any,
    payload: dict,
    task_id: str,
    *,
    governor: BlockingIOGovernor | None = None,
) -> Any:
    """进程内执行 CM 语义导入，返回 SemanticImportResult。

    用 ``weave_assembler`` 组装的整库 payload 直接构造 CM 的
    :class:`SemanticImportRequest`（等价于之前 HTTP body 的 pydantic 校验），
    再调用 :func:`run_semantic_import_async`——CPU 密集的 Neo4j 写入会被
    offload 到线程池，不阻塞事件循环。
    """
    if driver is None:
        raise RuntimeError("Neo4j driver 未初始化，无法执行语义导入（请检查 Neo4j 是否已启动）")
    from context_manager.contracts.import_models import SemanticImportRequest
    from context_manager.graph.semantic_import_service import run_semantic_import_async

    request = SemanticImportRequest.model_validate(payload)
    return await run_semantic_import_async(
        driver,
        request,
        task_id=task_id,
        governor=governor,
    )


def _map_import_result(result: Any) -> tuple[str, str | None]:
    """把 CM SemanticImportResult 映射成 weave 状态。

    CM ImportStatus 枚举：success / failed / degraded / pending / running。
    与 semantic_api 回调逻辑对齐：success→SUCCESS，failed→FAILED（拼接 errors），
    其余（degraded/pending/running）原样大写透传。
    """
    from context_manager.contracts.import_models import ImportStatus

    if result.status == ImportStatus.success:
        return "SUCCESS", None
    if result.status == ImportStatus.failed:
        msg = "; ".join(e.message for e in result.errors) if result.errors else "CM 导入失败"
        return "FAILED", _truncate(msg)
    return str(result.status.value).upper(), None


async def list_page(db, datasource_name, task_name, page, size) -> Page[WeaveTaskResponse]:
    total = await repo.count(db, datasource_name, task_name)
    rows = await repo.find_page(db, datasource_name, task_name, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def kill(db: aiosqlite.Connection, task_id: str) -> WeaveTaskResponse:
    task = await repo.find_by_task_id(db, task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: task_id={task_id}")
    if task["status"] in TERMINAL_STATUS:
        raise BadRequestError(f"任务已处于 {task['status']} 状态，无法杀死")
    # killOnCm：占位空实现，协议待 CM 提供
    await repo.update_status(db, task_id, "KILLED", task["error_msg"])
    return _to_response(await repo.find_response_by_task_id(db, task_id))


async def callback(db: aiosqlite.Connection, req: WeaveTaskCallback) -> WeaveTaskResponse:
    if not req.task_id or not req.task_id.strip():
        raise BadRequestError("task_id 不能为空")
    task = await repo.find_by_task_id(db, req.task_id)
    if task is None:
        raise NotFoundError(f"任务不存在: task_id={req.task_id}")
    await repo.update_status(db, req.task_id, req.status, req.error_msg)
    return _to_response(await repo.find_response_by_task_id(db, req.task_id))

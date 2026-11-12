from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import datetime
from typing import Any

import aiosqlite
from context_manager.heartbeat import Heartbeat
from context_manager.executor import BoundedExecutor
from context_manager.store import StoreError
from context_manager.transaction.recovery import RecoveryService
from qwenpaw_data_auth import Principal
from sqlalchemy.ext.asyncio import async_sessionmaker

from semantic_config.db import _tz
from semantic_config.errors import BadRequestError, NotFoundError
from semantic_config.models.weave_task import (
    WeaveTaskResponse,
    WeaveTaskSubmit,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import (
    biz_domain_repo,
    dataset_meta_repo,
    datasource_repo,
    table_column_repo,
)
from semantic_config.repositories import weave_task_repo as repo
from semantic_config.services import weave_assembler

MODE_FULL = "FULL"
MODE_INCREMENTAL = "INCREMENTAL"
SUPPORTED_MODES = {MODE_FULL, MODE_INCREMENTAL}
TERMINAL_STATUS = {"SUCCESS", "FAILED", "KILLED", "DEGRADED"}
_RAND_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

log = logging.getLogger("semantic_config.weave_task")

#: 终态回写只接管仍在 RUNNING 的行（CAS）：晚归的 worker 不得覆盖 KILLED，
#: 也不得覆盖同 task_id 上更晚一轮写入的终态。
_PERSIST_EXPECT_STATUS = ("RUNNING",)

#: 终态回写的重试间隔（秒）；尝试次数 = len + 1。每次重试换新会话——
#: 连接已失效时同一会话不可复用，池的 pre_ping 会在借出时探活重连。
_TERMINAL_PERSIST_DELAYS = (0.5, 2.0)


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


async def _has_weavable_objects(
    db: aiosqlite.Connection, datasource_id: str, tenant_id: str, workspace_id: str
) -> bool:
    # Shared domains and their definitions can be woven before datasets exist.
    total = (
        await biz_domain_repo.count(db, None, tenant_id, workspace_id)
        + await dataset_meta_repo.count(
            db, datasource_id, None, None, tenant_id, workspace_id
        )
        + await table_column_repo.count(
            db, datasource_id, None, tenant_id, workspace_id
        )
    )
    return total > 0


async def submit(
    db: aiosqlite.Connection,
    req: WeaveTaskSubmit,
    principal: Principal,
    *,
    executor: BoundedExecutor | None = None,
    graph_access: Any = None,
    recovery: RecoveryService | None = None,
) -> WeaveTaskResponse:
    require_admin(principal)
    if not req.task_name or not req.task_name.strip():
        raise BadRequestError("task_name 不能为空")
    weave_mode = (req.weave_mode or MODE_FULL).strip().upper()
    if weave_mode not in SUPPORTED_MODES:
        raise BadRequestError(
            f"weave_mode 仅支持 {', '.join(sorted(SUPPORTED_MODES))}，收到: {weave_mode}"
        )
    if req.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    ds = await datasource_repo.find_by_datasource_id(
        db, req.datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={req.datasource_id}")
    # 提交时捕获任务归属身份，带入后续（同步）执行；不在后台再取 current_principal。
    tenant_id, workspace_id, created_by = (
        ds["tenant_id"],
        ds["workspace_id"],
        principal.user_id,
    )
    if not await _has_weavable_objects(db, req.datasource_id, tenant_id, workspace_id):
        raise BadRequestError(
            "当前工作区没有共享业务定义，所选数据源也没有可编织的数据对象"
        )

    if recovery is not None and graph_access is not None:
        try:
            await recovery.require_ready(graph_access[1])
        except StoreError as exc:
            if exc.code == "recovery_workspace_busy":
                raise BadRequestError(
                    "当前工作区有未完成的图恢复，请等待恢复结束"
                ) from exc
            raise
    task_id = _generate_task_id()
    await repo.insert(
        db,
        task_id,
        req.task_name,
        req.datasource_id,
        weave_mode,
        "RUNNING",
        None,
        None,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        created_by=created_by,
    )
    await db.commit()
    _spawn(
        task_id,
        _weave_worker(
            task_id=task_id,
            datasource_id=req.datasource_id,
            drop_first=weave_mode == MODE_FULL,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=created_by,
            graph_access=graph_access,
            session_factory=async_sessionmaker(db.bind, expire_on_commit=False),
            recovery=recovery,
            executor=executor,
        ),
    )
    log.info(
        "weave submitted task=%s ds=%s mode=%s tenant=%s ws=%s",
        task_id,
        req.datasource_id,
        weave_mode,
        tenant_id,
        workspace_id,
    )

    row = await repo.find_response_by_task_id(db, task_id, tenant_id, workspace_id)
    return _to_response(row)


#: 在途织入后台任务引用（防被 GC；done 回调自动摘除）。
_BACKGROUND_TASKS: set[asyncio.Task] = set()
_LOCAL_WEAVES: dict[str, asyncio.Task] = {}


def _spawn(task_id: str, coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    _LOCAL_WEAVES[task_id] = task

    def _done(done: asyncio.Task) -> None:
        _BACKGROUND_TASKS.discard(done)
        if _LOCAL_WEAVES.get(task_id) is done:
            _LOCAL_WEAVES.pop(task_id, None)

    task.add_done_callback(_done)
    return task


async def _new_session():
    """后台 worker 自建会话（请求 session 随响应提交关闭，不可复用）。

    单独抽出便于测试注入共享 in-memory 引擎（见 autoexplore.trigger._new_session）。
    """
    from semantic_config.engine import get_session_factory

    return get_session_factory()()


async def _weave_worker(
    *,
    task_id,
    datasource_id,
    drop_first,
    tenant_id,
    workspace_id,
    created_by,
    graph_access,
    session_factory,
    recovery: RecoveryService | None = None,
    executor: BoundedExecutor | None = None,
) -> None:
    if executor is None:
        raise RuntimeError("weave worker requires the background execution pool")
    status, error_msg, persisted = "FAILED", None, None

    async def _touch() -> None:
        db = session_factory()
        try:
            await repo.touch_heartbeat(
                db, task_id, tenant_id=tenant_id, workspace_id=workspace_id
            )
            await db.commit()
        finally:
            await db.close()

    async def _row():
        db = session_factory()
        try:
            return await repo.find_by_task_id(db, task_id, tenant_id, workspace_id)
        finally:
            await db.close()

    async def _still_running() -> bool:
        row = await _row()
        return row is not None and row["status"] == "RUNNING"

    async def _cancel_requested() -> bool:
        row = await _row()
        return row is not None and bool(row["cancel_requested"])

    heartbeat = Heartbeat(_touch)
    heartbeat.start()
    try:
        try:
            if not await _still_running():
                return
            if await _cancel_requested():
                status, error_msg = "KILLED", None
            else:
                payload = await executor.run(
                    _assemble_and_stash,
                    task_id=task_id,
                    datasource_id=datasource_id,
                    drop_first=drop_first,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    updated_by=created_by,
                )
                if not await _still_running():
                    return
                if await _cancel_requested():
                    status, error_msg = "KILLED", None
                else:
                    if graph_access is None:
                        raise RuntimeError("语义导入缺少服务和已签发的 scope")
                    importer, scope = graph_access
                    if payload["task_id"] != task_id:
                        raise ValueError("语义导入 task_id 与编织任务不一致")
                    warnings = []

                    async def before_commit(plan):
                        if not await _still_running() or await _cancel_requested():
                            raise asyncio.CancelledError()
                        warnings.extend(
                            warning
                            for warning in plan.warnings
                            if warning not in warnings
                        )

                    if recovery is None:
                        await importer.import_snapshot(
                            payload, scope, before_commit=before_commit
                        )
                        if await _cancel_requested():
                            status, error_msg = "KILLED", None
                        else:
                            status = "DEGRADED" if warnings else "SUCCESS"
                            error_msg = (
                                _truncate("; ".join(warnings)) if warnings else None
                            )
                    else:
                        async with recovery.run(
                            scope, task_type="weave", task_id=task_id, rid=task_id
                        ) as recovery_session:
                            await importer.import_snapshot(
                                payload,
                                scope,
                                before_commit=before_commit,
                                recovery_session=recovery_session,
                            )
                            if await _cancel_requested():
                                status, error_msg = "KILLED", None
                            elif warnings:
                                status = "DEGRADED"
                                error_msg = _truncate("; ".join(warnings))
                                recovery_session.set_result(status, error_msg)
                            else:
                                status, error_msg = "SUCCESS", None
        except asyncio.CancelledError:
            if await _cancel_requested():
                status, error_msg = "KILLED", None
            else:
                error_msg = "执行已中断，请重新提交编织"
        except StoreError as exc:
            if exc.code == "recovery_workspace_busy":
                error_msg = "当前工作区有未完成的图恢复，请等待恢复结束"
                log.warning("weave blocked by pending recovery task=%s", task_id)
            else:
                error_msg = _truncate(str(exc))
                log.exception("weave failed task=%s", task_id)
        except Exception as exc:
            error_msg = _truncate(str(exc))
            log.exception("weave failed task=%s", task_id)
        persisted = await _persist_terminal(
            task_id,
            status,
            error_msg,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            updated_by=created_by,
        )
    except Exception:
        log.exception("weave finalization failed task=%s", task_id)
    finally:
        await heartbeat.stop()
    if persisted in {"SUCCESS", "DEGRADED"}:
        await _fire_autoexplore_hook(
            task_id=task_id,
            datasource_id=datasource_id,
            status=persisted,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            graph_access=graph_access,
        )


async def _assemble_and_stash(
    *,
    task_id: str,
    datasource_id: str,
    drop_first: bool,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> dict:
    """组装 payload 并在图导入开始前提交审计。导入期间不持有控制面事务。"""
    t0 = time.monotonic()
    db = await _new_session()
    try:
        payload = await weave_assembler.assemble(
            db,
            datasource_id,
            task_id,
            tenant_id,
            workspace_id,
            drop_semantic_first=drop_first,
        )
        audit_payload = {
            **payload,
            "datasource": {
                k: v for k, v in payload.get("datasource", {}).items() if k != "config"
            },
        }
        payload_json = json.dumps(audit_payload, ensure_ascii=False)
        row = await repo.find_by_task_id(db, task_id, tenant_id, workspace_id)
        if row is None or row["status"] != "RUNNING":
            raise asyncio.CancelledError()
        await repo.update_export_payload(
            db,
            task_id,
            payload_json,
            updated_by=updated_by,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
        )
        await db.commit()
    finally:
        await db.close()
    log.info(
        "weave assemble done task=%s ds=%s payload_bytes=%d assemble_seconds=%.3f",
        task_id,
        datasource_id,
        len(payload_json.encode()),
        time.monotonic() - t0,
    )
    return payload


async def _persist_terminal(
    task_id: str,
    status: str,
    error_msg: str | None,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> str | None:
    """终态只更新仍是 RUNNING 的这一行。已经被写成 KILLED 或 FAILED 的不覆盖。"""
    attempts = len(_TERMINAL_PERSIST_DELAYS) + 1
    t0 = time.monotonic()
    last_error: Exception | None = None

    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(_TERMINAL_PERSIST_DELAYS[attempt - 1])
        db = await _new_session()
        try:
            observed = await repo.find_response_by_task_id(
                db, task_id, tenant_id, workspace_id
            )
            state = None if observed is None else observed.get("_recovery_state")
            if state not in (None, "CLOSED"):
                return None
            if observed is not None and observed["status"] != "RUNNING":
                await db.commit()
                return observed["status"]
            rowcount = await repo.update_status(
                db,
                task_id,
                status,
                error_msg,
                updated_by=updated_by,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                expect_status=_PERSIST_EXPECT_STATUS,
            )
            await db.commit()
        except Exception as e:  # noqa: BLE001 — 连接失效等瞬时故障，换新会话重试
            last_error = e
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            log.warning(
                "weave terminal persist attempt %d/%d failed task=%s status=%s: %s",
                attempt + 1,
                attempts,
                task_id,
                status,
                e,
            )
        else:
            if rowcount == 0:
                log.warning(
                    "weave terminal persist skipped task=%s status=%s "
                    "(row missing or no longer RUNNING)",
                    task_id,
                    status,
                )
                return None
            log.info(
                "weave terminal persisted task=%s status=%s attempt=%d persist_seconds=%.3f",
                task_id,
                status,
                attempt + 1,
                time.monotonic() - t0,
            )
            return status
        finally:
            await db.close()

    log.error(
        "weave terminal persist exhausted task=%s status=%s attempts=%d "
        "persist_seconds=%.3f last_error=%s — task row left RUNNING",
        task_id,
        status,
        attempts,
        time.monotonic() - t0,
        last_error,
    )
    return None


async def _fire_autoexplore_hook(
    *,
    task_id: str,
    datasource_id: str,
    status: str,
    tenant_id: str,
    workspace_id: str,
    graph_access: Any,
) -> None:
    """mg-088 物理层自动探查：首织完成 → fire-and-forget 异步触发（spec §2.1）。

    钩子永不阻断织入 / 不染 weave 状态——探查与织入解耦，失败仅 warning。
    自建会话：终态回写的会话已随其短事务关闭，不可复用。
    """
    db = await _new_session()
    try:
        from semantic_config.services.autoexplore import trigger as autoexplore_trigger

        await autoexplore_trigger.maybe_trigger_first_weave_explore(
            db,
            datasource_id=datasource_id,
            task_id=task_id,
            status=status,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            graph_access=(graph_access[0].physical_mapping, graph_access[1]),
        )
    except Exception:  # noqa: BLE001 — 探查触发失败不得影响 weave
        log.warning(
            "autoexplore hook failed ds=%s task=%s (ignored)", datasource_id, task_id
        )
    finally:
        await db.close()


def _task_response(row):
    response = _to_response(row)
    state = row.get("_recovery_state")
    if response.status == "RUNNING" and state not in (None, "CLOSED"):
        response.error_msg = "图恢复尚未完成，请等待恢复结束"
    return response


async def get(
    db: aiosqlite.Connection, task_id: str, principal: Principal
) -> WeaveTaskResponse:
    """按 task_id 查单个任务（异步 submit 后前端轮询终态用）。"""
    tid, wid = principal.tenant_id, principal.active_workspace_id
    row = await repo.find_response_by_task_id(db, task_id, tid, wid)
    if row is None:
        raise NotFoundError(f"任务不存在: task_id={task_id}")
    return _task_response(row)


async def list_page(
    db, datasource_name, task_name, page, size, principal: Principal
) -> Page[WeaveTaskResponse]:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, datasource_name, task_name, tid, wid)
    rows = await repo.find_page(
        db, datasource_name, task_name, size, offset(page, size), tid, wid
    )
    return Page(
        records=[_task_response(r) for r in rows], total=total, page=page, size=size
    )


async def kill(
    db: aiosqlite.Connection, task_id: str, principal: Principal
) -> WeaveTaskResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    task = await repo.find_by_task_id(db, task_id, tid, wid)
    if task is None:
        raise NotFoundError(f"任务不存在: task_id={task_id}")
    if task["status"] in TERMINAL_STATUS:
        raise BadRequestError(f"任务已处于 {task['status']} 状态，无法杀死")
    updated = await repo.request_cancel(
        db,
        task_id,
        updated_by=principal.user_id,
        tenant_id=tid,
        workspace_id=wid,
        expect_status=task["status"],
    )
    await db.commit()
    if not updated:
        fresh = await repo.find_by_task_id(db, task_id, tid, wid)
        if fresh is None:
            raise NotFoundError(f"任务不存在: task_id={task_id}")
        if fresh["status"] in TERMINAL_STATUS:
            raise BadRequestError(f"任务已处于 {fresh['status']} 状态，无法杀死")
        raise BadRequestError("任务状态已变化，请刷新后重试")
    response = await get(db, task_id, principal)
    local = _LOCAL_WEAVES.get(task_id)
    if local is not None and not local.done():
        local.cancel()
    return response

"""探查任务编排：数据库租约认领、跨实例停止、受执行归属保护的结果写回。

进程内注册表只保留协程引用和合作式取消提示，不参与忙闲判定。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime

import aiosqlite
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from context_manager.heartbeat import Heartbeat, lock_scope, stale_cutoff
from context_manager.model.usage import bind_usage_context
from profiling.db import open_probe_conn
from profiling.tables import probe_task
from profiling.models.probe import (
    STAGE_TERMINAL,
    STATUS_NOT_STARTED,
    STATUS_PROBING,
    STATUS_REVIEWING,
    STATUS_SKIPPED,
    TASK_FAILED,
    TASK_RUNNING,
    TASK_STOPPED,
    TASK_SUCCESS,
    ProbeTaskResponse,
    task_row_to_response,
)
from profiling.provider import load_provider
from profiling.repositories import probe_stage_repo as stage_repo
from profiling.repositories import probe_task_repo as task_repo
from profiling.repositories import staging_repo
from profiling.services import context as ctx_builder
from profiling.services.sink import DbProbeSink
from profiling.stages import STAGE_DEFS
from qwenpaw_data_auth import Principal
from semantic_config.errors import BadRequestError, NotFoundNoBody
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo as ds_repo

logger = logging.getLogger(__name__)

# 仅保活及合作式取消；单飞和执行权由数据库租约决定。
_REGISTRY: dict[tuple[str, ...], asyncio.Task] = {}
_TOKENS: dict[tuple[str, ...], "_CancelToken"] = {}

_PREREQ_MSG = {
    "BIZ_DOMAIN": "请先完成业务域探查",
    "DATASET": "请先完成数据集探查",
    "DIMENSION_CALIBER": "请先完成维度口径探查",
    "DIMENSION_MANAGE": "请先完成维度管理探查",
    "METRIC_CALIBER": "请先完成指标口径探查",
}


class _CancelToken:
    def __init__(self, task_id: str) -> None:
        self.task_id = task_id
        self.cancelled = False

    def __call__(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError()


def _key(
    tenant_id: str, workspace_id: str, datasource_id: str, stage: str
) -> tuple[str, ...]:
    return tenant_id, workspace_id, datasource_id, stage


class ProbeLost(RuntimeError):
    """This probe row is no longer RUNNING, so the worker must not write success."""


class _ProbeRun:
    """Heartbeat and status check for one probe task. Not a lease."""

    def __init__(self, task_id: str) -> None:
        self.execution_id = task_id
        self.task_id = task_id

    async def guard(self, conn) -> None:
        """Hold the running row until this transaction commits its write."""
        if await task_repo.touch_heartbeat(conn, self.task_id) != 1:
            raise ProbeLost(self.task_id)

    @asynccontextmanager
    async def running(self, session_factory):
        async def touch() -> None:
            db = session_factory()
            try:
                await task_repo.touch_heartbeat(db, self.task_id)
                await db.commit()
            finally:
                await db.close()

        heartbeat = Heartbeat(touch)
        heartbeat.start()
        try:
            yield
        finally:
            await heartbeat.stop()


async def _restore_owned_stage(
    conn,
    *,
    datasource_id: str,
    stage: str,
    task_id: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
) -> None:
    """Leave PROBING only while this task still owns the stage."""
    from semantic_config.tables import metadata

    table = metadata.tables[STAGE_DEFS[stage].table]
    res = await conn.execute(
        select(table.c.id)
        .where(
            table.c.datasource_id == datasource_id,
            table.c.tenant_id == tenant_id,
            table.c.workspace_id == workspace_id,
            table.c.is_deleted == 0,
        )
        .limit(1)
    )
    restored = STATUS_REVIEWING if res.first() is not None else STATUS_NOT_STARTED
    await stage_repo.update_status(
        conn,
        datasource_id,
        stage,
        restored,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        updated_by=user_id,
        expected_status=STATUS_PROBING,
        expected_task_id=task_id,
    )


async def _stage_is_skipped(
    conn: aiosqlite.Connection,
    datasource_id: str,
    stage: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    row = await stage_repo.find_one(conn, datasource_id, stage, tenant_id, workspace_id)
    return row is not None and row["status"] == STATUS_SKIPPED


def _gen_task_id() -> str:
    return f"PROBE_{datetime.now().strftime('%Y%m%d%H%M%S')}_{secrets.token_hex(2).upper()}"


async def _require_ds(db, datasource_id, principal) -> aiosqlite.Row:
    ds = await ds_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise NotFoundNoBody()
    return ds


async def _check_prereq(db, datasource_id, stage, principal) -> None:
    prereq = STAGE_DEFS[stage].prereq
    if not prereq:
        return
    smap = await stage_repo.status_map(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if smap.get(prereq, "NOT_STARTED") not in STAGE_TERMINAL:
        raise BadRequestError(_PREREQ_MSG.get(prereq, "请先完成上游阶段"))


async def submit(
    db: aiosqlite.Connection,
    datasource_id: str,
    stage: str,
    principal: Principal,
    *,
    mode: str = "create",
) -> ProbeTaskResponse:
    """``mode``：``create``（首次探查）/ ``rerun``（重新探查）/ ``refine``（完善探查）。

    业务域阶段三种模式都是替换式写回（未锁定行在新行落库后删除）；
    非域阶段仅 ``rerun`` 先做 staging 清理。算法侧把 ``rerun`` 映射为 create。
    """
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if mode not in ("create", "rerun", "refine"):
        raise BadRequestError(f"未知探查模式: {mode}")

    task_id = _gen_task_id()
    await lock_scope(db, f"probe:{tid}:{wid}:{datasource_id}:{stage}")
    if await task_repo.has_fresh_running(
        db,
        datasource_id=datasource_id,
        stage=stage,
        tenant_id=tid,
        workspace_id=wid,
    ):
        raise BadRequestError("该阶段已有探查任务运行中")
    cutoff = stale_cutoff()
    await db.execute(
        update(probe_task)
        .where(
            probe_task.c.tenant_id == tid,
            probe_task.c.workspace_id == wid,
            probe_task.c.datasource_id == datasource_id,
            probe_task.c.stage == stage,
            probe_task.c.status == TASK_RUNNING,
            probe_task.c.is_deleted == 0,
            or_(
                probe_task.c.heartbeat_at.is_(None),
                probe_task.c.heartbeat_at < cutoff,
            ),
        )
        .values(
            status=TASK_FAILED,
            error_msg="执行中断",
            finished_at=datetime.now().isoformat(timespec="seconds"),
            updated_at=datetime.now().isoformat(timespec="seconds"),
        )
    )
    await stage_repo.ensure_stages(
        db, datasource_id, tenant_id=tid, workspace_id=wid, created_by=principal.user_id
    )
    await _check_prereq(db, datasource_id, stage, principal)

    msg = {
        "create": "任务已提交",
        "rerun": "重新探查已提交",
        "refine": "完善探查已提交",
    }[mode]
    await task_repo.insert_running(
        db,
        task_id=task_id,
        datasource_id=datasource_id,
        stage=stage,
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
        message=msg,
    )
    await stage_repo.update_status(
        db,
        datasource_id,
        stage,
        STATUS_PROBING,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
        current_task_id=task_id,
        set_task=True,
    )
    # 提交请求连接，确保后台独立连接可见 RUNNING 行与 PROBING 状态。
    await db.commit()

    token = _CancelToken(task_id)
    key = _key(tid, wid, datasource_id, stage)
    _TOKENS[key] = token
    task = asyncio.create_task(
        _run_probe(
            datasource_id=datasource_id,
            stage=stage,
            task_id=task_id,
            tenant_id=tid,
            workspace_id=wid,
            user_id=principal.user_id,
            mode=mode,
            token=token,
            principal=principal,
            lease=_ProbeRun(task_id),
            session_factory=async_sessionmaker(db.bind, expire_on_commit=False),
        )
    )
    _REGISTRY[key] = task
    return ProbeTaskResponse(
        task_id=task_id,
        datasource_id=datasource_id,
        stage=stage,
        status="RUNNING",
        progress=0,
        message=msg,
    )


async def _rerun_cleanup(
    conn, datasource_id, stage, *, tenant_id, workspace_id, user_id
) -> None:
    """按 §6.3 清理 staging：数据集整体清空；口径/管理保留 CONFIRMED。

    业务域不走这里 —— 它是替换式写回（见 ``_run_probe``）。"""
    table = STAGE_DEFS[stage].table
    if stage == "DATASET":
        await staging_repo.soft_delete_all(
            conn,
            table,
            datasource_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            updated_by=user_id,
        )
        # 字段子表随 dataset 一起重探：不清会让新旧两轮字段行并存，
        # 陈旧字段（本轮已不产出的列 / 旧描述）抢先占住 promote 去重键
        await staging_repo.soft_delete_all(
            conn,
            "staging_dataset_field",
            datasource_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            updated_by=user_id,
        )
    else:
        await staging_repo.soft_delete_non_confirmed(
            conn,
            table,
            datasource_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            updated_by=user_id,
        )
    await conn.commit()


async def _settle_probe_terminal(
    *,
    datasource_id: str,
    stage: str,
    task_id: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    status: str,
    error_msg: str | None,
    stage_status: str | None = None,
    restore_after_failure: bool = False,
) -> None:
    """Finish one probe under the same lock as submit and stop.

    Callers commit any progress transaction before entering, so this lock is
    not taken while the task row is already locked.
    """
    async with await open_probe_conn() as conn:
        await lock_scope(
            conn, f"probe:{tenant_id}:{workspace_id}:{datasource_id}:{stage}"
        )
        skipped = await _stage_is_skipped(
            conn, datasource_id, stage, tenant_id, workspace_id
        )
        changed = await task_repo.finish_if_running(
            conn,
            task_id,
            TASK_STOPPED if skipped else status,
            None if skipped else error_msg,
        )
        if changed and not skipped:
            if restore_after_failure:
                await _restore_owned_stage(
                    conn,
                    datasource_id=datasource_id,
                    stage=stage,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                )
            elif stage_status is not None:
                await stage_repo.update_status(
                    conn,
                    datasource_id,
                    stage,
                    stage_status,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    updated_by=user_id,
                    expected_status=STATUS_PROBING,
                    expected_task_id=task_id,
                )
        elif not skipped:
            await _restore_owned_stage(
                conn,
                datasource_id=datasource_id,
                stage=stage,
                task_id=task_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        await conn.commit()


async def _finish_interrupted_probe(
    *,
    datasource_id: str,
    stage: str,
    task_id: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    status: str,
    error_msg: str | None,
) -> None:
    """探查会话清理后，用独立事务保存失败/停止状态，不覆盖已提交的任务终态。"""
    try:
        await _settle_probe_terminal(
            datasource_id=datasource_id,
            stage=stage,
            task_id=task_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            status=status,
            error_msg=error_msg,
            restore_after_failure=True,
        )
    except ProbeLost:
        logger.info("probe execution no longer owned task_id=%s", task_id)
    except Exception:
        # 数据库仍不可用时不能保证落库，但不能再吞掉/取代已记录的原始探查异常。
        logger.exception(
            "probe terminal status update failed task_id=%s datasource_id=%s stage=%s "
            "tenant_id=%s workspace_id=%s",
            task_id,
            datasource_id,
            stage,
            tenant_id,
            workspace_id,
        )


async def _run_probe(*, lease, session_factory, **kwargs):
    with bind_usage_context(
        source="语义探查",
        source_step=f"semantic_profiling#{lease.execution_id}",
        session_id="",
        chat_id="",
        probe=False,
    ):
        try:
            async with lease.running(session_factory):
                await _run_owned_probe(lease=lease, **kwargs)
        except ProbeLost:
            logger.warning("probe ownership lost task_id=%s", lease.execution_id)


async def _run_owned_probe(
    *,
    datasource_id: str,
    stage: str,
    task_id: str,
    tenant_id: str,
    workspace_id: str,
    user_id: str,
    mode: str,
    token: _CancelToken,
    principal: Principal,
    lease: _ProbeRun,
) -> None:
    key = _key(tenant_id, workspace_id, datasource_id, stage)
    conn = None
    interrupted_status = None
    error_msg = None
    try:
        conn = await open_probe_conn()
        await lease.guard(conn)
        await conn.commit()
        ds = await ds_repo.find_by_datasource_id(
            conn, datasource_id, tenant_id, workspace_id
        )
        if ds is None:
            await conn.rollback()
            await _settle_probe_terminal(
                datasource_id=datasource_id,
                stage=stage,
                task_id=task_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
                status=TASK_FAILED,
                error_msg="数据源不存在",
                restore_after_failure=True,
            )
            return

        # 业务域阶段：替换式写回。探查前记下未锁定行（非 CONFIRMED：PENDING /
        # SKIPPED，含 HUMAN）的 id；事实照常带上它们（refine 以 PENDING 为输入，
        # create/rerun 的 handler 只认 CONFIRMED 锁定行）；新行落库成功后再软删
        # 这批旧行 —— LLM 失败时旧行原封不动。仅 CONFIRMED 全程不碰。
        # 其余阶段沿用 §6.3：仅 rerun 先清理再取数。
        replace_ids: list[int] = []
        table = STAGE_DEFS[stage].table
        if mode == "refine":
            replace_ids = await staging_repo.list_unlocked_ids(
                conn, table, datasource_id, tenant_id, workspace_id
            )
        elif mode == "rerun":
            await lease.guard(conn)
            await _rerun_cleanup(
                conn,
                datasource_id,
                stage,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                user_id=user_id,
            )
            await conn.commit()
        else:
            pass

        ctx = await ctx_builder.build_context(
            conn, ds, tenant_id, workspace_id, principal
        )
        await conn.commit()
        ctx.artifact_id = task_id
        ctx.mode = mode
        ctx.raise_if_cancelled = token

        # 进度回写必须用独立会话。LLM 单元并发会 fire-and-forget 多次
        # ``_report``；SQLAlchemy AsyncSession 不能并行 commit，叠在探查
        # 主会话上会 IllegalStateChangeError，且异常落在未 await 的 Task 上。
        progress_lock = asyncio.Lock()

        async def _report(pct: int, message: str) -> None:
            async with progress_lock:
                session = await open_probe_conn()
                try:
                    await lease.guard(session)
                    await task_repo.update_progress(session, task_id, pct, message)
                    await session.commit()
                except (ProbeLost, asyncio.CancelledError):
                    await session.rollback()
                except Exception:
                    logger.exception("probe progress update failed task_id=%s", task_id)
                finally:
                    await session.close()

        ctx.report_progress = _report

        sink = DbProbeSink(
            conn,
            ctx,
            datasource_id=datasource_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=user_id,
            task_id=task_id,
            execution=lease,
        )
        provider = load_provider()
        probe_fn = getattr(provider, STAGE_DEFS[stage].probe_fn)
        await probe_fn(ctx, sink)
        token()
        await lease.guard(conn)
        if replace_ids:
            n = await staging_repo.batch_soft_delete(
                conn,
                table,
                replace_ids,
                datasource_id=datasource_id,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                updated_by=user_id,
            )
            logger_msg = f"替换式写回：已删除上一轮未锁定行 {n} 行（{mode}）"
            await task_repo.update_progress(conn, task_id, 100, logger_msg)
        await conn.commit()
        await _settle_probe_terminal(
            datasource_id=datasource_id,
            stage=stage,
            task_id=task_id,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=user_id,
            status=TASK_SUCCESS,
            error_msg=None,
            stage_status=STATUS_REVIEWING,
        )
        return
    except asyncio.CancelledError:
        token.cancelled = True
        interrupted_status = TASK_STOPPED
    except ProbeLost:
        token.cancelled = True
        interrupted_status = TASK_FAILED
        error_msg = "执行中断"
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "probe failed task_id=%s datasource_id=%s stage=%s tenant_id=%s workspace_id=%s",
            task_id,
            datasource_id,
            stage,
            tenant_id,
            workspace_id,
        )
        interrupted_status = TASK_FAILED
        error_msg = str(e)[:500]
    finally:
        try:
            if conn is not None:
                if interrupted_status is not None:
                    try:
                        await conn.rollback()
                    except Exception:
                        logger.exception("probe rollback failed task_id=%s", task_id)
                try:
                    await conn.close()
                except Exception:
                    logger.exception("probe session close failed task_id=%s", task_id)
            if interrupted_status is not None:
                await _finish_interrupted_probe(
                    datasource_id=datasource_id,
                    stage=stage,
                    task_id=task_id,
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    status=interrupted_status,
                    error_msg=error_msg,
                )
        finally:
            if _REGISTRY.get(key) is asyncio.current_task():
                _REGISTRY.pop(key, None)
                _TOKENS.pop(key, None)


async def stop(
    db: aiosqlite.Connection, datasource_id: str, stage: str, principal: Principal
) -> dict:
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    key = _key(tid, wid, datasource_id, stage)
    await lock_scope(db, f"probe:{tid}:{wid}:{datasource_id}:{stage}")
    latest = await task_repo.find_latest(db, datasource_id, stage, tid, wid)
    task_id = latest["task_id"] if latest else None
    stopped = False
    if latest is not None and latest["status"] == TASK_RUNNING:
        stopped = await task_repo.finish_if_running(db, task_id, TASK_STOPPED)
        if stopped:
            await _restore_owned_stage(
                db,
                datasource_id=datasource_id,
                stage=stage,
                task_id=task_id,
                tenant_id=tid,
                workspace_id=wid,
                user_id=principal.user_id,
            )
    await db.commit()
    token = _TOKENS.get(key)
    if stopped and token is not None and token.task_id == task_id:
        token.cancelled = True
    stage_row = await stage_repo.find_one(db, datasource_id, stage, tid, wid)
    return {
        "stage": stage,
        "status": stage_row["status"] if stage_row else STATUS_NOT_STARTED,
        "task_id": task_id,
        "cancel_requested": stopped,
    }


async def get_task(
    db: aiosqlite.Connection, datasource_id: str, stage: str, principal: Principal
) -> ProbeTaskResponse:
    await _require_ds(db, datasource_id, principal)
    latest = await task_repo.find_latest(
        db, datasource_id, stage, principal.tenant_id, principal.active_workspace_id
    )
    return task_row_to_response(latest, datasource_id, stage)

"""probe_task 仓储：一次异步探查一行，留痕 + 进度轮询。

写入统一带 tenant_id/workspace_id/created_by（提交时捕获的身份，见 02-SPEC §13.8）。
后台任务用独立连接更新 progress/finish，故这些函数只依赖传入的 conn。
"""

from __future__ import annotations

import time

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from context_manager.heartbeat import is_fresh
from context_manager.utils.pod import current_pod_name
from profiling.models.probe import TASK_RUNNING
from profiling.tables import probe_task
from semantic_config.db import now_iso


async def insert_running(
    db: AsyncSession,
    *,
    task_id: str,
    datasource_id: str,
    stage: str,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
    message: str | None = "任务已提交",
) -> None:
    ts = now_iso()
    await db.execute(
        insert(probe_task).values(
            task_id=task_id,
            datasource_id=datasource_id,
            stage=stage,
            status=TASK_RUNNING,
            progress=0,
            message=message,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=created_by,
            updated_by=created_by,
            started_at=ts,
            created_at=ts,
            updated_at=ts,
            heartbeat_at=time.time(),
            executor_pod=current_pod_name() or None,
        )
    )


async def update_progress(
    db: AsyncSession, task_id: str, progress: int, message: str | None
) -> None:
    await db.execute(
        update(probe_task)
        .where(
            probe_task.c.task_id == task_id,
            probe_task.c.status == TASK_RUNNING,
            probe_task.c.is_deleted == 0,
        )
        .values(progress=progress, message=message, updated_at=now_iso())
    )


async def finish(
    db: AsyncSession, task_id: str, status: str, error_msg: str | None = None
) -> None:
    ts = now_iso()
    await db.execute(
        update(probe_task)
        .where(probe_task.c.task_id == task_id)
        .values(status=status, error_msg=error_msg, finished_at=ts, updated_at=ts)
    )


async def finish_if_running(
    db: AsyncSession,
    task_id: str,
    status: str,
    error_msg: str | None = None,
) -> bool:
    ts = now_iso()
    result = await db.execute(
        update(probe_task)
        .where(
            probe_task.c.task_id == task_id,
            probe_task.c.status == TASK_RUNNING,
            probe_task.c.is_deleted == 0,
        )
        .values(status=status, error_msg=error_msg, finished_at=ts, updated_at=ts)
        .returning(probe_task.c.task_id)
    )
    return result.first() is not None


async def touch_heartbeat(db: AsyncSession, task_id: str) -> int:
    result = await db.execute(
        update(probe_task)
        .where(
            probe_task.c.task_id == task_id,
            probe_task.c.status == TASK_RUNNING,
            probe_task.c.is_deleted == 0,
        )
        .values(heartbeat_at=time.time())
    )
    return result.rowcount


async def has_fresh_running(
    db: AsyncSession,
    *,
    datasource_id: str,
    stage: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(probe_task.c.heartbeat_at).where(
            probe_task.c.datasource_id == datasource_id,
            probe_task.c.stage == stage,
            probe_task.c.tenant_id == tenant_id,
            probe_task.c.workspace_id == workspace_id,
            probe_task.c.status == TASK_RUNNING,
            probe_task.c.is_deleted == 0,
        )
    )
    return any(is_fresh(row[0]) for row in res.all())


async def find_latest(
    db: AsyncSession, datasource_id: str, stage: str, tenant_id: str, workspace_id: str
):
    """该数据源该阶段最近一次任务（按 id DESC）。"""
    res = await db.execute(
        select(probe_task)
        .where(
            probe_task.c.datasource_id == datasource_id,
            probe_task.c.stage == stage,
            probe_task.c.tenant_id == tenant_id,
            probe_task.c.workspace_id == workspace_id,
            probe_task.c.is_deleted == 0,
        )
        .order_by(probe_task.c.id.desc())
        .limit(1)
    )
    return res.mappings().first()

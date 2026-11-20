from __future__ import annotations

import time
from collections.abc import Sequence

from context_manager.schema.relational import recovery_runs
from context_manager.utils.pod import current_pod_name
from sqlalchemy import and_, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import datasource, weave_task


def _response_select():
    return select(
        weave_task.c.id,
        weave_task.c.task_id,
        weave_task.c.task_name,
        weave_task.c.datasource_id,
        datasource.c.datasource_name,
        weave_task.c.weave_mode,
        weave_task.c.status,
        weave_task.c.error_msg,
        weave_task.c.cancel_requested,
        weave_task.c.created_at,
        weave_task.c.heartbeat_at,
        weave_task.c.executor_pod,
        weave_task.c.tenant_id,
        weave_task.c.workspace_id,
        recovery_runs.c.recovery_state.label("_recovery_state"),
        recovery_runs.c.result_status.label("_result_status"),
        recovery_runs.c.message.label("_recovery_message"),
    ).select_from(
        weave_task.outerjoin(
            datasource,
            and_(
                datasource.c.datasource_id == weave_task.c.datasource_id,
                datasource.c.tenant_id == weave_task.c.tenant_id,
                datasource.c.workspace_id == weave_task.c.workspace_id,
                datasource.c.is_deleted == 0,
            ),
        ).outerjoin(
            recovery_runs,
            and_(
                recovery_runs.c.tenant_id == weave_task.c.tenant_id,
                recovery_runs.c.workspace_id == weave_task.c.workspace_id,
                recovery_runs.c.task_type == "weave",
                recovery_runs.c.task_id == weave_task.c.task_id,
                recovery_runs.c.rid == weave_task.c.task_id,
            ),
        )
    )


async def _project_receipt(db, row):
    """Persist a finished graph receipt onto a weave that is still running."""
    if row is None or row["status"] != "RUNNING" or row["_recovery_state"] != "CLOSED":
        return row
    status, message = row["_result_status"], row["_recovery_message"]
    written = await update_status(
        db,
        row["task_id"],
        status,
        message,
        tenant_id=row["tenant_id"],
        workspace_id=row["workspace_id"],
        expect_status=("RUNNING",),
    )
    if not written:
        fresh = await db.execute(
            _response_select().where(
                weave_task.c.is_deleted == 0,
                weave_task.c.task_id == row["task_id"],
                *_scope_conds(row["tenant_id"], row["workspace_id"]),
            )
        )
        current = fresh.mappings().first()
        return current if current is not None else row
    return {**row, "status": status, "error_msg": message}


async def insert(
    db: AsyncSession,
    task_id,
    task_name,
    datasource_id,
    weave_mode,
    status,
    export_payload,
    error_msg,
    *,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
) -> int:
    ts = now_iso()
    res = await db.execute(
        sa_insert(weave_task).values(
            task_id=task_id,
            task_name=task_name,
            datasource_id=datasource_id,
            weave_mode=weave_mode,
            status=status,
            export_payload=export_payload,
            error_msg=error_msg,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=created_by,
            updated_by=created_by,
            created_at=ts,
            updated_at=ts,
            heartbeat_at=time.time(),
            executor_pod=current_pod_name() or None,
            cancel_requested=0,
        )
    )
    return int(res.inserted_primary_key[0])


def _scope_conds(tenant_id: str | None, workspace_id: str | None) -> list:
    """按需拼租户过滤：用户面接口传 tenant/ws；系统回调按全局唯一 task_id 更新，不传。"""
    if tenant_id is None or workspace_id is None:
        return []
    return [
        weave_task.c.tenant_id == tenant_id,
        weave_task.c.workspace_id == workspace_id,
    ]


async def find_by_task_id(
    db: AsyncSession,
    task_id: str,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
):
    res = await db.execute(
        select(weave_task).where(
            weave_task.c.task_id == task_id,
            *_scope_conds(tenant_id, workspace_id),
            weave_task.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_task_id(
    db: AsyncSession,
    task_id: str,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
):
    stmt = _response_select().where(
        weave_task.c.is_deleted == 0,
        weave_task.c.task_id == task_id,
        *_scope_conds(tenant_id, workspace_id),
    )
    res = await db.execute(stmt)
    return await _project_receipt(db, res.mappings().first())


async def update_status(
    db: AsyncSession,
    task_id: str,
    status: str,
    error_msg: str | None,
    *,
    updated_by: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
    export_payload: str | None = None,
    expect_status: Sequence[str] | None = None,
) -> int:
    """更新任务状态，返回命中行数。

    ``expect_status`` 传入时附加 CAS 条件（仅当前状态命中才更新）：后台 worker
    回写终态用它挡住「晚归的 worker 覆盖 KILLED 或覆盖新一轮 run 的终态」——
    rowcount==0 即表示这一行已不该由本次写入接管。用户面回调 / kill 不传，语义不变。
    """
    values = {
        "status": status,
        "error_msg": error_msg,
        "updated_by": func.coalesce(updated_by, weave_task.c.updated_by),
        "updated_at": now_iso(),
    }
    # export_payload 仅在显式传入时覆盖（异步织入完成后补回 payload 审计），
    # 缺省沿用原值——避免回调/kill 等路径把它清空。
    if export_payload is not None:
        values["export_payload"] = export_payload
    cas_conds = [weave_task.c.status.in_(list(expect_status))] if expect_status else []
    res = await db.execute(
        sa_update(weave_task)
        .where(
            weave_task.c.task_id == task_id,
            *_scope_conds(tenant_id, workspace_id),
            *cas_conds,
            weave_task.c.is_deleted == 0,
        )
        .values(**values)
    )
    return res.rowcount


async def request_cancel(
    db: AsyncSession,
    task_id: str,
    *,
    updated_by: str | None,
    tenant_id: str,
    workspace_id: str,
    expect_status: str,
) -> int:
    """Ask the running worker to stop. The row stays RUNNING until it exits."""
    res = await db.execute(
        sa_update(weave_task)
        .where(
            weave_task.c.task_id == task_id,
            *_scope_conds(tenant_id, workspace_id),
            weave_task.c.status == expect_status,
            weave_task.c.is_deleted == 0,
        )
        .values(
            cancel_requested=1,
            updated_by=func.coalesce(updated_by, weave_task.c.updated_by),
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def touch_heartbeat(
    db: AsyncSession,
    task_id: str,
    *,
    tenant_id: str,
    workspace_id: str,
) -> int:
    res = await db.execute(
        sa_update(weave_task)
        .where(
            weave_task.c.task_id == task_id,
            weave_task.c.tenant_id == tenant_id,
            weave_task.c.workspace_id == workspace_id,
            weave_task.c.status == "RUNNING",
            weave_task.c.is_deleted == 0,
        )
        .values(heartbeat_at=time.time())
    )
    return res.rowcount


async def update_export_payload(
    db: AsyncSession,
    task_id: str,
    export_payload: str,
    *,
    updated_by: str | None = None,
    tenant_id: str | None = None,
    workspace_id: str | None = None,
) -> int:
    """只补 payload 审计，不动 status。

    异步织入在组装完成、图导入开始**之前**先把 payload 落库并提交——组装事务不得
    跨越耗时的图导入（否则连接会被 Hologres 的
    ``idle_in_transaction_session_timeout`` 掐断），且终态回写失败时 payload 不跟着丢。
    """
    res = await db.execute(
        sa_update(weave_task)
        .where(
            weave_task.c.task_id == task_id,
            *_scope_conds(tenant_id, workspace_id),
            weave_task.c.is_deleted == 0,
        )
        .values(
            export_payload=export_payload,
            updated_by=func.coalesce(updated_by, weave_task.c.updated_by),
            updated_at=now_iso(),
        )
    )
    return res.rowcount


def _conds(
    datasource_name: str | None,
    task_name: str | None,
    tenant_id: str,
    workspace_id: str,
):
    conds = [
        weave_task.c.is_deleted == 0,
        weave_task.c.tenant_id == tenant_id,
        weave_task.c.workspace_id == workspace_id,
    ]
    if datasource_name:
        conds.append(datasource.c.datasource_name.like(f"%{datasource_name}%"))
    if task_name:
        conds.append(weave_task.c.task_name.like(f"%{task_name}%"))
    return conds


async def count(db, datasource_name, task_name, tenant_id, workspace_id) -> int:
    conds = _conds(datasource_name, task_name, tenant_id, workspace_id)
    stmt = (
        select(func.count())
        .select_from(
            weave_task.outerjoin(
                datasource,
                and_(
                    datasource.c.datasource_id == weave_task.c.datasource_id,
                    datasource.c.tenant_id == weave_task.c.tenant_id,
                    datasource.c.workspace_id == weave_task.c.workspace_id,
                    datasource.c.is_deleted == 0,
                ),
            )
        )
        .where(*conds)
    )
    res = await db.execute(stmt)
    return int(res.scalar_one())


async def find_page(
    db, datasource_name, task_name, limit, off, tenant_id, workspace_id
):
    conds = _conds(datasource_name, task_name, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(weave_task.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return [await _project_receipt(db, row) for row in res.mappings().all()]

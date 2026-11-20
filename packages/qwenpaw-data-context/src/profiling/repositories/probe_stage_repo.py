"""probe_stage 仓储：阶段状态机（每数据源 7 行）的读与懒创建。

懒创建按「系统写」处理（SPEC §13.7）：首次访问某数据源探查状态时补齐缺失的 7 行为
NOT_STARTED，不受 require_admin 约束；tenant_id/workspace_id 从数据源派生，created_by
取当前 principal.user_id。所有查询强制带 tenant_id + workspace_id 行级过滤。
"""

from __future__ import annotations

from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from profiling.models.probe import STAGES, STATUS_NOT_STARTED
from profiling.tables import probe_stage
from semantic_config.db import now_iso
from semantic_config.tables import datasource


async def list_by_datasource(
    db: AsyncSession, datasource_id: str, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(probe_stage).where(
            probe_stage.c.datasource_id == datasource_id,
            probe_stage.c.tenant_id == tenant_id,
            probe_stage.c.workspace_id == workspace_id,
            probe_stage.c.is_deleted == 0,
        )
    )
    return res.mappings().all()


async def list_by_datasources(
    db: AsyncSession,
    datasource_ids: list[str],
    tenant_id: str,
    workspace_id: str,
):
    """批量取多个数据源的阶段行（总览用，避免 N 次查询）。"""
    if not datasource_ids:
        return []
    res = await db.execute(
        select(probe_stage).where(
            probe_stage.c.datasource_id.in_(datasource_ids),
            probe_stage.c.tenant_id == tenant_id,
            probe_stage.c.workspace_id == workspace_id,
            probe_stage.c.is_deleted == 0,
        )
    )
    return res.mappings().all()


async def find_one(
    db: AsyncSession, datasource_id: str, stage: str, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(probe_stage).where(
            probe_stage.c.datasource_id == datasource_id,
            probe_stage.c.stage == stage,
            probe_stage.c.tenant_id == tenant_id,
            probe_stage.c.workspace_id == workspace_id,
            probe_stage.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def status_map(
    db: AsyncSession, datasource_id: str, tenant_id: str, workspace_id: str
) -> dict[str, str]:
    """{stage: status}；缺失阶段不含在内（调用方按 NOT_STARTED 兜底）。"""
    rows = await list_by_datasource(db, datasource_id, tenant_id, workspace_id)
    return {r["stage"]: r["status"] for r in rows}


async def update_status(
    db: AsyncSession,
    datasource_id: str,
    stage: str,
    status: str,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
    current_task_id: str | None = None,
    set_task: bool = False,
    expected_status: str | None = None,
    expected_task_id: str | None = None,
) -> bool:
    """更新阶段状态；set_task=True 时一并写 current_task_id（可为 None 清空）。"""
    values = {"status": status, "updated_by": updated_by, "updated_at": now_iso()}
    if set_task:
        values["current_task_id"] = current_task_id
    conditions = [
        probe_stage.c.datasource_id == datasource_id,
        probe_stage.c.stage == stage,
        probe_stage.c.tenant_id == tenant_id,
        probe_stage.c.workspace_id == workspace_id,
    ]
    if expected_status is not None:
        conditions.append(probe_stage.c.status == expected_status)
    if expected_task_id is not None:
        conditions.append(probe_stage.c.current_task_id == expected_task_id)
    result = await db.execute(update(probe_stage).where(*conditions).values(**values))
    return result.rowcount == 1


async def ensure_stages(
    db: AsyncSession,
    datasource_id: str,
    *,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
) -> None:
    """懒创建：补齐该数据源缺失的阶段行（NOT_STARTED）。系统写，绕过管理员门禁。

    首次初始化锁住已有数据源行，再查缺失阶段，避免不同实例重复创建。
    """
    existing = await list_by_datasource(db, datasource_id, tenant_id, workspace_id)
    present = {row["stage"] for row in existing}
    missing = [s for s in STAGES if s not in present]
    if not missing:
        return
    await db.execute(
        update(datasource)
        .where(
            datasource.c.datasource_id == datasource_id,
            datasource.c.tenant_id == tenant_id,
            datasource.c.workspace_id == workspace_id,
        )
        .values(datasource_id=datasource.c.datasource_id)
    )
    existing = await list_by_datasource(db, datasource_id, tenant_id, workspace_id)
    present = {row["stage"] for row in existing}
    missing = [s for s in STAGES if s not in present]
    if not missing:
        return
    ts = now_iso()
    await db.execute(
        insert(probe_stage),
        [
            {
                "datasource_id": datasource_id,
                "stage": stage,
                "status": STATUS_NOT_STARTED,
                "current_task_id": None,
                "tenant_id": tenant_id,
                "workspace_id": workspace_id,
                "created_by": created_by,
                "updated_by": created_by,
                "created_at": ts,
                "updated_at": ts,
            }
            for stage in missing
        ],
    )

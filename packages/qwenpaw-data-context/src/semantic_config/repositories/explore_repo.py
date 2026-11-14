"""mg-088 自动探查 repo：explore_scope / explore_state 两表的数据访问。

体例对齐 weave_task_repo（SQLAlchemy Core 表达式，SQLite/Hologres 双方言）。
首织完成判定（§2.1）的 weave_task 前驱计数也落在本模块——探查域专用查询，
不扩 weave_task_repo 的公共面。
"""

from __future__ import annotations

import time

from sqlalchemy import func, or_, select, text
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from context_manager.heartbeat import stale_cutoff
from context_manager.utils.pod import current_pod_name
from semantic_config.db import now_iso
from semantic_config.services.autoexplore.scope_filter import (
    DEFAULT_EXCLUDE_PATTERNS,
    join_patterns,
)
from semantic_config.tables import explore_scope, explore_state, weave_task

#: 「首次编织完成」判定计入的状态集（§2.1，裁决 Q7：DEGRADED 算完成）。
FIRST_WEAVE_STATUS = ("SUCCESS", "DEGRADED")


# ---------------------------------------------------------------------- #
# explore_scope（一行一 ds）
# ---------------------------------------------------------------------- #
def _scope_conds(tenant_id: str, workspace_id: str, datasource_id: str):
    return [
        explore_scope.c.tenant_id == tenant_id,
        explore_scope.c.workspace_id == workspace_id,
        explore_scope.c.datasource_id == datasource_id,
        explore_scope.c.is_deleted == 0,
    ]


async def get_scope(
    db: AsyncSession, tenant_id: str, workspace_id: str, datasource_id: str
):
    res = await db.execute(
        select(explore_scope).where(
            *_scope_conds(tenant_id, workspace_id, datasource_id)
        )
    )
    return res.mappings().first()


async def ensure_scope(
    db: AsyncSession, tenant_id: str, workspace_id: str, datasource_id: str
):
    """取 scope 行；缺失则自动生成默认行（origin='auto'，§1.2）并返回。

    已存在行（无论 origin）不覆盖——「已初始化」语义（§2.2）。
    """
    row = await get_scope(db, tenant_id, workspace_id, datasource_id)
    if row is not None:
        return row
    ts = now_iso()
    await db.execute(
        sa_insert(explore_scope).values(
            datasource_id=datasource_id,
            schemas="",
            include_patterns="",
            exclude_patterns=join_patterns(DEFAULT_EXCLUDE_PATTERNS),
            sample_mode="auto",
            sample_limit=10,
            table_limit=300,
            enabled=1,
            origin="auto",
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by="system",
            updated_by="system",
            created_at=ts,
            updated_at=ts,
        )
    )
    return await get_scope(db, tenant_id, workspace_id, datasource_id)


async def update_scope(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    *,
    schemas: str | None = None,
    include_patterns: str | None = None,
    exclude_patterns: str | None = None,
    sample_mode: str | None = None,
    sample_limit: int | None = None,
    table_limit: int | None = None,
    enabled: int | None = None,
    updated_by: str | None = None,
):
    """用户改 scope（§1.2：origin→manual；修改不自动重探，重探走手动端点）。"""
    values: dict = {"origin": "manual", "updated_at": now_iso()}
    if schemas is not None:
        values["schemas"] = schemas
    if include_patterns is not None:
        values["include_patterns"] = include_patterns
    if exclude_patterns is not None:
        values["exclude_patterns"] = exclude_patterns
    if sample_mode is not None:
        values["sample_mode"] = sample_mode
    if sample_limit is not None:
        values["sample_limit"] = sample_limit
    if table_limit is not None:
        values["table_limit"] = table_limit
    if enabled is not None:
        values["enabled"] = enabled
    if updated_by is not None:
        values["updated_by"] = updated_by
    await db.execute(
        sa_update(explore_scope)
        .where(*_scope_conds(tenant_id, workspace_id, datasource_id))
        .values(**values)
    )
    return await get_scope(db, tenant_id, workspace_id, datasource_id)


async def claim_run(
    db: AsyncSession, tenant_id: str, workspace_id: str, datasource_id: str
) -> int | None:
    """Take the datasource round unless a fresh running heartbeat still holds it."""
    await ensure_scope(db, tenant_id, workspace_id, datasource_id)
    now = time.time()
    cutoff = stale_cutoff(now)
    result = await db.execute(
        sa_update(explore_scope)
        .where(
            *_scope_conds(tenant_id, workspace_id, datasource_id),
            or_(
                explore_scope.c.last_status.is_(None),
                explore_scope.c.last_status != "running",
                explore_scope.c.heartbeat_at.is_(None),
                explore_scope.c.heartbeat_at < cutoff,
            ),
        )
        .values(
            last_status="running",
            last_run_at=now_iso(),
            heartbeat_at=now,
            executor_pod=current_pod_name() or None,
            run_epoch=explore_scope.c.run_epoch + 1,
            updated_at=now_iso(),
        )
        .returning(explore_scope.c.run_epoch)
    )
    return result.scalar_one_or_none()


async def touch_run(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    epoch: int,
) -> int:
    result = await db.execute(
        sa_update(explore_scope)
        .where(
            *_scope_conds(tenant_id, workspace_id, datasource_id),
            explore_scope.c.run_epoch == epoch,
            explore_scope.c.last_status == "running",
        )
        .values(heartbeat_at=time.time())
    )
    return result.rowcount


async def mark_running(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    epoch: int,
) -> int:
    """Refresh this round only while it is still the running one."""
    result = await db.execute(
        sa_update(explore_scope)
        .where(
            *_scope_conds(tenant_id, workspace_id, datasource_id),
            explore_scope.c.run_epoch == epoch,
            explore_scope.c.last_status == "running",
        )
        .values(last_status="running", last_run_at=now_iso(), updated_at=now_iso())
    )
    return int(result.rowcount or 0)


async def finish_run(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    *,
    status: str,
    counts_json: str | None = None,
    error: str | None = None,
    epoch: int | None = None,
):
    conditions = list(_scope_conds(tenant_id, workspace_id, datasource_id))
    if epoch is not None:
        conditions.append(explore_scope.c.run_epoch == epoch)
        conditions.append(explore_scope.c.last_status == "running")
    result = await db.execute(
        sa_update(explore_scope)
        .where(*conditions)
        .values(
            last_status=status,
            last_counts=counts_json,
            last_error=error,
            heartbeat_at=None,
            updated_at=now_iso(),
        )
    )
    return int(result.rowcount or 0)


# ---------------------------------------------------------------------- #
# explore_state（一行一表；指纹增量 §2.3）
# ---------------------------------------------------------------------- #
async def get_state_map(
    db: AsyncSession, tenant_id: str, workspace_id: str, datasource_id: str
) -> dict[str, str]:
    """{两段全名表: fingerprint}（软删行不计）。"""
    res = await db.execute(
        select(explore_state.c.table_name, explore_state.c.fingerprint).where(
            explore_state.c.tenant_id == tenant_id,
            explore_state.c.workspace_id == workspace_id,
            explore_state.c.datasource_id == datasource_id,
            explore_state.c.is_deleted == 0,
            explore_state.c.status == "ok",
        )
    )
    return {r["table_name"]: (r["fingerprint"] or "") for r in res.mappings().all()}


async def upsert_state(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    table_name: str,
    *,
    fingerprint: str,
    run_id: str,
    status: str,
    column_count: int,
    sampled: int,
    updated_by: str,
):
    """One statement per business key. The partial unique index is the conflict target."""
    ts = now_iso()
    values = dict(
        datasource_id=datasource_id,
        table_name=table_name,
        fingerprint=fingerprint,
        run_id=run_id,
        status=status,
        column_count=column_count,
        sampled=sampled,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        created_by=updated_by,
        updated_by=updated_by,
        created_at=ts,
        updated_at=ts,
        is_deleted=0,
    )
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    await db.execute(
        insert(explore_state)
        .values(**values)
        .on_conflict_do_update(
            index_elements=[
                "tenant_id",
                "workspace_id",
                "datasource_id",
                "table_name",
            ],
            index_where=text("is_deleted = 0"),
            set_={
                "fingerprint": fingerprint,
                "run_id": run_id,
                "status": status,
                "column_count": column_count,
                "sampled": sampled,
                "updated_by": updated_by,
                "updated_at": ts,
                "is_deleted": 0,
            },
        )
    )


# ---------------------------------------------------------------------- #
# ds 删除级联软删（§4.3 适配点）
# ---------------------------------------------------------------------- #
async def soft_delete_for_datasource(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    *,
    updated_by: str,
) -> int:
    """datasource 删除钩子：explore_scope/explore_state 随行软删。返回受影响行数。"""
    ts = now_iso()
    n_scope = (
        await db.execute(
            sa_update(explore_scope)
            .where(*_scope_conds(tenant_id, workspace_id, datasource_id))
            .values(is_deleted=1, updated_by=updated_by, updated_at=ts)
        )
    ).rowcount
    n_state = (
        await db.execute(
            sa_update(explore_state)
            .where(
                explore_state.c.tenant_id == tenant_id,
                explore_state.c.workspace_id == workspace_id,
                explore_state.c.datasource_id == datasource_id,
                explore_state.c.is_deleted == 0,
            )
            .values(is_deleted=1, updated_by=updated_by, updated_at=ts)
        )
    ).rowcount
    return int(n_scope) + int(n_state)


# ---------------------------------------------------------------------- #
# 首织判定（§2.1）：同 ds 历史中状态 ∈ {SUCCESS, DEGRADED} 且非本次的任务数
# ---------------------------------------------------------------------- #
async def count_first_weave_predecessors(
    db: AsyncSession,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
    *,
    exclude_task_id: str,
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(weave_task)
        .where(
            weave_task.c.tenant_id == tenant_id,
            weave_task.c.workspace_id == workspace_id,
            weave_task.c.datasource_id == datasource_id,
            weave_task.c.is_deleted == 0,
            weave_task.c.status.in_(FIRST_WEAVE_STATUS),
            weave_task.c.task_id != exclude_task_id,
        )
    )
    return int(res.scalar_one())

"""通用 staging 仓储：按 table 名驱动 6 张 staging 表的增改查 / 批量 / rerun 清理。

所有操作强制带 datasource_id + tenant_id + workspace_id + is_deleted=0 行级过滤（SPEC §13.4）。
table 名来自内部 STAGE_DEFS（非用户输入），经 PROFILING_TABLES 注册表映射为 Core Table 对象，
由 SQLAlchemy 按方言生成 SQL（不再手拼字符串），SQLite / Hologres 通吃。
"""

from __future__ import annotations

import json

from sqlalchemy import case, func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from profiling.models.probe import ROW_CONFIRMED, ROW_PENDING, SOURCE_ALGO
from profiling.tables import PROFILING_TABLES
from semantic_config.db import now_iso

# 不外泄给前端的内部隔离/审计列
_INTERNAL_COLS = frozenset(
    {
        "tenant_id",
        "workspace_id",
        "created_by",
        "updated_by",
        "is_deleted",
        "created_at",
        "updated_at",
    }
)


# probe_status 排序优先级：PENDING → SKIPPED → CONFIRMED → 其它
def _status_order(table):
    return case(
        (table.c.probe_status == "PENDING", 0),
        (table.c.probe_status == "SKIPPED", 1),
        (table.c.probe_status == "CONFIRMED", 2),
        else_=3,
    )


def _t(table: str):
    return PROFILING_TABLES[table]


def to_public_dict(row) -> dict:
    """staging 行 → 前端响应 dict：剔除隔离/审计列；member_tables JSON 反序列化为数组。"""
    d = {k: row[k] for k in row.keys() if k not in _INTERNAL_COLS}
    if "member_tables" in d:
        raw = d["member_tables"]
        if isinstance(raw, str) and raw:
            try:
                d["member_tables"] = json.loads(raw)
            except ValueError:
                d["member_tables"] = None
    return d


async def count(
    db: AsyncSession,
    table: str,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    *,
    filter_col: str | None = None,
    filter_val: str | None = None,
) -> int:
    t = _t(table)
    conds = [
        t.c.datasource_id == datasource_id,
        t.c.tenant_id == tenant_id,
        t.c.workspace_id == workspace_id,
        t.c.is_deleted == 0,
    ]
    if filter_col and filter_val:
        conds.append(t.c[filter_col].like(f"%{filter_val}%"))
    res = await db.execute(select(func.count()).select_from(t).where(*conds))
    return int(res.scalar_one())


async def find_page(
    db: AsyncSession,
    table: str,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    limit: int,
    off: int,
    *,
    filter_col: str | None = None,
    filter_val: str | None = None,
):
    t = _t(table)
    conds = [
        t.c.datasource_id == datasource_id,
        t.c.tenant_id == tenant_id,
        t.c.workspace_id == workspace_id,
        t.c.is_deleted == 0,
    ]
    if filter_col and filter_val:
        conds.append(t.c[filter_col].like(f"%{filter_val}%"))
    stmt = (
        select(t)
        .where(*conds)
        .order_by(_status_order(t), t.c.id.asc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def find_by_id(
    db: AsyncSession,
    table: str,
    row_id: int,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
):
    t = _t(table)
    res = await db.execute(
        select(t).where(
            t.c.id == row_id,
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def list_all(
    db: AsyncSession,
    table: str,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    *,
    statuses: tuple[str, ...] | None = None,
):
    """取该数据源该表全部有效行；statuses 给定时按 probe_status 过滤（promote/校验用）。"""
    t = _t(table)
    conds = [
        t.c.datasource_id == datasource_id,
        t.c.tenant_id == tenant_id,
        t.c.workspace_id == workspace_id,
        t.c.is_deleted == 0,
    ]
    if statuses:
        conds.append(t.c.probe_status.in_(statuses))
    res = await db.execute(select(t).where(*conds).order_by(t.c.id.asc()))
    return res.mappings().all()


async def insert_row(
    db: AsyncSession,
    table: str,
    values: dict,
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
    task_id: str | None,
    source_type: str = SOURCE_ALGO,
    probe_status: str = ROW_PENDING,
) -> int:
    t = _t(table)
    ts = now_iso()
    cols = dict(values)
    cols.update(
        datasource_id=datasource_id,
        task_id=task_id,
        source_type=source_type,
        probe_status=probe_status,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        created_by=created_by,
        updated_by=created_by,
        created_at=ts,
        updated_at=ts,
    )
    res = await db.execute(insert(t).values(**cols))
    return int(res.inserted_primary_key[0])


async def update_row(
    db: AsyncSession,
    table: str,
    row_id: int,
    values: dict,
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    if not values:
        return 0
    t = _t(table)
    res = await db.execute(
        update(t)
        .where(
            t.c.id == row_id,
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
        )
        .values(**values, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def soft_delete(
    db: AsyncSession,
    table: str,
    row_id: int,
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    t = _t(table)
    res = await db.execute(
        update(t)
        .where(
            t.c.id == row_id,
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def batch_set_status(
    db: AsyncSession,
    table: str,
    row_ids: list[int],
    status: str,
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    if not row_ids:
        return 0
    t = _t(table)
    res = await db.execute(
        update(t)
        .where(
            t.c.id.in_(row_ids),
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
        )
        .values(probe_status=status, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def batch_soft_delete(
    db: AsyncSession,
    table: str,
    row_ids: list[int],
    *,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    if not row_ids:
        return 0
    t = _t(table)
    res = await db.execute(
        update(t)
        .where(
            t.c.id.in_(row_ids),
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def soft_delete_all(
    db: AsyncSession,
    table: str,
    datasource_id: str,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    """清空该数据源该表（rerun 整体重来 / upload 覆盖）。"""
    t = _t(table)
    res = await db.execute(
        update(t)
        .where(
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def list_unlocked_ids(
    db: AsyncSession,
    table: str,
    datasource_id: str,
    tenant_id: str,
    workspace_id: str,
) -> list[int]:
    """未锁定行 = 非 CONFIRMED（含 PENDING/SKIPPED 的 ALGO 与 HUMAN）。

    业务域阶段的替换式写回：探查前记下这批 id，新行落库成功后再软删它们 ——
    仅 CONFIRMED 原地保留；未确认的人工新增同样可被 refine/create 替换。
    """
    t = _t(table)
    res = await db.execute(
        select(t.c.id).where(
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
            t.c.probe_status != ROW_CONFIRMED,
        )
    )
    return [int(r[0]) for r in res.all()]


async def soft_delete_non_confirmed(
    db: AsyncSession,
    table: str,
    datasource_id: str,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    """rerun 口径/管理阶段：保留 CONFIRMED 行，删除 PENDING/SKIPPED（SPEC §6.3）。"""
    t = _t(table)
    res = await db.execute(
        update(t)
        .where(
            t.c.datasource_id == datasource_id,
            t.c.tenant_id == tenant_id,
            t.c.workspace_id == workspace_id,
            t.c.is_deleted == 0,
            t.c.probe_status != ROW_CONFIRMED,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount

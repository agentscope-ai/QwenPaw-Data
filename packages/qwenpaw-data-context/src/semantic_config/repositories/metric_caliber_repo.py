from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import (
    dataset,
    datasource,
    domain,
    metric_caliber,
    metric_dict,
)

# 指标口径：表结构不变；冗余 datasource_id/domain_id 改为派生自父 dataset（SPEC 00 §5.4），
# 指标引用改指 metric_dict.id。唯一键 (tenant,ws,metric_id,dataset_id,time_window)。


def _response_select():
    return select(
        metric_caliber.c.id,
        metric_caliber.c.datasource_id,
        metric_caliber.c.domain_id,
        metric_caliber.c.metric_id,
        metric_caliber.c.dataset_id,
        datasource.c.datasource_name,
        domain.c.domain_name,
        metric_dict.c.metric_name,
        dataset.c.dataset_name,
        metric_caliber.c.expr,
        metric_caliber.c.time_window,
        metric_caliber.c.additivity,
        metric_caliber.c.is_default,
    ).select_from(
        metric_caliber.outerjoin(
            metric_dict, metric_dict.c.id == metric_caliber.c.metric_id
        )
        .outerjoin(dataset, dataset.c.id == metric_caliber.c.dataset_id)
        .outerjoin(
            datasource,
            (datasource.c.datasource_id == metric_caliber.c.datasource_id)
            & (datasource.c.tenant_id == metric_caliber.c.tenant_id)
            & (datasource.c.workspace_id == metric_caliber.c.workspace_id)
            & (datasource.c.is_deleted == 0),
        )
        .outerjoin(domain, domain.c.id == metric_caliber.c.domain_id)
    )


async def insert(
    db: AsyncSession,
    m,
    *,
    domain_id: int | None,
    datasource_id: str | None = None,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
) -> int:
    ts = now_iso()
    values = dict(
        metric_id=m.metric_id,
        dataset_id=m.dataset_id,
        domain_id=domain_id,
        datasource_id=datasource_id or getattr(m, "datasource_id", None),
        expr=m.expr,
        time_window=m.time_window,
        additivity=m.additivity,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        created_by=created_by,
        updated_by=created_by,
        created_at=ts,
        updated_at=ts,
    )
    if m.is_default is not None:  # None 时省略，保持 NULL=未标注（⑦a）
        values["is_default"] = m.is_default
    res = await db.execute(sa_insert(metric_caliber).values(**values))
    return int(res.inserted_primary_key[0])


async def update(
    db: AsyncSession, cid: int, m, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    values = dict(
        expr=m.expr,
        time_window=m.time_window,
        additivity=m.additivity,
        updated_by=updated_by,
        updated_at=now_iso(),
    )
    if m.is_default is not None:  # None 时省略，避免覆盖成 NULL
        values["is_default"] = m.is_default
    res = await db.execute(
        sa_update(metric_caliber)
        .where(
            metric_caliber.c.id == cid,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
        .values(**values)
    )
    return res.rowcount


async def find_by_id(db: AsyncSession, cid: int, tenant_id: str, workspace_id: str):
    res = await db.execute(
        select(metric_caliber).where(
            metric_caliber.c.id == cid,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, cid: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        metric_caliber.c.is_deleted == 0,
        metric_caliber.c.id == cid,
        metric_caliber.c.tenant_id == tenant_id,
        metric_caliber.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_key(
    db: AsyncSession,
    metric_id: int,
    dataset_id: int | None,
    time_window: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(metric_caliber.c.id).where(
            metric_caliber.c.metric_id == metric_id,
            metric_caliber.c.dataset_id.is_not_distinct_from(dataset_id),
            metric_caliber.c.time_window == time_window,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_key_excluding(
    db: AsyncSession,
    metric_id: int,
    dataset_id: int | None,
    time_window: str,
    cid: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(metric_caliber.c.id).where(
            metric_caliber.c.metric_id == metric_id,
            metric_caliber.c.dataset_id.is_not_distinct_from(dataset_id),
            metric_caliber.c.time_window == time_window,
            metric_caliber.c.id != cid,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _conds(metric_id, dataset_id, tenant_id, workspace_id):
    conds = [
        metric_caliber.c.is_deleted == 0,
        metric_caliber.c.tenant_id == tenant_id,
        metric_caliber.c.workspace_id == workspace_id,
    ]
    if metric_id is not None:
        conds.append(metric_caliber.c.metric_id == metric_id)
    if dataset_id is not None:
        conds.append(metric_caliber.c.dataset_id == dataset_id)
    return conds


async def count(db, metric_id, dataset_id, tenant_id, workspace_id) -> int:
    conds = _conds(metric_id, dataset_id, tenant_id, workspace_id)
    res = await db.execute(
        select(func.count()).select_from(metric_caliber).where(*conds)
    )
    return int(res.scalar_one())


async def find_page(db, metric_id, dataset_id, limit, off, tenant_id, workspace_id):
    conds = _conds(metric_id, dataset_id, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(metric_caliber.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def find_all_by_metric(
    db: AsyncSession, metric_id: int, tenant_id: str, workspace_id: str
):
    stmt = (
        _response_select()
        .where(
            metric_caliber.c.is_deleted == 0,
            metric_caliber.c.metric_id == metric_id,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
        )
        .order_by(metric_caliber.c.id)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession, cid: int, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    res = await db.execute(
        sa_update(metric_caliber)
        .where(
            metric_caliber.c.id == cid,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def soft_delete_by_metric(
    db: AsyncSession,
    metric_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(metric_caliber)
        .where(
            metric_caliber.c.metric_id == metric_id,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


exists_key = exists_key
exists_key_excluding = exists_key_excluding
find_response_by_id = find_response_by_id

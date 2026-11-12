"""数据集仓储（表 ``dataset``，SPEC 00 §5.2）。

12 实体重构两处关键变化：

- 数据源列 ``source_datasource`` 改名 ``datasource_id``，全模型统一命名；
- 唯一性由"同业务域内唯一"改为**同数据源内唯一** ``(tenant, ws, datasource_id, dataset_name)``。

数据集是"业务域 → 数据源"关系的唯一来源，写操作后须由调用方触发
:func:`semantic_config.services.domain_scope.recompute_domain_scope`。
"""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    dataset_field,
    datasource,
    domain,
    metric_caliber,
)


def _response_select():
    return select(
        dataset.c.id.label("dataset_id"),
        dataset.c.datasource_id,
        dataset.c.domain_id,
        datasource.c.datasource_name,
        domain.c.domain_name,
        dataset.c.dataset_name,
        dataset.c.description,
        dataset.c.source,
    ).select_from(
        dataset.outerjoin(
            datasource,
            and_(
                datasource.c.datasource_id == dataset.c.datasource_id,
                datasource.c.tenant_id == dataset.c.tenant_id,
                datasource.c.workspace_id == dataset.c.workspace_id,
                datasource.c.is_deleted == 0,
            ),
        ).outerjoin(
            domain,
            and_(
                domain.c.id == dataset.c.domain_id,
                domain.c.tenant_id == dataset.c.tenant_id,
                domain.c.workspace_id == dataset.c.workspace_id,
                domain.c.is_deleted == 0,
            ),
        )
    )


async def insert(
    db: AsyncSession, m, *, tenant_id: str, workspace_id: str, created_by: str
) -> int:
    ts = now_iso()
    res = await db.execute(
        sa_insert(dataset).values(
            datasource_id=m.datasource_id,
            domain_id=m.domain_id,
            dataset_name=m.dataset_name,
            description=m.description,
            source=m.source,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            created_by=created_by,
            updated_by=created_by,
            created_at=ts,
            updated_at=ts,
        )
    )
    return int(res.inserted_primary_key[0])


async def update(
    db: AsyncSession,
    dataset_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    """只改 name/description/source；datasource_id、domain_id 绑定不可改（SPEC 00 §11.3）。"""
    res = await db.execute(
        sa_update(dataset)
        .where(
            dataset.c.id == dataset_id,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
        .values(
            dataset_name=m.dataset_name,
            description=m.description,
            source=m.source,
            updated_by=updated_by,
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def find_by_id(
    db: AsyncSession, dataset_id: int, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(dataset).where(
            dataset.c.id == dataset_id,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, dataset_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        dataset.c.is_deleted == 0,
        dataset.c.id == dataset_id,
        dataset.c.tenant_id == tenant_id,
        dataset.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_name_in_datasource(
    db: AsyncSession,
    datasource_id: str | None,
    name: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    """唯一性：同一数据源下同名数据集仅一个（取代原"同业务域内唯一"）。"""
    res = await db.execute(
        select(func.count())
        .select_from(dataset)
        .where(
            dataset.c.datasource_id.is_not_distinct_from(datasource_id),
            dataset.c.dataset_name == name,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one()) > 0


async def exists_name_in_datasource_excluding(
    db: AsyncSession,
    datasource_id: str | None,
    name: str,
    dataset_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(func.count())
        .select_from(dataset)
        .where(
            dataset.c.datasource_id.is_not_distinct_from(datasource_id),
            dataset.c.dataset_name == name,
            dataset.c.id != dataset_id,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one()) > 0


def _conds(datasource_id, domain_id, dataset_name, tenant_id, workspace_id):
    conds = [
        dataset.c.is_deleted == 0,
        dataset.c.tenant_id == tenant_id,
        dataset.c.workspace_id == workspace_id,
    ]
    if datasource_id is not None:
        conds.append(dataset.c.datasource_id == datasource_id)
    if domain_id is not None:
        conds.append(dataset.c.domain_id == domain_id)
    if dataset_name:
        conds.append(dataset.c.dataset_name.like(f"%{dataset_name}%"))
    return conds


async def count(
    db, datasource_id, domain_id, dataset_name, tenant_id, workspace_id
) -> int:
    conds = _conds(datasource_id, domain_id, dataset_name, tenant_id, workspace_id)
    res = await db.execute(select(func.count()).select_from(dataset).where(*conds))
    return int(res.scalar_one())


async def find_page(
    db, datasource_id, domain_id, dataset_name, limit, off, tenant_id, workspace_id
):
    conds = _conds(datasource_id, domain_id, dataset_name, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(dataset.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession,
    dataset_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(dataset)
        .where(
            dataset.c.id == dataset_id,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


# ---- 引用计数（删除校验用；v2.1：字段/维度口径/指标口径三类子表）----
async def _count_child(
    db: AsyncSession, table, dataset_id: int, tenant_id: str, workspace_id: str
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(table)
        .where(
            table.c.dataset_id == dataset_id,
            table.c.tenant_id == tenant_id,
            table.c.workspace_id == workspace_id,
            table.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one())


async def count_fields(db, dataset_id, tenant_id: str, workspace_id: str):
    return await _count_child(db, dataset_field, dataset_id, tenant_id, workspace_id)


async def count_dimensions(db, dataset_id, tenant_id: str, workspace_id: str):
    return await _count_child(
        db, dataset_dimension, dataset_id, tenant_id, workspace_id
    )


async def count_calibers(db, dataset_id, tenant_id: str, workspace_id: str):
    return await _count_child(db, metric_caliber, dataset_id, tenant_id, workspace_id)

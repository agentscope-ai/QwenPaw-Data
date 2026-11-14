from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import (
    dataset,
    dataset_dimension,
    datasource,
    dimension_dict,
    domain,
)

# 维度口径：表结构不变；冗余 datasource_id/domain_id 改为派生自父 dataset（SPEC 00 §5.3），
# 维度引用改指 dimension_dict.id。


def _response_select():
    return select(
        dataset_dimension.c.id,
        dataset_dimension.c.datasource_id,
        dataset_dimension.c.domain_id,
        dataset_dimension.c.dataset_id,
        dataset_dimension.c.dimension_id,
        datasource.c.datasource_name,
        domain.c.domain_name,
        dataset.c.dataset_name,
        dimension_dict.c.dimension_name,
        dataset_dimension.c.expr,
        dataset_dimension.c.value_example,
    ).select_from(
        dataset_dimension.outerjoin(
            dataset, dataset.c.id == dataset_dimension.c.dataset_id
        )
        .outerjoin(
            dimension_dict, dimension_dict.c.id == dataset_dimension.c.dimension_id
        )
        .outerjoin(
            datasource,
            (datasource.c.datasource_id == dataset_dimension.c.datasource_id)
            & (datasource.c.tenant_id == dataset_dimension.c.tenant_id)
            & (datasource.c.workspace_id == dataset_dimension.c.workspace_id)
            & (datasource.c.is_deleted == 0),
        )
        .outerjoin(domain, domain.c.id == dataset_dimension.c.domain_id)
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
    res = await db.execute(
        sa_insert(dataset_dimension).values(
            dataset_id=m.dataset_id,
            dimension_id=m.dimension_id,
            domain_id=domain_id,
            datasource_id=datasource_id or getattr(m, "datasource_id", None),
            expr=m.expr,
            value_example=m.value_example,
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
    dd_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(dataset_dimension)
        .where(
            dataset_dimension.c.id == dd_id,
            dataset_dimension.c.tenant_id == tenant_id,
            dataset_dimension.c.workspace_id == workspace_id,
            dataset_dimension.c.is_deleted == 0,
        )
        .values(
            expr=m.expr,
            value_example=m.value_example,
            updated_by=updated_by,
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def find_by_id(db: AsyncSession, dd_id: int, tenant_id: str, workspace_id: str):
    res = await db.execute(
        select(dataset_dimension).where(
            dataset_dimension.c.id == dd_id,
            dataset_dimension.c.tenant_id == tenant_id,
            dataset_dimension.c.workspace_id == workspace_id,
            dataset_dimension.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, dd_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        dataset_dimension.c.is_deleted == 0,
        dataset_dimension.c.id == dd_id,
        dataset_dimension.c.tenant_id == tenant_id,
        dataset_dimension.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


def _conds(dataset_id, dimension_name, tenant_id, workspace_id):
    conds = [
        dataset_dimension.c.is_deleted == 0,
        dataset_dimension.c.tenant_id == tenant_id,
        dataset_dimension.c.workspace_id == workspace_id,
    ]
    if dataset_id is not None:
        conds.append(dataset_dimension.c.dataset_id == dataset_id)
    if dimension_name:
        conds.append(dimension_dict.c.dimension_name.like(f"%{dimension_name}%"))
    return conds


async def count(db, dataset_id, dimension_name, tenant_id, workspace_id) -> int:
    conds = _conds(dataset_id, dimension_name, tenant_id, workspace_id)
    res = await db.execute(
        select(func.count())
        .select_from(
            dataset_dimension.outerjoin(
                dimension_dict, dimension_dict.c.id == dataset_dimension.c.dimension_id
            )
        )
        .where(*conds)
    )
    return int(res.scalar_one())


async def find_page(
    db, dataset_id, dimension_name, limit, off, tenant_id, workspace_id
):
    conds = _conds(dataset_id, dimension_name, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(dataset_dimension.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def find_all_by_dataset(
    db: AsyncSession, dataset_id: int, tenant_id: str, workspace_id: str
):
    stmt = (
        _response_select()
        .where(
            dataset_dimension.c.is_deleted == 0,
            dataset_dimension.c.dataset_id == dataset_id,
            dataset_dimension.c.tenant_id == tenant_id,
            dataset_dimension.c.workspace_id == workspace_id,
        )
        .order_by(dataset_dimension.c.id)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession, dd_id: int, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    res = await db.execute(
        sa_update(dataset_dimension)
        .where(
            dataset_dimension.c.id == dd_id,
            dataset_dimension.c.tenant_id == tenant_id,
            dataset_dimension.c.workspace_id == workspace_id,
            dataset_dimension.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def soft_delete_by_dataset(
    db: AsyncSession,
    dataset_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(dataset_dimension)
        .where(
            dataset_dimension.c.dataset_id == dataset_id,
            dataset_dimension.c.tenant_id == tenant_id,
            dataset_dimension.c.workspace_id == workspace_id,
            dataset_dimension.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount

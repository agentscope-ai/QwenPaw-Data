from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import dataset, dataset_field, datasource, domain

# v2.1 新建（L3）：业务语义字段层。表式来源字段同步自 P3；SQL 式来源字段 = SQL 输出列
# （W2 导入链 dry-run 落）。


def _response_select():
    return select(
        dataset_field.c.id,
        dataset_field.c.datasource_id,
        dataset_field.c.domain_id,
        dataset_field.c.dataset_id,
        datasource.c.datasource_name,
        domain.c.domain_name,
        dataset.c.dataset_name,
        dataset_field.c.field_name,
        dataset_field.c.description,
        dataset_field.c.data_type,
        dataset_field.c.is_primary,
        dataset_field.c.is_nullable,
    ).select_from(
        dataset_field.outerjoin(dataset, dataset.c.id == dataset_field.c.dataset_id)
        .outerjoin(
            datasource,
            (datasource.c.datasource_id == dataset_field.c.datasource_id)
            & (datasource.c.tenant_id == dataset_field.c.tenant_id)
            & (datasource.c.workspace_id == dataset_field.c.workspace_id)
            & (datasource.c.is_deleted == 0),
        )
        .outerjoin(domain, domain.c.id == dataset_field.c.domain_id)
    )


async def insert(
    db: AsyncSession, m, *, tenant_id: str, workspace_id: str, created_by: str
) -> int:
    ts = now_iso()
    res = await db.execute(
        sa_insert(dataset_field).values(
            dataset_id=m.dataset_id,
            datasource_id=getattr(m, "datasource_id", None),
            domain_id=m.domain_id,
            field_name=m.field_name,
            description=m.description,
            data_type=m.data_type,
            is_primary=m.is_primary or "N",
            is_nullable=(m.is_nullable if m.is_nullable is not None else "Y"),
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
    field_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(dataset_field)
        .where(
            dataset_field.c.id == field_id,
            dataset_field.c.tenant_id == tenant_id,
            dataset_field.c.workspace_id == workspace_id,
            dataset_field.c.is_deleted == 0,
        )
        .values(
            field_name=m.field_name,
            description=m.description,
            data_type=m.data_type,
            is_primary=m.is_primary,
            is_nullable=m.is_nullable,
            updated_by=updated_by,
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def find_by_id(
    db: AsyncSession, field_id: int, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(dataset_field).where(
            dataset_field.c.id == field_id,
            dataset_field.c.tenant_id == tenant_id,
            dataset_field.c.workspace_id == workspace_id,
            dataset_field.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, field_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        dataset_field.c.is_deleted == 0,
        dataset_field.c.id == field_id,
        dataset_field.c.tenant_id == tenant_id,
        dataset_field.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_name_in_dataset(
    db: AsyncSession,
    dataset_id: int,
    field_name: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(dataset_field.c.id).where(
            dataset_field.c.dataset_id == dataset_id,
            dataset_field.c.field_name == field_name,
            dataset_field.c.tenant_id == tenant_id,
            dataset_field.c.workspace_id == workspace_id,
            dataset_field.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_name_in_dataset_excluding(
    db: AsyncSession,
    dataset_id: int,
    field_name: str,
    field_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(dataset_field.c.id).where(
            dataset_field.c.dataset_id == dataset_id,
            dataset_field.c.field_name == field_name,
            dataset_field.c.id != field_id,
            dataset_field.c.tenant_id == tenant_id,
            dataset_field.c.workspace_id == workspace_id,
            dataset_field.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _list_conds(datasource_id, domain_id, dataset_id, tenant_id, workspace_id):
    conds = [
        dataset_field.c.is_deleted == 0,
        dataset_field.c.tenant_id == tenant_id,
        dataset_field.c.workspace_id == workspace_id,
    ]
    if datasource_id is not None:
        conds.append(dataset_field.c.datasource_id == datasource_id)
    if domain_id is not None:
        conds.append(dataset_field.c.domain_id == domain_id)
    if dataset_id is not None:
        conds.append(dataset_field.c.dataset_id == dataset_id)
    return conds


async def count(
    db, dataset_id, tenant_id, workspace_id, datasource_id=None, domain_id=None
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(dataset_field)
        .where(
            *_list_conds(datasource_id, domain_id, dataset_id, tenant_id, workspace_id)
        )
    )
    return int(res.scalar_one())


async def find_page(
    db,
    dataset_id,
    limit,
    off,
    tenant_id,
    workspace_id,
    datasource_id=None,
    domain_id=None,
):
    stmt = (
        _response_select()
        .where(
            *_list_conds(datasource_id, domain_id, dataset_id, tenant_id, workspace_id)
        )
        .order_by(dataset_field.c.id.desc())
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
            dataset_field.c.is_deleted == 0,
            dataset_field.c.dataset_id == dataset_id,
            dataset_field.c.tenant_id == tenant_id,
            dataset_field.c.workspace_id == workspace_id,
        )
        .order_by(dataset_field.c.id)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession,
    field_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(dataset_field)
        .where(
            dataset_field.c.id == field_id,
            dataset_field.c.tenant_id == tenant_id,
            dataset_field.c.workspace_id == workspace_id,
            dataset_field.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


exists_name_in_dataset = exists_name_in_dataset
exists_name_in_dataset_excluding = exists_name_in_dataset_excluding
find_response_by_id = find_response_by_id

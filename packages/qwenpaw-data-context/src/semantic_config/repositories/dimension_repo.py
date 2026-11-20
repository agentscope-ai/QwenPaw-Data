"""维度字典仓储（表 ``dimension_dict``，SPEC 00 §5.5）。

维度字典定义在业务域上、不再挂数据源：本模块不读写 ``datasource_id``、不 JOIN
``datasource``。唯一性 ``(tenant, ws, domain_id, dimension_name)``。

列表仍接受 ``datasource_id``，但它是**派生过滤**——经 ``domain.datasource_scope``
解析出入围域集合 D 后按 ``domain_id IN D`` 收敛，供建模工作台的单数据源视图使用
（SPEC 00 §15.3），并非字典自身的列。
"""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.services.domain_scope import domain_ids_for_datasource
from semantic_config.tables import dataset_dimension, dimension_dict, domain


def _response_select():
    return select(
        dimension_dict.c.id.label("dimension_id"),
        dimension_dict.c.domain_id,
        domain.c.domain_name,
        dimension_dict.c.dimension_name,
        dimension_dict.c.description,
        dimension_dict.c.synonyms,
        dimension_dict.c.dimension_type,
        dimension_dict.c.child_dimensions,
        dimension_dict.c.priority,
    ).select_from(
        dimension_dict.outerjoin(
            domain,
            and_(
                domain.c.id == dimension_dict.c.domain_id,
                domain.c.tenant_id == dimension_dict.c.tenant_id,
                domain.c.workspace_id == dimension_dict.c.workspace_id,
                domain.c.is_deleted == 0,
            ),
        )
    )


async def insert(
    db: AsyncSession, m, *, tenant_id: str, workspace_id: str, created_by: str
) -> int:
    ts = now_iso()
    values = dict(
        domain_id=m.domain_id,
        dimension_name=m.dimension_name,
        description=m.description,
        synonyms=m.synonyms,
        dimension_type=m.dimension_type,
        child_dimensions=m.child_dimensions,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        created_by=created_by,
        updated_by=created_by,
        created_at=ts,
        updated_at=ts,
    )
    if m.priority is not None:
        values["priority"] = m.priority
    res = await db.execute(sa_insert(dimension_dict).values(**values))
    return int(res.inserted_primary_key[0])


async def update(
    db: AsyncSession,
    dim_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    values = dict(
        dimension_name=m.dimension_name,
        description=m.description,
        synonyms=m.synonyms,
        dimension_type=m.dimension_type,
        child_dimensions=m.child_dimensions,
        updated_by=updated_by,
        updated_at=now_iso(),
    )
    if m.priority is not None:
        values["priority"] = m.priority
    res = await db.execute(
        sa_update(dimension_dict)
        .where(
            dimension_dict.c.id == dim_id,
            dimension_dict.c.tenant_id == tenant_id,
            dimension_dict.c.workspace_id == workspace_id,
            dimension_dict.c.is_deleted == 0,
        )
        .values(**values)
    )
    return res.rowcount


async def find_by_id(db: AsyncSession, dim_id: int, tenant_id: str, workspace_id: str):
    res = await db.execute(
        select(dimension_dict).where(
            dimension_dict.c.id == dim_id,
            dimension_dict.c.tenant_id == tenant_id,
            dimension_dict.c.workspace_id == workspace_id,
            dimension_dict.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, dim_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        dimension_dict.c.is_deleted == 0,
        dimension_dict.c.id == dim_id,
        dimension_dict.c.tenant_id == tenant_id,
        dimension_dict.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_by_name_in_domain(
    db: AsyncSession,
    domain_id: int,
    dimension_name: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(dimension_dict.c.id).where(
            dimension_dict.c.domain_id == domain_id,
            dimension_dict.c.dimension_name == dimension_name,
            dimension_dict.c.tenant_id == tenant_id,
            dimension_dict.c.workspace_id == workspace_id,
            dimension_dict.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_by_name_in_domain_excluding(
    db: AsyncSession,
    domain_id: int,
    dimension_name: str,
    dim_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(dimension_dict.c.id).where(
            dimension_dict.c.domain_id == domain_id,
            dimension_dict.c.dimension_name == dimension_name,
            dimension_dict.c.id != dim_id,
            dimension_dict.c.tenant_id == tenant_id,
            dimension_dict.c.workspace_id == workspace_id,
            dimension_dict.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _conds(datasource_id, domain_id, dimension_name, tenant_id, workspace_id):
    conds = [
        dimension_dict.c.is_deleted == 0,
        dimension_dict.c.tenant_id == tenant_id,
        dimension_dict.c.workspace_id == workspace_id,
    ]
    if datasource_id is not None:
        # 派生过滤：收敛到"至少有一个数据集落在该数据源上"的业务域（§15.3）。
        conds.append(
            dimension_dict.c.domain_id.in_(
                domain_ids_for_datasource(datasource_id, tenant_id, workspace_id)
            )
        )
    if domain_id is not None:
        conds.append(dimension_dict.c.domain_id == domain_id)
    if dimension_name:
        conds.append(dimension_dict.c.dimension_name.like(f"%{dimension_name}%"))
    return conds


async def count(
    db, domain_id, dimension_name, tenant_id, workspace_id, datasource_id=None
) -> int:
    conds = _conds(datasource_id, domain_id, dimension_name, tenant_id, workspace_id)
    res = await db.execute(
        select(func.count()).select_from(dimension_dict).where(*conds)
    )
    return int(res.scalar_one())


async def find_page(
    db,
    domain_id,
    dimension_name,
    limit,
    off,
    tenant_id,
    workspace_id,
    datasource_id=None,
):
    conds = _conds(datasource_id, domain_id, dimension_name, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(dimension_dict.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession, dim_id: int, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    res = await db.execute(
        sa_update(dimension_dict)
        .where(
            dimension_dict.c.id == dim_id,
            dimension_dict.c.tenant_id == tenant_id,
            dimension_dict.c.workspace_id == workspace_id,
            dimension_dict.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def count_bindings(
    db: AsyncSession, dim_id: int, tenant_id: str, workspace_id: str
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(dataset_dimension)
        .where(
            dataset_dimension.c.dimension_id == dim_id,
            dataset_dimension.c.tenant_id == tenant_id,
            dataset_dimension.c.workspace_id == workspace_id,
            dataset_dimension.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one())


exists_name_in_domain = exists_by_name_in_domain
exists_name_in_domain_excluding = exists_by_name_in_domain_excluding

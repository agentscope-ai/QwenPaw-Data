"""指标字典仓储（表 ``metric_dict``，SPEC 00 §5.6）。

与维度字典对称：指标定义在业务域上、不再挂数据源，唯一性
``(tenant, ws, domain_id, metric_name)``。列表的 ``datasource_id`` 是经
``domain.datasource_scope`` 收敛的派生过滤（SPEC 00 §15.3），非字典自身的列。
"""

from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.services.domain_scope import domain_ids_for_datasource
from semantic_config.tables import domain, metric_caliber, metric_dict


def _response_select():
    return select(
        metric_dict.c.id.label("metric_id"),
        metric_dict.c.domain_id,
        domain.c.domain_name,
        metric_dict.c.metric_name,
        metric_dict.c.description,
        metric_dict.c.synonyms,
        metric_dict.c.priority,
    ).select_from(
        metric_dict.outerjoin(
            domain,
            and_(
                domain.c.id == metric_dict.c.domain_id,
                domain.c.tenant_id == metric_dict.c.tenant_id,
                domain.c.workspace_id == metric_dict.c.workspace_id,
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
        metric_name=m.metric_name,
        description=m.description,
        synonyms=m.synonyms,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
        created_by=created_by,
        updated_by=created_by,
        created_at=ts,
        updated_at=ts,
    )
    if m.priority is not None:
        values["priority"] = m.priority
    res = await db.execute(sa_insert(metric_dict).values(**values))
    return int(res.inserted_primary_key[0])


async def update(
    db: AsyncSession,
    metric_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    values = dict(
        metric_name=m.metric_name,
        description=m.description,
        synonyms=m.synonyms,
        updated_by=updated_by,
        updated_at=now_iso(),
    )
    if m.priority is not None:
        values["priority"] = m.priority
    res = await db.execute(
        sa_update(metric_dict)
        .where(
            metric_dict.c.id == metric_id,
            metric_dict.c.tenant_id == tenant_id,
            metric_dict.c.workspace_id == workspace_id,
            metric_dict.c.is_deleted == 0,
        )
        .values(**values)
    )
    return res.rowcount


async def find_by_id(
    db: AsyncSession, metric_id: int, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(metric_dict).where(
            metric_dict.c.id == metric_id,
            metric_dict.c.tenant_id == tenant_id,
            metric_dict.c.workspace_id == workspace_id,
            metric_dict.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, metric_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        metric_dict.c.is_deleted == 0,
        metric_dict.c.id == metric_id,
        metric_dict.c.tenant_id == tenant_id,
        metric_dict.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_by_name_in_domain(
    db: AsyncSession,
    domain_id: int,
    metric_name: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(metric_dict.c.id).where(
            metric_dict.c.domain_id == domain_id,
            metric_dict.c.metric_name == metric_name,
            metric_dict.c.tenant_id == tenant_id,
            metric_dict.c.workspace_id == workspace_id,
            metric_dict.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_by_name_in_domain_excluding(
    db: AsyncSession,
    domain_id: int,
    metric_name: str,
    metric_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(metric_dict.c.id).where(
            metric_dict.c.domain_id == domain_id,
            metric_dict.c.metric_name == metric_name,
            metric_dict.c.id != metric_id,
            metric_dict.c.tenant_id == tenant_id,
            metric_dict.c.workspace_id == workspace_id,
            metric_dict.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _conds(datasource_id, domain_id, metric_name, tenant_id, workspace_id):
    conds = [
        metric_dict.c.is_deleted == 0,
        metric_dict.c.tenant_id == tenant_id,
        metric_dict.c.workspace_id == workspace_id,
    ]
    if datasource_id is not None:
        # 派生过滤：收敛到"至少有一个数据集落在该数据源上"的业务域（§15.3）。
        conds.append(
            metric_dict.c.domain_id.in_(
                domain_ids_for_datasource(datasource_id, tenant_id, workspace_id)
            )
        )
    if domain_id is not None:
        conds.append(metric_dict.c.domain_id == domain_id)
    if metric_name:
        conds.append(metric_dict.c.metric_name.like(f"%{metric_name}%"))
    return conds


async def count(
    db, domain_id, metric_name, tenant_id, workspace_id, datasource_id=None
) -> int:
    conds = _conds(datasource_id, domain_id, metric_name, tenant_id, workspace_id)
    res = await db.execute(select(func.count()).select_from(metric_dict).where(*conds))
    return int(res.scalar_one())


async def find_page(
    db, domain_id, metric_name, limit, off, tenant_id, workspace_id, datasource_id=None
):
    conds = _conds(datasource_id, domain_id, metric_name, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(metric_dict.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession,
    metric_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(metric_dict)
        .where(
            metric_dict.c.id == metric_id,
            metric_dict.c.tenant_id == tenant_id,
            metric_dict.c.workspace_id == workspace_id,
            metric_dict.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def count_calibers(
    db: AsyncSession, metric_id: int, tenant_id: str, workspace_id: str
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(metric_caliber)
        .where(
            metric_caliber.c.metric_id == metric_id,
            metric_caliber.c.tenant_id == tenant_id,
            metric_caliber.c.workspace_id == workspace_id,
            metric_caliber.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one())


exists_name_in_domain = exists_by_name_in_domain
exists_name_in_domain_excluding = exists_by_name_in_domain_excluding

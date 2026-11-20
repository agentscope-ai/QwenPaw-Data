"""业务域仓储（表 ``domain``，SPEC 00 §5.1）。

12 实体重构后业务域不再直连数据源：本模块不读写 ``datasource_id``、不 JOIN
``datasource``。域涉及的数据源由只读派生列 ``datasource_scope`` 表达，其写入
统一在 :mod:`semantic_config.services.domain_scope`。
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import (
    dataset,
    dimension_dict,
    domain,
    metric_dict,
    verified_query,
)


def _response_select():
    return select(
        domain.c.id.label("domain_id"),
        domain.c.domain_name,
        domain.c.description,
        domain.c.datasource_scope,
    ).select_from(domain)


async def insert(
    db: AsyncSession,
    domain_name: str | None,
    description: str | None,
    *,
    import_id: str | None = None,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
) -> int:
    ts = now_iso()
    res = await db.execute(
        sa_insert(domain).values(
            domain_name=domain_name,
            description=description,
            # 新建的域尚无数据集，范围为空；由 dataset 写操作重算填充。
            datasource_scope="",
            import_id=import_id,
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
    domain_id: int,
    domain_name: str | None,
    description: str | None,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(domain)
        .where(
            domain.c.id == domain_id,
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
        )
        .values(
            domain_name=domain_name,
            description=description,
            updated_by=updated_by,
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def find_by_id(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(domain).where(
            domain.c.id == domain_id,
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        domain.c.is_deleted == 0,
        domain.c.id == domain_id,
        domain.c.tenant_id == tenant_id,
        domain.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_by_name(
    db: AsyncSession,
    domain_name: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    """唯一性：``(tenant, ws, domain_name)``——已去掉 datasource 维度。"""
    res = await db.execute(
        select(domain.c.id).where(
            domain.c.domain_name == domain_name,
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_by_name_excluding(
    db: AsyncSession,
    domain_name: str,
    domain_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    res = await db.execute(
        select(domain.c.id).where(
            domain.c.domain_name == domain_name,
            domain.c.id != domain_id,
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _conds(domain_name: str | None, tenant_id: str, workspace_id: str) -> list:
    conds = [
        domain.c.is_deleted == 0,
        domain.c.tenant_id == tenant_id,
        domain.c.workspace_id == workspace_id,
    ]
    if domain_name:
        conds.append(domain.c.domain_name.like(f"%{domain_name}%"))
    return conds


async def count(
    db: AsyncSession,
    domain_name: str | None,
    tenant_id: str,
    workspace_id: str,
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(domain)
        .where(*_conds(domain_name, tenant_id, workspace_id))
    )
    return int(res.scalar_one())


async def find_page(
    db: AsyncSession,
    domain_name: str | None,
    limit: int,
    off: int,
    tenant_id: str,
    workspace_id: str,
):
    stmt = (
        _response_select()
        .where(*_conds(domain_name, tenant_id, workspace_id))
        .order_by(domain.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession,
    domain_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(domain)
        .where(
            domain.c.id == domain_id,
            domain.c.tenant_id == tenant_id,
            domain.c.workspace_id == workspace_id,
            domain.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


async def _count(
    db: AsyncSession, table, domain_id: int, tenant_id: str, workspace_id: str
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(table)
        .where(
            table.c.domain_id == domain_id,
            table.c.tenant_id == tenant_id,
            table.c.workspace_id == workspace_id,
            table.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one())


async def count_datasets(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
) -> int:
    return await _count(db, dataset, domain_id, tenant_id, workspace_id)


async def count_dimensions(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
) -> int:
    return await _count(db, dimension_dict, domain_id, tenant_id, workspace_id)


async def count_metrics(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
) -> int:
    return await _count(db, metric_dict, domain_id, tenant_id, workspace_id)


async def count_verified_queries(
    db: AsyncSession, domain_id: int, tenant_id: str, workspace_id: str
) -> int:
    return await _count(db, verified_query, domain_id, tenant_id, workspace_id)

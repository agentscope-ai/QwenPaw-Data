from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.tables import dataset, datasource


async def insert(
    db: AsyncSession,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    config_json: str | None = None,
    *,
    description: str | None = None,
    tenant_id: str,
    workspace_id: str,
    created_by: str,
) -> int:
    ts = now_iso()
    res = await db.execute(
        sa_insert(datasource).values(
            datasource_id=datasource_id,
            datasource_name=name,
            datasource_type=dtype,
            config=config_json,
            description=description,
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
    ds_id: int,
    *,
    tenant_id: str,
    workspace_id: str,
    name: str | None = None,
    dtype: str | None = None,
    config_json: str | None = None,
    update_config: bool = False,
    description: str | None = None,
    update_description: bool = False,
    updated_by: str,
) -> int:
    """按字段更新数据源。仅更新非 None 的 name/dtype；config/description 仅在各自 update_*=True 时替换。"""
    values: dict = {}
    if name is not None:
        values["datasource_name"] = name
    if dtype is not None:
        values["datasource_type"] = dtype
    if update_config:
        values["config"] = config_json
    if update_description:
        values["description"] = description
    values["updated_by"] = updated_by
    values["updated_at"] = now_iso()
    res = await db.execute(
        sa_update(datasource)
        .where(
            datasource.c.id == ds_id,
            datasource.c.tenant_id == tenant_id,
            datasource.c.workspace_id == workspace_id,
            datasource.c.is_deleted == 0,
        )
        .values(**values)
    )
    return res.rowcount


async def find_by_id(db: AsyncSession, ds_id: int, tenant_id: str, workspace_id: str):
    res = await db.execute(
        select(datasource).where(
            datasource.c.id == ds_id,
            datasource.c.tenant_id == tenant_id,
            datasource.c.workspace_id == workspace_id,
            datasource.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_by_datasource_id(
    db: AsyncSession, datasource_id: str, tenant_id: str, workspace_id: str
):
    res = await db.execute(
        select(datasource).where(
            datasource.c.datasource_id == datasource_id,
            datasource.c.tenant_id == tenant_id,
            datasource.c.workspace_id == workspace_id,
            datasource.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_by_name_and_type(
    db: AsyncSession, name: str, dtype: str, tenant_id: str, workspace_id: str
):
    """精确「名 + 类型」查（tenant/workspace 域内、未删）；创建幂等判据。

    与 ``_conds`` 的 name 模糊匹配不同，这里是等值匹配；多行时取最早（id 升序）一条。
    """
    res = await db.execute(
        select(datasource)
        .where(
            datasource.c.datasource_name == name,
            datasource.c.datasource_type == dtype,
            datasource.c.tenant_id == tenant_id,
            datasource.c.workspace_id == workspace_id,
            datasource.c.is_deleted == 0,
        )
        .order_by(datasource.c.id.asc())
    )
    return res.mappings().first()


def _conds(
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    tenant_id: str,
    workspace_id: str,
) -> list:
    conds = [
        datasource.c.is_deleted == 0,
        datasource.c.tenant_id == tenant_id,
        datasource.c.workspace_id == workspace_id,
    ]
    if datasource_id:
        conds.append(datasource.c.datasource_id.like(f"%{datasource_id}%"))
    if name:
        conds.append(datasource.c.datasource_name.like(f"%{name}%"))
    if dtype:
        conds.append(datasource.c.datasource_type == dtype)
    return conds


async def count(
    db: AsyncSession,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    tenant_id: str,
    workspace_id: str,
) -> int:
    conds = _conds(datasource_id, name, dtype, tenant_id, workspace_id)
    res = await db.execute(select(func.count()).select_from(datasource).where(*conds))
    return int(res.scalar_one())


async def find_page(
    db: AsyncSession,
    datasource_id: str | None,
    name: str | None,
    dtype: str | None,
    limit: int,
    off: int,
    tenant_id: str,
    workspace_id: str,
):
    conds = _conds(datasource_id, name, dtype, tenant_id, workspace_id)
    res = await db.execute(
        select(datasource)
        .where(*conds)
        .order_by(datasource.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    return res.mappings().all()


async def find_metadata_page(
    db: AsyncSession, limit: int, off: int, tenant_id: str, workspace_id: str
):
    """Read only credential-free identity fields for one Workspace."""
    res = await db.execute(
        select(datasource.c.datasource_id, datasource.c.datasource_name, datasource.c.datasource_type)
        .where(*_conds(None, None, None, tenant_id, workspace_id))
        .order_by(datasource.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession, ds_id: int, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    res = await db.execute(
        sa_update(datasource)
        .where(
            datasource.c.id == ds_id,
            datasource.c.tenant_id == tenant_id,
            datasource.c.workspace_id == workspace_id,
            datasource.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount


# ---- 引用计数（删除校验用）：子表用数据源编码关联，按 编码 + tenant/ws 统计 ----
# 12 实体重构：业务域已与数据源解耦，删除数据源**不再检查业务域**，只看数据集（SPEC 00 §6）。
async def count_datasets(
    db: AsyncSession, datasource_id: str, tenant_id: str, workspace_id: str
) -> int:
    res = await db.execute(
        select(func.count())
        .select_from(dataset)
        .where(
            dataset.c.datasource_id == datasource_id,
            dataset.c.tenant_id == tenant_id,
            dataset.c.workspace_id == workspace_id,
            dataset.c.is_deleted == 0,
        )
    )
    return int(res.scalar_one())

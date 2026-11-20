from __future__ import annotations

from sqlalchemy import and_, func, select
from sqlalchemy import insert as sa_insert
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from semantic_config.db import now_iso
from semantic_config.errors import BadRequestError
from semantic_config.tables import datasource, physical_table, table_column

# v2.1（③a）：key=(datasource_id, table_name, column_name)；dataset_id/domain_id 退役，
# 业务语义（中/英文名、枚举、维度类型）迁 L3 dataset_field。
# 物理层重构：table_column 不存 table_name，改存 table_id → physical_table.id。
# 对外 API 仍收/发 table_name，name↔id 转换全部收口在本 repo 边界（读 join、写解析）。


def _physical_table_join():
    """table_column → physical_table（按 table_id，同租户/ws，未软删）。表名反解唯一来源。"""
    return and_(
        physical_table.c.id == table_column.c.table_id,
        physical_table.c.tenant_id == table_column.c.tenant_id,
        physical_table.c.workspace_id == table_column.c.workspace_id,
        physical_table.c.is_deleted == 0,
    )


async def _resolve_table_id(
    db: AsyncSession,
    datasource_id: str,
    table_name: str,
    tenant_id: str,
    workspace_id: str,
) -> int | None:
    """(datasource_id, table_name) → physical_table.id；不存在返回 None（不自动建表）。"""
    res = await db.execute(
        select(physical_table.c.id).where(
            physical_table.c.datasource_id == datasource_id,
            physical_table.c.table_name == table_name,
            physical_table.c.tenant_id == tenant_id,
            physical_table.c.workspace_id == workspace_id,
            physical_table.c.is_deleted == 0,
        )
    )
    row = res.first()
    return int(row[0]) if row is not None else None


def _response_select():
    return select(
        table_column.c.id,
        table_column.c.datasource_id,
        datasource.c.datasource_name,
        physical_table.c.table_name,
        table_column.c.column_name,
        table_column.c.data_type,
        table_column.c.column_comment,
        table_column.c.sample_values,
        table_column.c.is_primary,
        table_column.c.is_nullable,
    ).select_from(
        table_column.outerjoin(
            datasource,
            and_(
                datasource.c.datasource_id == table_column.c.datasource_id,
                datasource.c.tenant_id == table_column.c.tenant_id,
                datasource.c.workspace_id == table_column.c.workspace_id,
                datasource.c.is_deleted == 0,
            ),
        ).outerjoin(physical_table, _physical_table_join())
    )


async def insert(
    db: AsyncSession, m, *, tenant_id: str, workspace_id: str, created_by: str
) -> int:
    ts = now_iso()
    # table_id NOT NULL：表名必须已在 physical_table 声明，未声明即硬失败（不自动建表）。
    table_id = await _resolve_table_id(
        db, m.datasource_id, m.table_name, tenant_id, workspace_id
    )
    if table_id is None:
        raise BadRequestError(
            f"物理表不存在（请先创建 table）: table_name={m.table_name}"
        )
    res = await db.execute(
        sa_insert(table_column).values(
            datasource_id=m.datasource_id,
            table_id=table_id,
            column_name=m.column_name,
            data_type=m.data_type,
            column_comment=m.column_comment,
            sample_values=m.sample_values,
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
    col_id: int,
    m,
    *,
    tenant_id: str,
    workspace_id: str,
    updated_by: str,
) -> int:
    res = await db.execute(
        sa_update(table_column)
        .where(
            table_column.c.id == col_id,
            table_column.c.tenant_id == tenant_id,
            table_column.c.workspace_id == workspace_id,
            table_column.c.is_deleted == 0,
        )
        .values(
            column_name=m.column_name,
            data_type=m.data_type,
            column_comment=m.column_comment,
            sample_values=m.sample_values,
            is_primary=m.is_primary,
            is_nullable=m.is_nullable,
            updated_by=updated_by,
            updated_at=now_iso(),
        )
    )
    return res.rowcount


async def find_by_id(db: AsyncSession, col_id: int, tenant_id: str, workspace_id: str):
    # 附带 physical_table.table_name 反解，供 service 层（更新查重/报错）读 table_name。
    res = await db.execute(
        select(table_column, physical_table.c.table_name.label("table_name"))
        .select_from(table_column.outerjoin(physical_table, _physical_table_join()))
        .where(
            table_column.c.id == col_id,
            table_column.c.tenant_id == tenant_id,
            table_column.c.workspace_id == workspace_id,
            table_column.c.is_deleted == 0,
        )
    )
    return res.mappings().first()


async def find_response_by_id(
    db: AsyncSession, col_id: int, tenant_id: str, workspace_id: str
):
    stmt = _response_select().where(
        table_column.c.is_deleted == 0,
        table_column.c.id == col_id,
        table_column.c.tenant_id == tenant_id,
        table_column.c.workspace_id == workspace_id,
    )
    res = await db.execute(stmt)
    return res.mappings().first()


async def exists_name_in_table(
    db: AsyncSession,
    datasource_id: str,
    table_name: str,
    column_name: str,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    table_id = await _resolve_table_id(
        db, datasource_id, table_name, tenant_id, workspace_id
    )
    if table_id is None:
        return False
    res = await db.execute(
        select(table_column.c.id).where(
            table_column.c.datasource_id == datasource_id,
            table_column.c.table_id == table_id,
            table_column.c.column_name == column_name,
            table_column.c.tenant_id == tenant_id,
            table_column.c.workspace_id == workspace_id,
            table_column.c.is_deleted == 0,
        )
    )
    return res.first() is not None


async def exists_name_in_table_excluding(
    db: AsyncSession,
    datasource_id: str,
    table_name: str,
    column_name: str,
    col_id: int,
    tenant_id: str,
    workspace_id: str,
) -> bool:
    table_id = await _resolve_table_id(
        db, datasource_id, table_name, tenant_id, workspace_id
    )
    if table_id is None:
        return False
    res = await db.execute(
        select(table_column.c.id).where(
            table_column.c.datasource_id == datasource_id,
            table_column.c.table_id == table_id,
            table_column.c.column_name == column_name,
            table_column.c.id != col_id,
            table_column.c.tenant_id == tenant_id,
            table_column.c.workspace_id == workspace_id,
            table_column.c.is_deleted == 0,
        )
    )
    return res.first() is not None


def _conds(datasource_id, table_id, tenant_id, workspace_id):
    conds = [
        table_column.c.is_deleted == 0,
        table_column.c.tenant_id == tenant_id,
        table_column.c.workspace_id == workspace_id,
    ]
    if datasource_id is not None:
        conds.append(table_column.c.datasource_id == datasource_id)
    if table_id is not None:
        conds.append(table_column.c.table_id == table_id)
    return conds


async def count(db, datasource_id, table_name, tenant_id, workspace_id) -> int:
    if table_name:
        table_id = await _resolve_table_id(
            db, datasource_id, table_name, tenant_id, workspace_id
        )
        if table_id is None:
            return 0  # 表未声明 → 无列
    else:
        table_id = None
    conds = _conds(datasource_id, table_id, tenant_id, workspace_id)
    res = await db.execute(select(func.count()).select_from(table_column).where(*conds))
    return int(res.scalar_one())


async def find_page(db, datasource_id, table_name, limit, off, tenant_id, workspace_id):
    if table_name:
        table_id = await _resolve_table_id(
            db, datasource_id, table_name, tenant_id, workspace_id
        )
        if table_id is None:
            return []
    else:
        table_id = None
    conds = _conds(datasource_id, table_id, tenant_id, workspace_id)
    stmt = (
        _response_select()
        .where(*conds)
        .order_by(table_column.c.id.desc())
        .limit(limit)
        .offset(off)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def find_all_by_table(
    db: AsyncSession,
    datasource_id: str,
    table_name: str,
    tenant_id: str,
    workspace_id: str,
):
    table_id = await _resolve_table_id(
        db, datasource_id, table_name, tenant_id, workspace_id
    )
    if table_id is None:
        return []
    stmt = (
        _response_select()
        .where(*_conds(datasource_id, table_id, tenant_id, workspace_id))
        .order_by(table_column.c.id)
    )
    res = await db.execute(stmt)
    return res.mappings().all()


async def soft_delete(
    db: AsyncSession, col_id: int, *, tenant_id: str, workspace_id: str, updated_by: str
) -> int:
    res = await db.execute(
        sa_update(table_column)
        .where(
            table_column.c.id == col_id,
            table_column.c.tenant_id == tenant_id,
            table_column.c.workspace_id == workspace_id,
            table_column.c.is_deleted == 0,
        )
        .values(is_deleted=1, updated_by=updated_by, updated_at=now_iso())
    )
    return res.rowcount

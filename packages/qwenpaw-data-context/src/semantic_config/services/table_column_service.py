from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.table_column import (
    TableColumnCreate,
    TableColumnResponse,
    TableColumnUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo
from semantic_config.repositories import table_column_repo as repo


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    return type(payload)(
        **{
            f: (getattr(payload, f) if getattr(payload, f) is not None else existing[f])
            for f in type(payload).model_fields
        }
    )


def _to_response(row: aiosqlite.Row) -> TableColumnResponse:
    return TableColumnResponse(**{k: row[k] for k in row.keys()})


async def create(
    db: aiosqlite.Connection, payload: TableColumnCreate, principal: Principal
) -> TableColumnResponse:
    """v2.1：key=(datasource_id, table_name, column_name)；column_comment 不再必填。"""
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if payload.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if not payload.table_name:
        raise BadRequestError("table_name 不能为空")
    if not payload.column_name:
        raise BadRequestError("column_name 不能为空")
    ds = await datasource_repo.find_by_datasource_id(
        db, payload.datasource_id, tid, wid
    )
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={payload.datasource_id}")
    if await repo.exists_name_in_table(
        db,
        payload.datasource_id,
        payload.table_name,
        payload.column_name,
        ds["tenant_id"],
        ds["workspace_id"],
    ):
        raise BadRequestError(f"列已存在: {payload.table_name}.{payload.column_name}")
    new_id = await repo.insert(
        db,
        payload,
        tenant_id=ds["tenant_id"],
        workspace_id=ds["workspace_id"],
        created_by=principal.user_id,
    )
    return _to_response(
        await repo.find_response_by_id(db, new_id, ds["tenant_id"], ds["workspace_id"])
    )


async def update(
    db: aiosqlite.Connection,
    col_id: int,
    payload: TableColumnUpdate,
    principal: Principal,
) -> TableColumnResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, col_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"列不存在: id={col_id}")
    merged = _merge(payload, existing)
    if merged.column_name != existing["column_name"]:
        if await repo.exists_name_in_table_excluding(
            db,
            existing["datasource_id"],
            existing["table_name"],
            merged.column_name,
            col_id,
            tid,
            wid,
        ):
            raise BadRequestError(
                f"列已存在: {existing['table_name']}.{merged.column_name}"
            )
    await repo.update(
        db,
        col_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, col_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, col_id: int, principal: Principal
) -> TableColumnResponse:
    row = await repo.find_response_by_id(
        db, col_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db, datasource_id, table_name, page, size, principal: Principal
) -> Page[TableColumnResponse]:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, datasource_id, table_name, tid, wid)
    rows = await repo.find_page(
        db, datasource_id, table_name, size, offset(page, size), tid, wid
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def list_by_table(
    db, datasource_id: str, table_name: str, principal: Principal
) -> list[TableColumnResponse]:
    rows = await repo.find_all_by_table(
        db,
        datasource_id,
        table_name,
        principal.tenant_id,
        principal.active_workspace_id,
    )
    return [_to_response(r) for r in rows]


async def delete(db: aiosqlite.Connection, col_id: int, principal: Principal) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, col_id, tid, wid) is None:
        raise NotFoundError(f"列不存在: id={col_id}")
    await repo.soft_delete(
        db, col_id, tenant_id=tid, workspace_id=wid, updated_by=principal.user_id
    )
    return {"message": "deleted", "id": str(col_id)}

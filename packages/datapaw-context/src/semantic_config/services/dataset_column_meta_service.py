from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dataset_column_meta import ColumnCreate, ColumnResponse, ColumnUpdate
from semantic_config.pagination import Page, offset
from semantic_config.repositories import dataset_column_meta_repo as repo
from semantic_config.repositories import dataset_meta_repo


def _to_response(row: aiosqlite.Row) -> ColumnResponse:
    return ColumnResponse(**{k: row[k] for k in row.keys()})


async def create(db: aiosqlite.Connection, payload: ColumnCreate) -> ColumnResponse:
    if payload.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if payload.dataset_id is None:
        raise BadRequestError("dataset_id 不能为空")
    if not payload.column_name:
        raise BadRequestError("column_name 不能为空")
    if not payload.column_comment:
        raise BadRequestError("column_comment 不能为空")
    if await dataset_meta_repo.find_by_id(db, payload.dataset_id) is None:
        raise BadRequestError(f"数据集不存在: dataset_id={payload.dataset_id}")
    if await repo.exists_name_in_dataset(db, payload.dataset_id, payload.column_name):
        raise BadRequestError(f"列名已存在: {payload.column_name}")
    new_id = await repo.insert(db, payload)
    return _to_response(await repo.find_response_by_id(db, new_id))


async def update(db: aiosqlite.Connection, col_id: int, payload: ColumnUpdate) -> ColumnResponse:
    existing = await repo.find_by_id(db, col_id)
    if existing is None:
        raise NotFoundError(f"列不存在: id={col_id}")
    if payload.column_name and payload.column_name != existing["column_name"]:
        if await repo.exists_name_in_dataset_excluding(
            db, existing["dataset_id"], payload.column_name, col_id
        ):
            raise BadRequestError(f"列名已存在: {payload.column_name}")
    await repo.update(db, col_id, payload)
    return _to_response(await repo.find_response_by_id(db, col_id))


async def get_one(db: aiosqlite.Connection, col_id: int) -> ColumnResponse:
    row = await repo.find_response_by_id(db, col_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, datasource_id, domain_id, dataset_id, page, size):
    total = await repo.count(db, datasource_id, domain_id, dataset_id)
    rows = await repo.find_page(db, datasource_id, domain_id, dataset_id, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def list_by_dataset(db, dataset_id) -> list[ColumnResponse]:
    rows = await repo.find_all_by_dataset(db, dataset_id)
    return [_to_response(r) for r in rows]


async def delete(db: aiosqlite.Connection, col_id: int) -> dict:
    if await repo.find_by_id(db, col_id) is None:
        raise NotFoundError(f"列不存在: id={col_id}")
    await repo.soft_delete(db, col_id)
    return {"message": "deleted", "id": str(col_id)}

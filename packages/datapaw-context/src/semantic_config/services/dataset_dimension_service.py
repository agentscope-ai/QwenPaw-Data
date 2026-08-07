from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dataset_dimension import (
    DatasetDimensionCreate,
    DatasetDimensionResponse,
    DatasetDimensionUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.repositories import dataset_dimension_repo as repo
from semantic_config.repositories import dataset_meta_repo, dimension_repo


def _to_response(row: aiosqlite.Row) -> DatasetDimensionResponse:
    return DatasetDimensionResponse(**{k: row[k] for k in row.keys()})


async def create(db: aiosqlite.Connection, payload: DatasetDimensionCreate) -> DatasetDimensionResponse:
    if payload.dataset_id is None:
        raise BadRequestError("dataset_id 不能为空")
    if payload.dimension_id is None:
        raise BadRequestError("dimension_id 不能为空")
    if await dataset_meta_repo.find_by_id(db, payload.dataset_id) is None:
        raise BadRequestError(f"数据集不存在: dataset_id={payload.dataset_id}")
    if await dimension_repo.find_by_id(db, payload.dimension_id) is None:
        raise BadRequestError(f"维度不存在: dimension_id={payload.dimension_id}")
    new_id = await repo.insert(db, payload)
    return _to_response(await repo.find_response_by_id(db, new_id))


async def update(
    db: aiosqlite.Connection, dd_id: int, payload: DatasetDimensionUpdate
) -> DatasetDimensionResponse:
    if await repo.find_by_id(db, dd_id) is None:
        raise NotFoundError(f"维度口径不存在: id={dd_id}")
    await repo.update(db, dd_id, payload)
    return _to_response(await repo.find_response_by_id(db, dd_id))


async def get_one(db: aiosqlite.Connection, dd_id: int) -> DatasetDimensionResponse:
    row = await repo.find_response_by_id(db, dd_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, datasource_id, domain_id, dataset_id, dataset_name, dimension_name, page, size):
    total = await repo.count(db, datasource_id, domain_id, dataset_id, dataset_name, dimension_name)
    rows = await repo.find_page(
        db, datasource_id, domain_id, dataset_id, dataset_name, dimension_name, size, offset(page, size)
    )
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def list_by_dataset(db, dataset_id) -> list[DatasetDimensionResponse]:
    rows = await repo.find_all_by_dataset(db, dataset_id)
    return [_to_response(r) for r in rows]


async def delete(db: aiosqlite.Connection, dd_id: int) -> dict:
    if await repo.find_by_id(db, dd_id) is None:
        raise NotFoundError(f"维度口径不存在: id={dd_id}")
    await repo.soft_delete(db, dd_id)
    return {"message": "deleted", "id": str(dd_id)}


async def delete_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> dict:
    await repo.soft_delete_by_dataset(db, dataset_id)
    return {"message": "deleted by dataset", "dataset_id": str(dataset_id)}

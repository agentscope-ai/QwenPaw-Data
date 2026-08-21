from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dataset_meta import DatasetMetaCreate, DatasetMetaResponse, DatasetMetaUpdate
from semantic_config.pagination import Page, offset
from semantic_config.repositories import biz_domain_repo, datasource_repo
from semantic_config.repositories import dataset_meta_repo as repo


def _to_response(row: aiosqlite.Row) -> DatasetMetaResponse:
    return DatasetMetaResponse(**{k: row[k] for k in row.keys()})


async def create(db: aiosqlite.Connection, payload: DatasetMetaCreate) -> DatasetMetaResponse:
    if payload.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if not payload.dataset_name:
        raise BadRequestError("dataset_name 不能为空")
    if await datasource_repo.find_by_datasource_id(db, payload.datasource_id) is None:
        raise BadRequestError(f"数据源不存在: datasource_id={payload.datasource_id}")
    if await biz_domain_repo.find_by_id(db, payload.domain_id) is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    if await repo.exists_name_in_domain(db, payload.domain_id, payload.dataset_name):
        raise BadRequestError(f"数据集名称已存在: {payload.dataset_name}")
    new_id = await repo.insert(db, payload)
    return _to_response(await repo.find_response_by_id(db, new_id))


async def update(db: aiosqlite.Connection, dataset_id: int, payload: DatasetMetaUpdate) -> DatasetMetaResponse:
    existing = await repo.find_by_id(db, dataset_id)
    if existing is None:
        raise NotFoundError(f"数据集不存在: id={dataset_id}")
    if payload.dataset_name and payload.dataset_name != existing["dataset_name"]:
        if await repo.exists_name_in_domain_excluding(
            db, existing["domain_id"], payload.dataset_name, dataset_id
        ):
            raise BadRequestError(f"数据集名称已存在: {payload.dataset_name}")
    await repo.update(db, dataset_id, payload)
    return _to_response(await repo.find_response_by_id(db, dataset_id))


async def get_one(db: aiosqlite.Connection, dataset_id: int) -> DatasetMetaResponse:
    row = await repo.find_response_by_id(db, dataset_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, datasource_id, domain_id, dataset_name, dataset_type, page, size):
    total = await repo.count(db, datasource_id, domain_id, dataset_name, dataset_type)
    rows = await repo.find_page(db, datasource_id, domain_id, dataset_name, dataset_type, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def delete(db: aiosqlite.Connection, dataset_id: int) -> dict:
    if await repo.find_by_id(db, dataset_id) is None:
        raise NotFoundError(f"数据集不存在: id={dataset_id}")
    cols = await repo.count_columns(db, dataset_id)
    dims = await repo.count_dimensions(db, dataset_id)
    vals = await repo.count_dimension_values(db, dataset_id)
    formulas = await repo.count_formulas(db, dataset_id)
    if cols or dims or vals or formulas:
        raise BadRequestError(
            f"数据集被引用，无法删除：列 {cols} 个、维度口径 {dims} 个、维度值 {vals} 个、指标口径 {formulas} 个"
        )
    await repo.soft_delete(db, dataset_id)
    return {"message": "deleted", "id": str(dataset_id)}

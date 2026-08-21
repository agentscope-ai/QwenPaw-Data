from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.metric_formula_lib import FormulaCreate, FormulaResponse, FormulaUpdate
from semantic_config.pagination import Page, offset
from semantic_config.repositories import dataset_meta_repo, metric_lib_repo
from semantic_config.repositories import metric_formula_lib_repo as repo


def _to_response(row: aiosqlite.Row) -> FormulaResponse:
    return FormulaResponse(**{k: row[k] for k in row.keys()})


async def create(db: aiosqlite.Connection, payload: FormulaCreate) -> FormulaResponse:
    if payload.metric_id is None:
        raise BadRequestError("metric_id 不能为空")
    if payload.dataset_id is None:
        raise BadRequestError("dataset_id 不能为空")
    if await metric_lib_repo.find_by_id(db, payload.metric_id) is None:
        raise BadRequestError(f"指标不存在: metric_id={payload.metric_id}")
    if await dataset_meta_repo.find_by_id(db, payload.dataset_id) is None:
        raise BadRequestError(f"数据集不存在: dataset_id={payload.dataset_id}")
    new_id = await repo.insert(db, payload)
    return _to_response(await repo.find_response_by_id(db, new_id))


async def update(db: aiosqlite.Connection, fid: int, payload: FormulaUpdate) -> FormulaResponse:
    if await repo.find_by_id(db, fid) is None:
        raise NotFoundError(f"指标口径不存在: id={fid}")
    await repo.update(db, fid, payload)
    return _to_response(await repo.find_response_by_id(db, fid))


async def get_one(db: aiosqlite.Connection, fid: int) -> FormulaResponse:
    row = await repo.find_response_by_id(db, fid)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, datasource_id, domain_id, metric_id, dataset_id, page, size):
    total = await repo.count(db, datasource_id, domain_id, metric_id, dataset_id)
    rows = await repo.find_page(db, datasource_id, domain_id, metric_id, dataset_id, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def list_by_dataset(db, dataset_id) -> list[FormulaResponse]:
    rows = await repo.find_all_by_dataset(db, dataset_id)
    return [_to_response(r) for r in rows]


async def delete(db: aiosqlite.Connection, fid: int) -> dict:
    if await repo.find_by_id(db, fid) is None:
        raise NotFoundError(f"指标口径不存在: id={fid}")
    await repo.soft_delete(db, fid)
    return {"message": "deleted", "id": str(fid)}


async def delete_by_dataset(db: aiosqlite.Connection, dataset_id: int) -> dict:
    await repo.soft_delete_by_dataset(db, dataset_id)
    return {"message": "deleted by dataset", "dataset_id": str(dataset_id)}

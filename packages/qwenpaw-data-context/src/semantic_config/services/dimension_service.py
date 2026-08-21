from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dimension import DimensionCreate, DimensionResponse, DimensionUpdate
from semantic_config.pagination import Page, offset
from semantic_config.repositories import biz_domain_repo, datasource_repo
from semantic_config.repositories import dimension_repo as repo


def _to_response(row: aiosqlite.Row) -> DimensionResponse:
    d = {k: row[k] for k in row.keys()}
    for f in ("is_visible", "is_attribution"):
        if d.get(f) is not None:
            d[f] = bool(d[f])
    return DimensionResponse(**d)


async def create(db: aiosqlite.Connection, payload: DimensionCreate) -> DimensionResponse:
    if payload.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if not payload.dimension_name:
        raise BadRequestError("dimension_name 不能为空")
    if await datasource_repo.find_by_datasource_id(db, payload.datasource_id) is None:
        raise BadRequestError(f"数据源不存在: datasource_id={payload.datasource_id}")
    if await biz_domain_repo.find_by_id(db, payload.domain_id) is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    if await repo.exists_name_in_domain(db, payload.domain_id, payload.dimension_name):
        raise BadRequestError(f"维度名称已存在: {payload.dimension_name}")
    new_id = await repo.insert(db, payload)
    return _to_response(await repo.find_response_by_id(db, new_id))


async def update(db: aiosqlite.Connection, dim_id: int, payload: DimensionUpdate) -> DimensionResponse:
    existing = await repo.find_by_id(db, dim_id)
    if existing is None:
        raise NotFoundError(f"维度不存在: id={dim_id}")
    if payload.dimension_name and payload.dimension_name != existing["dimension_name"]:
        if await repo.exists_name_in_domain_excluding(
            db, existing["domain_id"], payload.dimension_name, dim_id
        ):
            raise BadRequestError(f"维度名称已存在: {payload.dimension_name}")
    await repo.update(db, dim_id, payload)
    return _to_response(await repo.find_response_by_id(db, dim_id))


async def get_one(db: aiosqlite.Connection, dim_id: int) -> DimensionResponse:
    row = await repo.find_response_by_id(db, dim_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, datasource_id, domain_id, dimension_name, page, size):
    total = await repo.count(db, datasource_id, domain_id, dimension_name)
    rows = await repo.find_page(db, datasource_id, domain_id, dimension_name, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def delete(db: aiosqlite.Connection, dim_id: int) -> dict:
    if await repo.find_by_id(db, dim_id) is None:
        raise NotFoundError(f"维度不存在: id={dim_id}")
    bindings = await repo.count_bindings(db, dim_id)
    if bindings > 0:
        raise BadRequestError(f"维度被 {bindings} 条维度口径引用，无法删除")
    await repo.soft_delete(db, dim_id)
    return {"message": "deleted", "id": str(dim_id)}

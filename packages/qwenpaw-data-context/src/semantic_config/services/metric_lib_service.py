from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.metric_lib import MetricCreate, MetricResponse, MetricUpdate
from semantic_config.pagination import Page, offset
from semantic_config.repositories import biz_domain_repo, datasource_repo
from semantic_config.repositories import metric_lib_repo as repo


def _to_response(row: aiosqlite.Row) -> MetricResponse:
    d = {k: row[k] for k in row.keys()}
    for f in ("is_polaris", "show_distribution", "is_visible"):
        if d.get(f) is not None:
            d[f] = bool(d[f])
    return MetricResponse(**d)


async def create(db: aiosqlite.Connection, payload: MetricCreate) -> MetricResponse:
    if payload.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if not payload.metric_name:
        raise BadRequestError("metric_name 不能为空")
    if await datasource_repo.find_by_datasource_id(db, payload.datasource_id) is None:
        raise BadRequestError(f"数据源不存在: datasource_id={payload.datasource_id}")
    if await biz_domain_repo.find_by_id(db, payload.domain_id) is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    if await repo.exists_name_in_domain(db, payload.domain_id, payload.metric_name):
        raise BadRequestError(f"指标名称已存在: {payload.metric_name}")
    new_id = await repo.insert(db, payload)
    return _to_response(await repo.find_response_by_id(db, new_id))


async def update(db: aiosqlite.Connection, metric_id: int, payload: MetricUpdate) -> MetricResponse:
    existing = await repo.find_by_id(db, metric_id)
    if existing is None:
        raise NotFoundError(f"指标不存在: id={metric_id}")
    if payload.metric_name and payload.metric_name != existing["metric_name"]:
        if await repo.exists_name_in_domain_excluding(
            db, existing["domain_id"], payload.metric_name, metric_id
        ):
            raise BadRequestError(f"指标名称已存在: {payload.metric_name}")
    await repo.update(db, metric_id, payload)
    return _to_response(await repo.find_response_by_id(db, metric_id))


async def get_one(db: aiosqlite.Connection, metric_id: int) -> MetricResponse:
    row = await repo.find_response_by_id(db, metric_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, datasource_id, domain_id, metric_name, page, size):
    total = await repo.count(db, datasource_id, domain_id, metric_name)
    rows = await repo.find_page(db, datasource_id, domain_id, metric_name, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def delete(db: aiosqlite.Connection, metric_id: int) -> dict:
    if await repo.find_by_id(db, metric_id) is None:
        raise NotFoundError(f"指标不存在: id={metric_id}")
    formulas = await repo.count_formulas(db, metric_id)
    if formulas > 0:
        raise BadRequestError(f"指标被 {formulas} 条指标口径引用，无法删除")
    await repo.soft_delete(db, metric_id)
    return {"message": "deleted", "id": str(metric_id)}

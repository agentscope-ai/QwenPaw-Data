from __future__ import annotations

import aiosqlite

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.biz_domain import BizDomainCreate, BizDomainResponse, BizDomainUpdate
from semantic_config.pagination import Page, offset
from semantic_config.repositories import biz_domain_repo as repo
from semantic_config.repositories import datasource_repo as ds_repo


def _to_response(row: aiosqlite.Row) -> BizDomainResponse:
    return BizDomainResponse(
        domain_id=row["domain_id"],
        datasource_id=row["datasource_id"],
        datasource_name=row["datasource_name"],
        domain_name=row["domain_name"],
        display_name=row["display_name"],
        description=row["description"],
        aliases=row["aliases"],
    )


async def create(db: aiosqlite.Connection, payload: BizDomainCreate) -> BizDomainResponse:
    if payload.datasource_id is None:
        raise BadRequestError("datasource_id 不能为空")
    if not payload.domain_name:
        raise BadRequestError("domain_name 不能为空")
    if not payload.description:
        raise BadRequestError("description 不能为空")
    if await ds_repo.find_by_datasource_id(db, payload.datasource_id) is None:
        raise BadRequestError(f"数据源不存在: datasource_id={payload.datasource_id}")
    if await repo.exists_by_ds_and_name(db, payload.datasource_id, payload.domain_name):
        raise BadRequestError(f"业务域名称已存在: {payload.domain_name}")
    new_id = await repo.insert(
        db, payload.datasource_id, payload.domain_name, payload.display_name,
        payload.description, payload.aliases,
    )
    row = await repo.find_response_by_id(db, new_id)
    return _to_response(row)


async def update(db: aiosqlite.Connection, domain_id: int, payload: BizDomainUpdate) -> BizDomainResponse:
    existing = await repo.find_by_id(db, domain_id)
    if existing is None:
        raise NotFoundError(f"业务域不存在: id={domain_id}")
    if payload.domain_name and payload.domain_name != existing["domain_name"]:
        if await repo.exists_by_ds_and_name_excluding(
            db, existing["datasource_id"], payload.domain_name, domain_id
        ):
            raise BadRequestError(f"业务域名称已存在: {payload.domain_name}")
    await repo.update(
        db, domain_id, payload.domain_name, payload.display_name, payload.description, payload.aliases
    )
    row = await repo.find_response_by_id(db, domain_id)
    return _to_response(row)


async def get_one(db: aiosqlite.Connection, domain_id: int) -> BizDomainResponse:
    row = await repo.find_response_by_id(db, domain_id)
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db: aiosqlite.Connection,
    datasource_id: str | None,
    domain_name: str | None,
    page: int,
    size: int,
) -> Page[BizDomainResponse]:
    total = await repo.count(db, datasource_id, domain_name)
    rows = await repo.find_page(db, datasource_id, domain_name, size, offset(page, size))
    return Page(records=[_to_response(r) for r in rows], total=total, page=page, size=size)


async def delete(db: aiosqlite.Connection, domain_id: int) -> dict:
    if await repo.find_by_id(db, domain_id) is None:
        raise NotFoundError(f"业务域不存在: id={domain_id}")
    datasets = await repo.count_datasets(db, domain_id)
    metrics = await repo.count_metrics(db, domain_id)
    dimensions = await repo.count_dimensions(db, domain_id)
    if datasets or metrics or dimensions:
        raise BadRequestError(
            f"业务域下存在数据集 {datasets} 个、指标 {metrics} 个、维度 {dimensions} 个，无法删除"
        )
    await repo.soft_delete(db, domain_id)
    return {"message": "deleted", "id": str(domain_id)}

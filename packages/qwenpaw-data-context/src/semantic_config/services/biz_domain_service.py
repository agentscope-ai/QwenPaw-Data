from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.biz_domain import (
    BizDomainCreate,
    BizDomainResponse,
    BizDomainUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import biz_domain_repo as repo


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    data = {}
    for f in type(payload).model_fields:
        incoming = getattr(payload, f)
        data[f] = incoming if incoming is not None else existing.get(f)
    return type(payload)(**data)


def _to_response(row) -> BizDomainResponse:
    keys = set(row.keys())
    return BizDomainResponse(
        domain_id=row["domain_id"],
        domain_name=row["domain_name"],
        description=row["description"],
        datasource_scope=row["datasource_scope"]
        if "datasource_scope" in keys
        else None,
    )


async def create(
    db: aiosqlite.Connection, payload: BizDomainCreate, principal: Principal
) -> BizDomainResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if not payload.domain_name:
        raise BadRequestError("domain_name 不能为空")
    if not payload.description:
        raise BadRequestError("description 不能为空")
    if await repo.exists_by_name(db, payload.domain_name, tid, wid):
        raise BadRequestError(f"业务域名称已存在: {payload.domain_name}")
    new_id = await repo.insert(
        db,
        payload.domain_name,
        payload.description,
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
    )
    row = await repo.find_response_by_id(db, new_id, tid, wid)
    return _to_response(row)


async def update(
    db: aiosqlite.Connection,
    domain_id: int,
    payload: BizDomainUpdate,
    principal: Principal,
) -> BizDomainResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, domain_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"业务域不存在: id={domain_id}")
    merged = _merge(payload, existing)
    if merged.domain_name != existing["domain_name"]:
        if await repo.exists_by_name_excluding(
            db, merged.domain_name, domain_id, tid, wid
        ):
            raise BadRequestError(f"业务域名称已存在: {merged.domain_name}")
    await repo.update(
        db,
        domain_id,
        merged.domain_name,
        merged.description,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    row = await repo.find_response_by_id(db, domain_id, tid, wid)
    return _to_response(row)


async def get_one(
    db: aiosqlite.Connection, domain_id: int, principal: Principal
) -> BizDomainResponse:
    row = await repo.find_response_by_id(
        db, domain_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db: aiosqlite.Connection,
    domain_name: str | None,
    page: int,
    size: int,
    principal: Principal,
) -> Page[BizDomainResponse]:
    """业务域是工作空间级对象，不按数据源过滤（含建模工作台，SPEC 00 §15.2 ①）。"""
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, domain_name, tid, wid)
    rows = await repo.find_page(db, domain_name, size, offset(page, size), tid, wid)
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(
    db: aiosqlite.Connection, domain_id: int, principal: Principal
) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, domain_id, tid, wid) is None:
        raise NotFoundError(f"业务域不存在: id={domain_id}")
    datasets = await repo.count_datasets(db, domain_id, tid, wid)
    metrics = await repo.count_metrics(db, domain_id, tid, wid)
    dimensions = await repo.count_dimensions(db, domain_id, tid, wid)
    queries = await repo.count_verified_queries(db, domain_id, tid, wid)
    if datasets or metrics or dimensions or queries:
        raise BadRequestError(
            f"业务域下存在数据集 {datasets} 个、指标 {metrics} 个、"
            f"维度 {dimensions} 个、黄金查询 {queries} 个，无法删除"
        )
    await repo.soft_delete(
        db,
        domain_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return {"message": "deleted", "id": str(domain_id)}

from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dimension import (
    DimensionCreate,
    DimensionResponse,
    DimensionUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import biz_domain_repo
from semantic_config.repositories import dimension_repo as repo

# Keep historical priorities valid when editing existing dimensions.
_PRIORITIES = {"HIGH", "NORMAL", "MEDIUM", "LOW"}


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    data = {}
    for f in type(payload).model_fields:
        incoming = getattr(payload, f)
        data[f] = incoming if incoming is not None else existing.get(f)
    return type(payload)(**data)


def _to_response(row) -> DimensionResponse:
    return DimensionResponse(**{k: row[k] for k in row.keys()})


def _check_priority(value: str | None) -> None:
    if value is None:
        return
    if str(value).upper() not in _PRIORITIES:
        raise BadRequestError(
            "priority 仅支持 high / normal（兼容旧值 HIGH / MEDIUM / LOW）"
        )


async def create(
    db: aiosqlite.Connection, payload: DimensionCreate, principal: Principal
) -> DimensionResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if not payload.dimension_name:
        raise BadRequestError("dimension_name 不能为空")
    _check_priority(payload.priority)
    if await biz_domain_repo.find_by_id(db, payload.domain_id, tid, wid) is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    if await repo.exists_name_in_domain(
        db, payload.domain_id, payload.dimension_name, tid, wid
    ):
        raise BadRequestError(f"维度名称已存在: {payload.dimension_name}")
    new_id = await repo.insert(
        db,
        payload,
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, new_id, tid, wid))


async def update(
    db: aiosqlite.Connection,
    dim_id: int,
    payload: DimensionUpdate,
    principal: Principal,
) -> DimensionResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, dim_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"维度不存在: id={dim_id}")
    merged = _merge(payload, existing)
    _check_priority(merged.priority)
    if merged.dimension_name != existing["dimension_name"]:
        if await repo.exists_name_in_domain_excluding(
            db, existing["domain_id"], merged.dimension_name, dim_id, tid, wid
        ):
            raise BadRequestError(f"维度名称已存在: {merged.dimension_name}")
    await repo.update(
        db,
        dim_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, dim_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, dim_id: int, principal: Principal
) -> DimensionResponse:
    row = await repo.find_response_by_id(
        db, dim_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db, domain_id, dimension_name, page, size, principal: Principal, datasource_id=None
):
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(
        db, domain_id, dimension_name, tid, wid, datasource_id=datasource_id
    )
    rows = await repo.find_page(
        db,
        domain_id,
        dimension_name,
        size,
        offset(page, size),
        tid,
        wid,
        datasource_id=datasource_id,
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(db: aiosqlite.Connection, dim_id: int, principal: Principal) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, dim_id, tid, wid) is None:
        raise NotFoundError(f"维度不存在: id={dim_id}")
    bindings = await repo.count_bindings(db, dim_id, tid, wid)
    if bindings > 0:
        raise BadRequestError(f"维度被 {bindings} 条维度口径引用，无法删除")
    await repo.soft_delete(
        db,
        dim_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return {"message": "deleted", "id": str(dim_id)}

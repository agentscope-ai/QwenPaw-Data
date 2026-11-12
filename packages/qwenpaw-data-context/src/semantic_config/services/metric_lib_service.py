from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.metric_lib import MetricCreate, MetricResponse, MetricUpdate
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import biz_domain_repo
from semantic_config.repositories import metric_lib_repo as repo

_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    data = {}
    for f in type(payload).model_fields:
        incoming = getattr(payload, f)
        data[f] = incoming if incoming is not None else existing.get(f)
    return type(payload)(**data)


def _to_response(row) -> MetricResponse:
    return MetricResponse(**{k: row[k] for k in row.keys()})


def _check_priority(value: str | None) -> None:
    if value is None:
        return
    if str(value).upper() not in _PRIORITIES:
        raise BadRequestError("priority 仅支持 HIGH / MEDIUM / LOW")


async def create(
    db: aiosqlite.Connection, payload: MetricCreate, principal: Principal
) -> MetricResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if not payload.metric_name:
        raise BadRequestError("metric_name 不能为空")
    _check_priority(payload.priority)
    if await biz_domain_repo.find_by_id(db, payload.domain_id, tid, wid) is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    if await repo.exists_name_in_domain(
        db, payload.domain_id, payload.metric_name, tid, wid
    ):
        raise BadRequestError(f"指标名称已存在: {payload.metric_name}")
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
    metric_id: int,
    payload: MetricUpdate,
    principal: Principal,
) -> MetricResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, metric_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"指标不存在: id={metric_id}")
    merged = _merge(payload, existing)
    _check_priority(merged.priority)
    if merged.metric_name != existing["metric_name"]:
        if await repo.exists_name_in_domain_excluding(
            db, existing["domain_id"], merged.metric_name, metric_id, tid, wid
        ):
            raise BadRequestError(f"指标名称已存在: {merged.metric_name}")
    await repo.update(
        db,
        metric_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, metric_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, metric_id: int, principal: Principal
) -> MetricResponse:
    row = await repo.find_response_by_id(
        db, metric_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db, domain_id, metric_name, page, size, principal: Principal, datasource_id=None
):
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(
        db, domain_id, metric_name, tid, wid, datasource_id=datasource_id
    )
    rows = await repo.find_page(
        db,
        domain_id,
        metric_name,
        size,
        offset(page, size),
        tid,
        wid,
        datasource_id=datasource_id,
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(
    db: aiosqlite.Connection, metric_id: int, principal: Principal
) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, metric_id, tid, wid) is None:
        raise NotFoundError(f"指标不存在: id={metric_id}")
    calibers = await repo.count_calibers(db, metric_id, tid, wid)
    if calibers > 0:
        raise BadRequestError(f"指标被 {calibers} 条口径引用，无法删除")
    await repo.soft_delete(
        db,
        metric_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return {"message": "deleted", "id": str(metric_id)}

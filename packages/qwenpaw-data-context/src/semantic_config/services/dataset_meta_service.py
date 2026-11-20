from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dataset_meta import (
    DatasetMetaCreate,
    DatasetMetaResponse,
    DatasetMetaUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import biz_domain_repo, datasource_repo
from semantic_config.repositories import dataset_meta_repo as repo
from semantic_config.services.domain_scope import recompute_domain_scope


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    data = {}
    for f in type(payload).model_fields:
        incoming = getattr(payload, f)
        data[f] = incoming if incoming is not None else existing.get(f)
    return type(payload)(**data)


def _to_response(row) -> DatasetMetaResponse:
    return DatasetMetaResponse(**{k: row[k] for k in row.keys()})


async def create(
    db: aiosqlite.Connection, payload: DatasetMetaCreate, principal: Principal
) -> DatasetMetaResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if not payload.datasource_id:
        raise BadRequestError("datasource_id 不能为空")
    if payload.domain_id is None:
        raise BadRequestError("domain_id 不能为空")
    if not payload.dataset_name:
        raise BadRequestError("dataset_name 不能为空")
    if not payload.source:
        raise BadRequestError("source 不能为空")
    ds = await datasource_repo.find_by_datasource_id(
        db, payload.datasource_id, tid, wid
    )
    if ds is None:
        raise BadRequestError(f"数据源不存在: datasource_id={payload.datasource_id}")
    if await biz_domain_repo.find_by_id(db, payload.domain_id, tid, wid) is None:
        raise BadRequestError(f"业务域不存在: domain_id={payload.domain_id}")
    if await repo.exists_name_in_datasource(
        db, payload.datasource_id, payload.dataset_name, tid, wid
    ):
        raise BadRequestError(f"数据集名称已存在: {payload.dataset_name}")
    new_id = await repo.insert(
        db,
        payload,
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
    )
    # 数据集是"业务域 → 数据源"关系的唯一来源：入库后该域的范围随之变化。
    await recompute_domain_scope(
        db,
        payload.domain_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, new_id, tid, wid))


async def update(
    db: aiosqlite.Connection,
    dataset_id: int,
    payload: DatasetMetaUpdate,
    principal: Principal,
) -> DatasetMetaResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, dataset_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"数据集不存在: id={dataset_id}")
    merged = _merge(payload, existing)
    if merged.dataset_name != existing["dataset_name"]:
        if await repo.exists_name_in_datasource_excluding(
            db, existing["datasource_id"], merged.dataset_name, dataset_id, tid, wid
        ):
            raise BadRequestError(f"数据集名称已存在: {merged.dataset_name}")
    await repo.update(
        db,
        dataset_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, dataset_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, dataset_id: int, principal: Principal
) -> DatasetMetaResponse:
    row = await repo.find_response_by_id(
        db, dataset_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db, datasource_id, domain_id, dataset_name, page, size, principal: Principal
):
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, datasource_id, domain_id, dataset_name, tid, wid)
    rows = await repo.find_page(
        db, datasource_id, domain_id, dataset_name, size, offset(page, size), tid, wid
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(
    db: aiosqlite.Connection, dataset_id: int, principal: Principal
) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, dataset_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"数据集不存在: id={dataset_id}")
    fields = await repo.count_fields(db, dataset_id, tid, wid)
    dims = await repo.count_dimensions(db, dataset_id, tid, wid)
    calibers = await repo.count_calibers(db, dataset_id, tid, wid)
    if fields or dims or calibers:
        raise BadRequestError(
            f"数据集被引用，无法删除：字段 {fields} 个、维度口径 {dims} 个、指标口径 {calibers} 个"
        )
    await repo.soft_delete(
        db,
        dataset_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    # 软删同样改变该域的数据源范围（可能是该域在某数据源上的最后一个数据集）。
    await recompute_domain_scope(
        db,
        existing["domain_id"],
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return {"message": "deleted", "id": str(dataset_id)}

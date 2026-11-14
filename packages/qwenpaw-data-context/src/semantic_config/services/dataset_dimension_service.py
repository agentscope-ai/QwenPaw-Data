from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dataset_dimension import (
    DatasetDimensionCreate,
    DatasetDimensionResponse,
    DatasetDimensionUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import dataset_meta_repo, dimension_repo
from semantic_config.repositories import dataset_dimension_repo as repo


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    return type(payload)(
        **{
            f: (getattr(payload, f) if getattr(payload, f) is not None else existing[f])
            for f in type(payload).model_fields
        }
    )


def _to_response(row: aiosqlite.Row) -> DatasetDimensionResponse:
    return DatasetDimensionResponse(**{k: row[k] for k in row.keys()})


async def create(
    db: aiosqlite.Connection, payload: DatasetDimensionCreate, principal: Principal
) -> DatasetDimensionResponse:
    """expr 必填；冗余 datasource_id/domain_id 全部派生自父数据集（SPEC 00 §5.3）。

    维度字典已无数据源，故 datasource_id 只能取自 dataset；并强制 dataset 与
    dimension_dict 同域，跨域绑定 400（§9-Q7）——Excel 单源导出的一致性依赖此约束。
    """
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if payload.dataset_id is None:
        raise BadRequestError("dataset_id 不能为空")
    if payload.dimension_id is None:
        raise BadRequestError("dimension_id 不能为空")
    if not payload.expr:
        raise BadRequestError("expr 不能为空")
    ds = await dataset_meta_repo.find_by_id(db, payload.dataset_id, tid, wid)
    if ds is None:
        raise BadRequestError(f"数据集不存在: dataset_id={payload.dataset_id}")
    dim = await dimension_repo.find_by_id(db, payload.dimension_id, tid, wid)
    if dim is None:
        raise BadRequestError(f"维度不存在: dimension_id={payload.dimension_id}")
    if ds["domain_id"] != dim["domain_id"]:
        raise BadRequestError(
            f"数据集与维度字典不属于同一业务域，无法绑定："
            f"dataset.domain_id={ds['domain_id']}, dimension.domain_id={dim['domain_id']}"
        )
    new_id = await repo.insert(
        db,
        payload,
        domain_id=ds["domain_id"],
        datasource_id=ds["datasource_id"],
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, new_id, tid, wid))


async def update(
    db: aiosqlite.Connection,
    dd_id: int,
    payload: DatasetDimensionUpdate,
    principal: Principal,
) -> DatasetDimensionResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, dd_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"维度口径不存在: id={dd_id}")
    merged = _merge(payload, existing)
    await repo.update(
        db,
        dd_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, dd_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, dd_id: int, principal: Principal
) -> DatasetDimensionResponse:
    row = await repo.find_response_by_id(
        db, dd_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, dataset_id, dimension_name, page, size, principal: Principal):
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, dataset_id, dimension_name, tid, wid)
    rows = await repo.find_page(
        db, dataset_id, dimension_name, size, offset(page, size), tid, wid
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def list_by_dataset(
    db, dataset_id, principal: Principal
) -> list[DatasetDimensionResponse]:
    rows = await repo.find_all_by_dataset(
        db, dataset_id, principal.tenant_id, principal.active_workspace_id
    )
    return [_to_response(r) for r in rows]


async def delete(db: aiosqlite.Connection, dd_id: int, principal: Principal) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, dd_id, tid, wid) is None:
        raise NotFoundError(f"维度口径不存在: id={dd_id}")
    await repo.soft_delete(
        db,
        dd_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return {"message": "deleted", "id": str(dd_id)}


async def delete_by_dataset(
    db: aiosqlite.Connection, dataset_id: int, principal: Principal
) -> dict:
    require_admin(principal)
    await repo.soft_delete_by_dataset(
        db,
        dataset_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        updated_by=principal.user_id,
    )
    return {"message": "deleted by dataset", "dataset_id": str(dataset_id)}

from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.dataset_field import (
    DatasetFieldCreate,
    DatasetFieldResponse,
    DatasetFieldUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import dataset_meta_repo
from semantic_config.repositories import dataset_field_repo as repo


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    return type(payload)(
        **{
            f: (getattr(payload, f) if getattr(payload, f) is not None else existing[f])
            for f in type(payload).model_fields
        }
    )


def _to_response(row: aiosqlite.Row) -> DatasetFieldResponse:
    return DatasetFieldResponse(**{k: row[k] for k in row.keys()})


async def create(
    db: aiosqlite.Connection, payload: DatasetFieldCreate, principal: Principal
) -> DatasetFieldResponse:
    """v2.1（L3）：字段挂数据集；tenant/ws 继承父数据集。"""
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if payload.dataset_id is None:
        raise BadRequestError("dataset_id 不能为空")
    if not payload.field_name:
        raise BadRequestError("field_name 不能为空")
    parent = await dataset_meta_repo.find_by_id(db, payload.dataset_id, tid, wid)
    if parent is None:
        raise BadRequestError(f"数据集不存在: dataset_id={payload.dataset_id}")
    # 冗余列一律以父数据集为准（dataset 已统一为 datasource_id，SPEC 00 §5.8）。
    payload = payload.model_copy(
        update={
            "datasource_id": payload.datasource_id or parent.get("datasource_id"),
            "domain_id": payload.domain_id
            if payload.domain_id is not None
            else parent.get("domain_id"),
        }
    )
    if await repo.exists_name_in_dataset(
        db, payload.dataset_id, payload.field_name, tid, wid
    ):
        raise BadRequestError(f"字段已存在: {payload.field_name}")
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
    field_id: int,
    payload: DatasetFieldUpdate,
    principal: Principal,
) -> DatasetFieldResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, field_id, tid, wid)
    if existing is None:
        raise NotFoundError(f"字段不存在: id={field_id}")
    merged = _merge(payload, existing)
    if merged.field_name != existing["field_name"]:
        if await repo.exists_name_in_dataset_excluding(
            db,
            existing["dataset_id"],
            merged.field_name,
            field_id,
            tid,
            wid,
        ):
            raise BadRequestError(f"字段已存在: {merged.field_name}")
    await repo.update(
        db,
        field_id,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, field_id, tid, wid))


async def get_one(
    db: aiosqlite.Connection, field_id: int, principal: Principal
) -> DatasetFieldResponse:
    row = await repo.find_response_by_id(
        db, field_id, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(
    db, dataset_id, page, size, principal: Principal, datasource_id=None, domain_id=None
) -> Page[DatasetFieldResponse]:
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(
        db, dataset_id, tid, wid, datasource_id=datasource_id, domain_id=domain_id
    )
    rows = await repo.find_page(
        db,
        dataset_id,
        size,
        offset(page, size),
        tid,
        wid,
        datasource_id=datasource_id,
        domain_id=domain_id,
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def list_by_dataset(
    db, dataset_id: int, principal: Principal
) -> list[DatasetFieldResponse]:
    rows = await repo.find_all_by_dataset(
        db, dataset_id, principal.tenant_id, principal.active_workspace_id
    )
    return [_to_response(r) for r in rows]


async def delete(db: aiosqlite.Connection, field_id: int, principal: Principal) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, field_id, tid, wid) is None:
        raise NotFoundError(f"字段不存在: id={field_id}")
    await repo.soft_delete(
        db, field_id, tenant_id=tid, workspace_id=wid, updated_by=principal.user_id
    )
    return {"message": "deleted", "id": str(field_id)}

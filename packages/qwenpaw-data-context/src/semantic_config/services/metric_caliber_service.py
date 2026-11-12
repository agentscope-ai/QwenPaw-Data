from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from semantic_config.errors import BadRequestError, NotFoundError, NotFoundNoBody
from semantic_config.models.metric_caliber import (
    CaliberCreate,
    CaliberResponse,
    CaliberUpdate,
)
from semantic_config.pagination import Page, offset
from semantic_config.principal import require_admin
from semantic_config.repositories import dataset_meta_repo, metric_lib_repo
from semantic_config.repositories import metric_caliber_repo as repo


def _merge(payload, existing):
    """部分更新：None 字段回填现存值，避免把 NOT NULL 列清成 NULL。"""
    return type(payload)(
        **{
            f: (getattr(payload, f) if getattr(payload, f) is not None else existing[f])
            for f in type(payload).model_fields
        }
    )


def _to_response(row: aiosqlite.Row) -> CaliberResponse:
    return CaliberResponse(**{k: row[k] for k in row.keys()})


async def create(
    db: aiosqlite.Connection, payload: CaliberCreate, principal: Principal
) -> CaliberResponse:
    """expr+time_window 必填；冗余 datasource_id/domain_id 派生自父数据集（SPEC 00 §5.4）。

    指标字典已无数据源，故 datasource_id 改由 dataset 提供；并强制 dataset 与
    metric_dict 同域，跨域绑定 400（§9-Q7）。
    """
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if payload.metric_id is None:
        raise BadRequestError("metric_id 不能为空")
    if payload.dataset_id is None:
        raise BadRequestError("dataset_id 不能为空")
    if not payload.expr:
        raise BadRequestError("expr 不能为空")
    if not payload.time_window:
        raise BadRequestError("time_window 不能为空")
    metric = await metric_lib_repo.find_by_id(db, payload.metric_id, tid, wid)
    if metric is None:
        raise BadRequestError(f"指标不存在: metric_id={payload.metric_id}")
    ds = await dataset_meta_repo.find_by_id(db, payload.dataset_id, tid, wid)
    if ds is None:
        raise BadRequestError(f"数据集不存在: dataset_id={payload.dataset_id}")
    if ds["domain_id"] != metric["domain_id"]:
        raise BadRequestError(
            f"数据集与指标字典不属于同一业务域，无法绑定："
            f"dataset.domain_id={ds['domain_id']}, metric.domain_id={metric['domain_id']}"
        )
    if await repo.exists_key(
        db,
        payload.metric_id,
        payload.dataset_id,
        payload.time_window,
        tid,
        wid,
    ):
        raise BadRequestError(
            f"口径已存在: metric_id={payload.metric_id}, time_window={payload.time_window}"
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
    db: aiosqlite.Connection, cid: int, payload: CaliberUpdate, principal: Principal
) -> CaliberResponse:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await repo.find_by_id(db, cid, tid, wid)
    if existing is None:
        raise NotFoundError(f"指标口径不存在: id={cid}")
    merged = _merge(payload, existing)
    if merged.time_window != existing["time_window"]:
        if await repo.exists_key_excluding(
            db,
            existing["metric_id"],
            existing["dataset_id"],
            merged.time_window,
            cid,
            tid,
            wid,
        ):
            raise BadRequestError(
                f"口径已存在: metric_id={existing['metric_id']}, time_window={merged.time_window}"
            )
    await repo.update(
        db,
        cid,
        merged,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    return _to_response(await repo.find_response_by_id(db, cid, tid, wid))


async def get_one(
    db: aiosqlite.Connection, cid: int, principal: Principal
) -> CaliberResponse:
    row = await repo.find_response_by_id(
        db, cid, principal.tenant_id, principal.active_workspace_id
    )
    if row is None:
        raise NotFoundNoBody()
    return _to_response(row)


async def list_page(db, metric_id, dataset_id, page, size, principal: Principal):
    tid, wid = principal.tenant_id, principal.active_workspace_id
    total = await repo.count(db, metric_id, dataset_id, tid, wid)
    rows = await repo.find_page(
        db, metric_id, dataset_id, size, offset(page, size), tid, wid
    )
    return Page(
        records=[_to_response(r) for r in rows], total=total, page=page, size=size
    )


async def delete(db: aiosqlite.Connection, cid: int, principal: Principal) -> dict:
    require_admin(principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if await repo.find_by_id(db, cid, tid, wid) is None:
        raise NotFoundError(f"指标口径不存在: id={cid}")
    await repo.soft_delete(
        db, cid, tenant_id=tid, workspace_id=wid, updated_by=principal.user_id
    )
    return {"message": "deleted", "id": str(cid)}


async def delete_by_metric(
    db: aiosqlite.Connection, metric_id: int, principal: Principal
) -> dict:
    require_admin(principal)
    n = await repo.soft_delete_by_metric(
        db,
        metric_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        updated_by=principal.user_id,
    )
    return {"message": "deleted by metric", "metric_id": str(metric_id), "deleted": n}

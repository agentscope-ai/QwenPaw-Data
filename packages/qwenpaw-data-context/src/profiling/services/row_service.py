"""staging 行级 CRUD + 批量确定/跳过/删除（SPEC §7.2 ⑦~⑩、§7.3）。

- 仅候选态阶段（has_rows）支持批量 confirm/skip/delete；仅 can_add 阶段支持人工新增。
- 人工新增强制 source_type=HUMAN；编辑只接受该阶段 editable 白名单列（member_tables 等只读列忽略）。
- 写操作 require_admin（02-SPEC §13.5）；一切按 datasource + tenant/ws 行级隔离。
"""

from __future__ import annotations

import aiosqlite
from qwenpaw_data_auth import Principal

from profiling.models.probe import ROW_CONFIRMED, ROW_SKIPPED, SOURCE_HUMAN
from profiling.repositories import staging_repo
from profiling.stages import StageDef
from semantic_config.errors import BadRequestError, NotFoundNoBody
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo as ds_repo


async def _require_ds(db, datasource_id, principal) -> None:
    ds = await ds_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise NotFoundNoBody()


def _filter_editable(sdef: StageDef, body: dict) -> dict:
    return {k: v for k, v in body.items() if k in sdef.editable}


async def add_row(
    db: aiosqlite.Connection,
    sdef: StageDef,
    datasource_id: str,
    body: dict,
    principal: Principal,
) -> dict:
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    if not sdef.can_add:
        raise BadRequestError("该阶段不支持新增行")
    values = _filter_editable(sdef, body)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    new_id = await staging_repo.insert_row(
        db,
        sdef.table,
        values,
        datasource_id=datasource_id,
        tenant_id=tid,
        workspace_id=wid,
        created_by=principal.user_id,
        task_id=None,
        source_type=SOURCE_HUMAN,
    )
    row = await staging_repo.find_by_id(db, sdef.table, new_id, datasource_id, tid, wid)
    return staging_repo.to_public_dict(row)


async def update_row(
    db: aiosqlite.Connection,
    sdef: StageDef,
    datasource_id: str,
    row_id: int,
    body: dict,
    principal: Principal,
) -> dict:
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    tid, wid = principal.tenant_id, principal.active_workspace_id
    existing = await staging_repo.find_by_id(
        db, sdef.table, row_id, datasource_id, tid, wid
    )
    if existing is None:
        raise NotFoundNoBody()
    values = _filter_editable(sdef, body)
    await staging_repo.update_row(
        db,
        sdef.table,
        row_id,
        values,
        datasource_id=datasource_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    row = await staging_repo.find_by_id(db, sdef.table, row_id, datasource_id, tid, wid)
    return staging_repo.to_public_dict(row)


async def delete_row(
    db: aiosqlite.Connection,
    sdef: StageDef,
    datasource_id: str,
    row_id: int,
    principal: Principal,
) -> dict:
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    n = await staging_repo.soft_delete(
        db,
        sdef.table,
        row_id,
        datasource_id=datasource_id,
        tenant_id=principal.tenant_id,
        workspace_id=principal.active_workspace_id,
        updated_by=principal.user_id,
    )
    return {"deleted": n}


async def _batch(
    db,
    sdef: StageDef,
    datasource_id,
    row_ids,
    principal,
    *,
    status: str | None,
    delete: bool,
) -> int:
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    if not sdef.has_rows:
        raise BadRequestError("该阶段不支持批量确定/跳过/删除")
    tid, wid = principal.tenant_id, principal.active_workspace_id
    if delete:
        return await staging_repo.batch_soft_delete(
            db,
            sdef.table,
            row_ids,
            datasource_id=datasource_id,
            tenant_id=tid,
            workspace_id=wid,
            updated_by=principal.user_id,
        )
    return await staging_repo.batch_set_status(
        db,
        sdef.table,
        row_ids,
        status,
        datasource_id=datasource_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )


async def batch_confirm(db, sdef, datasource_id, row_ids, principal) -> dict:
    n = await _batch(
        db, sdef, datasource_id, row_ids, principal, status=ROW_CONFIRMED, delete=False
    )
    return {"confirmed": n}


async def batch_skip(db, sdef, datasource_id, row_ids, principal) -> dict:
    n = await _batch(
        db, sdef, datasource_id, row_ids, principal, status=ROW_SKIPPED, delete=False
    )
    return {"skipped": n}


async def batch_delete(db, sdef, datasource_id, row_ids, principal) -> dict:
    n = await _batch(
        db, sdef, datasource_id, row_ids, principal, status=None, delete=True
    )
    return {"deleted": n}

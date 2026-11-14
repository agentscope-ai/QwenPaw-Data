"""阶段级 Excel 上传（覆盖暂存）+ §7.4 整库一键导入（覆盖正式表并全部置已完成）。

- upload：完全覆盖该阶段 staging（先软删旧、再灌新），不 promote，上传后进入 REVIEWING。
  表头用字段名；上游引用用 id 列（domain_id/dataset_id）。仅 .xlsx、≤50MB。
- import_all：复用上期 excel_import_service 写正式实体表，成功后把 6 阶段状态全部置 COMPLETED，
  并触发一次图同步（失败不回滚正式表）。
写操作 require_admin。
"""

from __future__ import annotations

import io
import json
from typing import Any

import aiosqlite
from qwenpaw_data_auth import Principal

from profiling.models.probe import (
    ROW_PENDING,
    SOURCE_HUMAN,
    STAGES,
    STATUS_COMPLETED,
    STATUS_REVIEWING,
)
from context_manager.heartbeat import lock_scope
from profiling.repositories import probe_stage_repo as stage_repo
from profiling.repositories import probe_task_repo as task_repo
from profiling.repositories import staging_repo
from profiling.stages import StageDef
from semantic_config.errors import BadRequestError, NotFoundNoBody
from semantic_config.principal import require_admin
from semantic_config.repositories import datasource_repo as ds_repo
from semantic_config.services import excel_import_service

_MAX_BYTES = 50 * 1024 * 1024
_INT_COLS = frozenset({"domain_id", "dataset_id"})
# v2.1：布尔装饰列（is_visible/is_polaris 等）全部退役，导出面暂无布尔列
_BOOL_COLS: frozenset[str] = frozenset()


def _validate_file(filename: str, content: bytes) -> None:
    if not content:
        raise BadRequestError("上传文件为空")
    if not filename.lower().endswith(".xlsx"):
        raise BadRequestError("仅支持 .xlsx 文件")
    if len(content) > _MAX_BYTES:
        raise BadRequestError("文件过大（≤50MB）")


def _coerce(col: str, val):
    if val is None or val == "":
        return None
    if col in _INT_COLS:
        try:
            return int(val)
        except (ValueError, TypeError):
            return None
    if col in _BOOL_COLS:
        s = str(val).strip().lower()
        return 1 if s in ("1", "true", "yes", "y", "是") else 0
    return str(val).strip() if not isinstance(val, str) else val.strip()


def _read_rows(content: bytes) -> tuple[list[str], list[dict]]:
    import openpyxl

    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise BadRequestError(f"解析 xlsx 失败：{exc}") from exc
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    try:
        header_row = next(it)
    except StopIteration:
        return [], []
    headers = [str(h).strip() if h is not None else "" for h in header_row]
    rows: list[dict] = []
    for raw in it:
        if raw is None or all(c is None or c == "" for c in raw):
            continue
        rows.append({headers[i]: raw[i] for i in range(min(len(headers), len(raw)))})
    return headers, rows


async def _require_ds(db, datasource_id, principal):
    ds = await ds_repo.find_by_datasource_id(
        db, datasource_id, principal.tenant_id, principal.active_workspace_id
    )
    if ds is None:
        raise NotFoundNoBody()
    return ds


async def upload_stage(
    db: aiosqlite.Connection,
    sdef: StageDef,
    datasource_id: str,
    filename: str,
    content: bytes,
    principal: Principal,
) -> dict:
    require_admin(principal)
    await _require_ds(db, datasource_id, principal)
    _validate_file(filename, content)
    _, rows = _read_rows(content)

    tid, wid = principal.tenant_id, principal.active_workspace_id
    await lock_scope(db, f"probe:{tid}:{wid}:{datasource_id}:{sdef.stage}")
    if await task_repo.has_fresh_running(
        db,
        datasource_id=datasource_id,
        stage=sdef.stage,
        tenant_id=tid,
        workspace_id=wid,
    ):
        raise BadRequestError("该阶段已有探查任务运行中")
    stage_row = await stage_repo.find_one(db, datasource_id, sdef.stage, tid, wid)
    seen_status = stage_row["status"] if stage_row is not None else None
    allowed = set(sdef.excel_columns)

    # 覆盖：先软删旧 staging，再灌新
    await staging_repo.soft_delete_all(
        db,
        sdef.table,
        datasource_id,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
    )
    imported = 0
    for r in rows:
        values = {}
        for col in allowed:
            if col == "probe_status":
                continue
            if col in r:
                values[col] = _coerce(col, r[col])
        probe_status = r.get("probe_status") or ROW_PENDING
        await staging_repo.insert_row(
            db,
            sdef.table,
            values,
            datasource_id=datasource_id,
            tenant_id=tid,
            workspace_id=wid,
            created_by=principal.user_id,
            task_id=None,
            source_type=SOURCE_HUMAN,
            probe_status=str(probe_status).strip() or ROW_PENDING,
        )
        imported += 1
    await stage_repo.update_status(
        db,
        datasource_id,
        sdef.stage,
        STATUS_REVIEWING,
        tenant_id=tid,
        workspace_id=wid,
        updated_by=principal.user_id,
        expected_status=seen_status,
    )
    return {"success": True, "imported": imported}


def _excel_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return value


async def export_stage(
    db: aiosqlite.Connection,
    sdef: StageDef,
    datasource_id: str,
    principal: Principal,
) -> bytes:
    """导出当前阶段全部有效 staging 结果，列与上传覆盖保持一致。"""
    await _require_ds(db, datasource_id, principal)
    rows = await staging_repo.list_all(
        db,
        sdef.table,
        datasource_id,
        principal.tenant_id,
        principal.active_workspace_id,
    )

    import openpyxl

    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sdef.path[:31]
    worksheet.append(list(sdef.excel_columns))
    for row in rows:
        public_row = staging_repo.to_public_dict(row)
        worksheet.append(
            [_excel_value(public_row.get(column)) for column in sdef.excel_columns]
        )

    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


async def import_all(
    db: aiosqlite.Connection,
    datasource_id: str,
    filename: str,
    content: bytes,
    principal: Principal,
    *,
    runtime: Any = None,
) -> dict:
    """§7.4：整库覆盖正式实体 + 6 阶段全部置 COMPLETED，并触发一次图同步。"""
    require_admin(principal)
    ds = await _require_ds(db, datasource_id, principal)
    result = await excel_import_service.import_excel(
        db, filename, content, principal, datasource_id=datasource_id
    )
    if not result.get("success"):
        return result
    tid, wid = principal.tenant_id, principal.active_workspace_id
    await stage_repo.ensure_stages(
        db,
        datasource_id,
        tenant_id=ds["tenant_id"],
        workspace_id=ds["workspace_id"],
        created_by=principal.user_id,
    )
    for stage in STAGES:
        await lock_scope(db, f"probe:{tid}:{wid}:{datasource_id}:{stage}")
        if await task_repo.has_fresh_running(
            db,
            datasource_id=datasource_id,
            stage=stage,
            tenant_id=tid,
            workspace_id=wid,
        ):
            raise BadRequestError("该阶段已有探查任务运行中")
        row = await stage_repo.find_one(db, datasource_id, stage, tid, wid)
        await stage_repo.update_status(
            db,
            datasource_id,
            stage,
            STATUS_COMPLETED,
            tenant_id=tid,
            workspace_id=wid,
            updated_by=principal.user_id,
            expected_status=row["status"] if row is not None else None,
        )
    from profiling.services import stage_service

    result["graph_sync"] = await stage_service._sync_if_all_terminal(
        db, datasource_id, principal, runtime
    )
    return result

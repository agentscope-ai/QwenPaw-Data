"""语义层导出 + 增量回灌 REST 路由。

- GET  /api/v1/semantic/export?datasource_id=<ds>      导出单源 xlsx（8 sheet + _meta）
- POST /api/v1/semantic/import                           上传 xlsx → 增量 diff → 直接应用
- POST /api/v1/semantic/import/preview                   （保留）上传 xlsx → dry-run diff
- POST /api/v1/semantic/import/confirm                   （保留）plan_id → 应用
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse

from datapaw.context.uploads import save_upload_to_temp
from datapaw.context.job_store import get_job_store

from ..graph.semantic_diff import DiffResult, apply_diff, compute_diff
from ..graph.semantic_template_io import (
    SHEET_ORDER,
    export_to_workbook,
    parse_import_workbook,
    workbook_to_semantic_payload,
)
from ..utils import neo4j_session

log = logging.getLogger("api.semantic_io")

router = APIRouter(prefix="/api/v1/semantic", tags=["semantic-io"])

# 持久化 plan store（SQLite WAL，多 worker 共享，TTL 10 min）
_PLAN_TTL = 600.0
_PLAN_NAMESPACE = "semantic-import-plan"


def _driver(request: Request):
    return request.app.state.driver


def _evict_expired() -> None:
    get_job_store().delete_expired()


def _ser_change(c):
    out = {"type": c.type, "key": c.key, "op": c.op}
    if c.fields:
        out["fields"] = c.fields
    if c.reason:
        out["reason"] = c.reason
    return out


def _post_apply_rebuild(driver) -> list[str]:
    """应用 diff 后重建 embedding + caliber，返回警告列表。"""
    warnings: list[str] = []
    try:
        from ..graph.embeddings import index_embeddings
        index_embeddings(driver, scope="all")
    except Exception as exc:
        warnings.append(f"embedding reindex failed (non-fatal): {exc}")
        log.warning(warnings[-1])
    try:
        from ..graph.semantic_derive import derive_calibers_from_formulas
        derive_calibers_from_formulas(driver)
    except Exception as exc:
        warnings.append(f"caliber rederive failed (non-fatal): {exc}")
        log.warning(warnings[-1])
    return warnings


# ---------------------------------------------------------------------- #
# GET /export
# ---------------------------------------------------------------------- #

@router.get("/export")
def export_semantic(
    request: Request,
    datasource_id: str = Query(..., description="数据源 id，如 test_db"),
):
    """导出单数据源的语义层为 xlsx（8 sheet + 隐藏 _meta）。"""
    driver = _driver(request)
    ds = (datasource_id or "").strip()
    if not ds:
        raise HTTPException(400, "datasource_id is required")
    try:
        wb, baseline = export_to_workbook(driver, ds)
    except Exception as exc:
        log.exception("export failed: %s", exc)
        raise HTTPException(500, detail=f"export failed: {exc}")

    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    fname = f"semantic_export_{ds}_{baseline.export_id}.xlsx"
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Content-Length": str(len(data)),
        },
    )


# ---------------------------------------------------------------------- #
# POST /import  —— 一步到位：上传 xlsx → diff → 直接应用
# ---------------------------------------------------------------------- #

@router.post("/import")
def import_semantic(
    request: Request,
    file: UploadFile = File(..., description="编辑后的 xlsx"),
):
    """上传 xlsx → 计算增量 diff → 直接应用变更 → 返回结果。

    跳过 preview/confirm 两步流程，适合信任来源的自动化场景。
    冲突项（被引用节点的删除）仍会跳过，不会静默丢数据。
    """
    driver = _driver(request)

    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    tmp = save_upload_to_temp(file, suffix=suffix)
    try:
        try:
            parsed = parse_import_workbook(tmp)
        except Exception as exc:
            log.exception("parse import xlsx failed: %s", exc)
            raise HTTPException(400, detail=f"parse failed: {exc}")

        if not parsed.datasource_id:
            raise HTTPException(400, "datasource_id missing in uploaded xlsx")

        try:
            diff: DiffResult = compute_diff(driver, parsed)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        except Exception as exc:
            log.exception("compute_diff failed: %s", exc)
            raise HTTPException(500, detail=f"diff failed: {exc}")

        if not diff.changes:
            return {
                "status": "no_change",
                "datasource_id": diff.datasource_id,
                "summary": diff.summary,
                "changes": [],
                "warnings": [],
            }

        try:
            applied = apply_diff(driver, diff)
        except Exception as exc:
            log.exception("apply_diff failed: %s", exc)
            raise HTTPException(500, detail=f"apply failed: {exc}")

        warnings = _post_apply_rebuild(driver)

        return {
            "status": "applied",
            "datasource_id": diff.datasource_id,
            "summary": diff.summary,
            "applied": applied,
            "changes": [_ser_change(c) for c in diff.changes],
            "warnings": warnings,
        }
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------- #
# POST /import/preview  （保留，可选）
# ---------------------------------------------------------------------- #

@router.post("/import/preview")
def import_preview(
    request: Request,
    file: UploadFile = File(..., description="编辑后的 xlsx"),
):
    """上传 xlsx → dry-run 三方向 diff → 返回 plan_id + changes 预览。不写图。"""
    _evict_expired()
    driver = _driver(request)

    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    tmp = save_upload_to_temp(file, suffix=suffix)
    try:
        try:
            parsed = parse_import_workbook(tmp)
        except Exception as exc:
            log.exception("parse import xlsx failed: %s", exc)
            raise HTTPException(400, detail=f"parse failed: {exc}")

        if not parsed.datasource_id:
            raise HTTPException(400, "datasource_id missing in uploaded xlsx")

        try:
            diff: DiffResult = compute_diff(driver, parsed)
        except ValueError as exc:
            raise HTTPException(400, detail=str(exc))
        except Exception as exc:
            log.exception("compute_diff failed: %s", exc)
            raise HTTPException(500, detail=f"diff failed: {exc}")

        get_job_store().put(
            _PLAN_NAMESPACE,
            diff.plan_id,
            status="ready",
            ttl_seconds=_PLAN_TTL,
            payload={"diff": diff.to_dict()},
        )

        return {
            "plan_id": diff.plan_id,
            "graph_digest": diff.graph_digest,
            "datasource_id": diff.datasource_id,
            "summary": diff.summary,
            "changes": [_ser_change(c) for c in diff.changes],
        }
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------- #
# POST /import/confirm  （保留，可选）
# ---------------------------------------------------------------------- #

@router.post("/import/confirm")
def import_confirm(
    request: Request,
    plan_id: str = Query(..., description="preview 返回的 plan_id"),
):
    """按 plan_id 应用变更计划（带漂移守卫）。"""
    _evict_expired()
    driver = _driver(request)

    plan = get_job_store().get(_PLAN_NAMESPACE, plan_id)
    if not plan:
        raise HTTPException(410, detail="plan not found or expired, please re-run preview")
    if plan.status == "consumed":
        return {"plan_id": plan_id, "status": "no-op", "detail": "plan already consumed"}
    if not get_job_store().transition(
        _PLAN_NAMESPACE,
        plan_id,
        expected={"ready"},
        status="running",
    ):
        latest = get_job_store().get(_PLAN_NAMESPACE, plan_id)
        if latest and latest.status == "consumed":
            return {"plan_id": plan_id, "status": "no-op", "detail": "plan already consumed"}
        raise HTTPException(409, detail="plan is already being applied")

    diff = DiffResult.from_dict(plan.payload["diff"])
    expected_digest = diff.graph_digest

    from ..graph.semantic_template_io import build_bundle_from_graph
    try:
        cur_bundle = build_bundle_from_graph(driver, diff.datasource_id)
        cur_digest = cur_bundle["meta"].graph_digest
    except Exception as exc:
        get_job_store().transition(
            _PLAN_NAMESPACE,
            plan_id,
            expected={"running"},
            status="failed",
            error=str(exc),
        )
        log.exception("recompute digest failed: %s", exc)
        raise HTTPException(500, detail=f"digest recompute failed: {exc}")

    if cur_digest != expected_digest:
        get_job_store().delete(_PLAN_NAMESPACE, plan_id)
        raise HTTPException(
            409,
            detail=(
                "graph drifted since preview: expected "
                f"{expected_digest}, got {cur_digest}. Re-run preview."
            ),
        )

    try:
        applied = apply_diff(driver, diff)
    except Exception as exc:
        get_job_store().transition(
            _PLAN_NAMESPACE,
            plan_id,
            expected={"running"},
            status="failed",
            error=str(exc),
        )
        log.exception("apply_diff failed: %s", exc)
        raise HTTPException(500, detail=f"apply failed: {exc}")

    warnings = _post_apply_rebuild(driver)

    if not get_job_store().transition(
        _PLAN_NAMESPACE,
        plan_id,
        expected={"running"},
        status="consumed",
    ):
        raise HTTPException(409, detail="plan state changed while applying")
    return {
        "plan_id": plan_id,
        "status": "applied",
        "applied": applied,
        "datasource_id": diff.datasource_id,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------- #
# POST /import/from-excel  —— xlsx → SemanticPayload JSON（预览 / 下载）
# ---------------------------------------------------------------------- #

@router.post("/import/from-excel")
def import_from_excel_convert(
    file: UploadFile = File(..., description="export 导出的 xlsx 文件"),
    target_datasource_id: str = Query("", description="目标 datasource_id；非空时一键替换 xlsx 中的 datasource_id"),
):
    """上传导出的 xlsx → 转换为 SemanticImportRequest 兼容的 JSON。

    不写图，纯转换。返回的 JSON 可直接 POST 到 ``/api/v1/semantic/import``。

    当 ``target_datasource_id`` 非空时，xlsx 中所有 ``datasource_id``
    会被替换为该值，实现「导出 A 环境 → 导入 B 环境」的一键迁移。
    """
    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    tmp = save_upload_to_temp(file, suffix=suffix)
    try:
        try:
            parsed = parse_import_workbook(tmp)
        except Exception as exc:
            log.exception("parse xlsx failed: %s", exc)
            raise HTTPException(400, detail=f"parse failed: {exc}")

        if not parsed.datasource_id:
            raise HTTPException(400, "datasource_id missing in uploaded xlsx")

        payload = workbook_to_semantic_payload(
            parsed,
            target_datasource_id=target_datasource_id,
        )
        return payload
    finally:
        tmp.unlink(missing_ok=True)


# ---------------------------------------------------------------------- #
# POST /import/from-excel/apply  —— xlsx → 转换 + 直接写入语义层
# ---------------------------------------------------------------------- #

@router.post("/import/from-excel/apply")
def import_from_excel_apply(
    request: Request,
    file: UploadFile = File(..., description="export 导出的 xlsx 文件"),
    target_datasource_id: str = Query("", description="目标 datasource_id；非空时一键替换"),
    drop_semantic_first: bool = Query(False, description="写入前是否先清空目标 datasource 下的语义节点"),
):
    """上传导出的 xlsx → 转换为 SemanticPayload → 直接写入语义层。

    等效于先调 ``/import/from-excel`` 得到 JSON，再 POST 到 ``/api/v1/semantic/import``。
    ``target_datasource_id`` 支持一键替换 datasource。
    """
    from ..contracts.import_models import SemanticImportRequest, SemanticPayload

    suffix = Path(file.filename or "upload.xlsx").suffix or ".xlsx"
    tmp = save_upload_to_temp(file, suffix=suffix)
    try:
        try:
            parsed = parse_import_workbook(tmp)
        except Exception as exc:
            log.exception("parse xlsx failed: %s", exc)
            raise HTTPException(400, detail=f"parse failed: {exc}")

        if not parsed.datasource_id:
            raise HTTPException(400, "datasource_id missing in uploaded xlsx")

        payload = workbook_to_semantic_payload(
            parsed,
            target_datasource_id=target_datasource_id,
        )
    finally:
        tmp.unlink(missing_ok=True)

    sem_req = SemanticImportRequest(
        datasource_id=payload["datasource_id"],
        semantic=SemanticPayload(**payload["semantic"]),
        drop_semantic_first=drop_semantic_first,
    )

    from ..graph.semantic_import_service import run_semantic_import_sync

    return run_semantic_import_sync(_driver(request), sem_req)


__all__ = ["router"]

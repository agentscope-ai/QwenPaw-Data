"""Import 编排器：把 ``ImportRequest`` 转化为物理层 + schema_auto 构建流程。

核心逻辑：
1. 根据 ``source.type`` 获取 Adapter → 抽取 ``PhysicalManifest``
2. 调用 ``ingest_from_manifest`` 写入物理层
3. 运行 JOINS_ON 推断
4. 运行语义层 provider（默认 ``schema_auto``）
5. 收集统计 + 错误 → ``ImportResult``

语义字典导入请走 ``semantic_import_service``（``POST /api/v1/semantic/import``）。
"""
from __future__ import annotations

import time
import traceback
import uuid
from pathlib import Path
from typing import Optional

from neo4j import Driver

from ..contracts.import_models import (
    ImportErrorItem,
    ImportErrorLevel,
    ImportRequest,
    ImportResult,
    ImportStats,
    ImportStatus,
    ManifestSummary,
)
from ..secrets.redact import _redact_str
from ..utils import get_logger
from . import schema_init
from .adapters import get_adapter
from .joins import write_join_inference
from .physical import ingest_from_manifest
from .profile import get_profile
from .semantic_pipeline import SemanticStageInput, run_semantic_stage
from .stats import count_semantic_nodes

log = get_logger("graph.import_runner")


def run_import(
    request: ImportRequest,
    driver: Driver,
    *,
    task_id: str | None = None,
) -> ImportResult:
    """执行一次完整的 import 构建，返回 ``ImportResult``。"""
    task_id = task_id or uuid.uuid4().hex[:12]
    t0 = time.monotonic()
    errors: list[ImportErrorItem] = []

    log.info("import [%s] start: source=%s ds=%s", task_id, request.source.connection.type, request.datasource_id)

    # --- 1. 选择 Adapter 并抽取 manifest ---
    try:
        adapter = get_adapter(request.source, request.datasource_id)
        manifest = adapter.extract_metadata(request.source.schemas)
        log.info(
            "import [%s] manifest: %d tables, %d columns, %d fks",
            task_id, len(manifest.tables), len(manifest.columns), len(manifest.fks),
        )
    except Exception as exc:
        log.error("import [%s] adapter failed: %s", task_id, exc)
        errors.append(ImportErrorItem(
            level=ImportErrorLevel.fatal,
            message=_redact_str(f"Adapter extraction failed: {exc}"),
            context=_redact_str(traceback.format_exc())[:2000],
        ))
        return ImportResult(
            task_id=task_id,
            status=ImportStatus.failed,
            errors=errors,
            elapsed_seconds=time.monotonic() - t0,
        )

    credential_ref: Optional[str] = request.credential_ref
    profile = get_profile("generic")
    opts = request.options
    db_id = manifest.db_id
    sch = manifest.schema or "public"

    # --- 2. Init schema + 写入物理层 ---
    if opts.dry_run:
        log.info("import [%s] dry_run=True — skipping Neo4j writes", task_id)
        elapsed = time.monotonic() - t0
        return ImportResult(
            task_id=task_id,
            status=ImportStatus.success,
            errors=errors,
            stats=ImportStats(
                tables=len(manifest.tables),
                columns=len(manifest.columns),
                fks=len(manifest.fks),
            ),
            manifest_summary=ManifestSummary(
                db_id=db_id,
                schema=sch,
                source_type=request.source.connection.type,
                table_names=manifest.table_names,
            ),
            credential_ref=credential_ref,
            elapsed_seconds=round(elapsed, 2),
        )

    try:
        if opts.drop_topology_first:
            schema_init.drop_topology(driver)
        schema_init.init_all(driver)

        db_id, sch = ingest_from_manifest(driver, manifest=manifest, profile=profile)
        log.info("import [%s] physical layer done: db=%s schema=%s", task_id, db_id, sch)
    except Exception as exc:
        log.error("import [%s] physical write failed: %s", task_id, exc)
        errors.append(ImportErrorItem(
            level=ImportErrorLevel.fatal,
            message=_redact_str(f"Physical layer write failed: {exc}"),
            context=_redact_str(traceback.format_exc())[:2000],
        ))
        return ImportResult(
            task_id=task_id,
            status=ImportStatus.failed,
            errors=errors,
            stats=ImportStats(
                tables=len(manifest.tables),
                columns=len(manifest.columns),
                fks=len(manifest.fks),
            ),
            manifest_summary=ManifestSummary(
                db_id=manifest.db_id,
                schema=manifest.schema,
                source_type=request.source.connection.type,
                table_names=manifest.table_names,
            ),
            credential_ref=credential_ref,
            elapsed_seconds=time.monotonic() - t0,
        )

    # --- 3. JOINS_ON 推断 ---
    if opts.do_join_inference:
        try:
            write_join_inference(driver, db_id=db_id, schema=sch, profile=profile)
            log.info("import [%s] JOINS_ON inference done", task_id)
        except Exception as exc:
            errors.append(ImportErrorItem(
                level=ImportErrorLevel.degrade,
                message=_redact_str(f"JOIN inference failed: {exc}"),
            ))

    # --- 4. 语义层（默认 schema_auto）---
    if opts.semantic_providers:
        providers = ",".join(opts.semantic_providers)
        try:
            inp = SemanticStageInput(
                driver=driver,
                db_id=db_id,
                schema=sch,
                metrics_dict_path=Path("/dev/null"),
                profile=profile,
                datasource_name=request.datasource_name,
                datasource_id=request.datasource_id,
            )
            run_semantic_stage(providers, inp)
            log.info("import [%s] semantic layer done (providers=%s)", task_id, providers)
        except Exception as exc:
            errors.append(ImportErrorItem(
                level=ImportErrorLevel.degrade,
                message=_redact_str(f"Semantic layer failed: {exc}"),
            ))

    # --- 5. 收集结果 ---
    elapsed = time.monotonic() - t0
    has_fatal = any(e.level == ImportErrorLevel.fatal for e in errors)
    has_degrade = any(e.level == ImportErrorLevel.degrade for e in errors)

    status = ImportStatus.failed if has_fatal else (
        ImportStatus.degraded if has_degrade else ImportStatus.success
    )

    result = ImportResult(
        task_id=task_id,
        status=status,
        errors=errors,
        stats=ImportStats(
            tables=len(manifest.tables),
            columns=len(manifest.columns),
            fks=len(manifest.fks),
            semantic_nodes=count_semantic_nodes(driver),
        ),
        manifest_summary=ManifestSummary(
            db_id=db_id,
            schema=sch,
            source_type=request.source.connection.type,
            table_names=manifest.table_names,
        ),
        credential_ref=credential_ref,
        elapsed_seconds=round(elapsed, 2),
    )
    log.info("import [%s] done: status=%s elapsed=%.1fs", task_id, status.value, elapsed)
    return result

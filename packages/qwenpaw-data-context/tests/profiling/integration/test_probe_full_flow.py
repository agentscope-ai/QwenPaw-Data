"""批次 2~5：探查全链路端到端（StubProvider）+ 路由注册校验。

用真实文件 SQLite（后台 _run_probe 走独立连接，需与请求连接同库文件），跑通：
提交异步探查 → emit 落 staging → 批量确定 → 完成 promote，7 阶段串行至总状态 COMPLETED，
校验正式实体表（domain/dataset/dimension_dict/dataset_dimension/metric_dict/metric_formula_lib）落库。
另用 openapi 校验 7 阶段动作 + 行 CRUD + 上传 + 整库导入路由已注册。
"""

from __future__ import annotations

import asyncio
import tempfile

import pytest
from pathlib import Path

from qwenpaw_data_auth import Principal
from fastapi import FastAPI
from sqlalchemy import text

from profiling.models.probe import (
    OVERALL_COMPLETED,
    ROW_CONFIRMED,
    ROW_PENDING,
    ROW_SKIPPED,
)
from profiling.repositories import probe_stage_repo, probe_task_repo, staging_repo
from profiling.routers import probe_router
from profiling.services import orchestrator, row_service, stage_service, upload_service
from profiling.stages import STAGE_DEFS, def_by_stage
from profiling.stub_provider import StubProvider
from semantic_config.config import get_settings
from semantic_config.db import init_db
from profiling.db import init_probe_db
from semantic_config.repositories import datasource_repo as ds_repo
from semantic_config.integration import mount_semantic_config

_STAGE_ORDER = [
    "BIZ_DOMAIN",
    "DATASET",
    "DATASET_FIELDS",
    "DIMENSION_CALIBER",
    "DIMENSION_MANAGE",
    "METRIC_CALIBER",
    "METRIC_MANAGE",
]


def _principal(user="u1", ws="w1", tenant="t1", role="tenant_admin") -> Principal:
    return Principal(
        user_id=user,
        tenant_id=tenant,
        workspace_ids=[ws],
        active_workspace_id=ws,
        role=role,
    )


async def _open_conn(path: str):
    """请求作用域外的会话：复用 init_db/init_probe_db 建好的模块引擎（同一物理库文件），
    与后台 _run_probe 用的 open_probe_conn 同引擎，保证 emit 结果互相可见。"""
    from semantic_config.engine import get_session_factory

    return get_session_factory()()


async def _drain() -> None:
    """等待所有后台探查任务结束（StubProvider 很快）。"""
    for _ in range(500):
        tasks = list(orchestrator._REGISTRY.values())
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)


async def _scalar(conn, sql, params=()) -> int:
    res = await conn.execute(text(sql))
    val = res.scalar()
    return val if val is not None else 0


def test_probe_full_flow_stub_promotes_all_stages():
    async def _run():
        settings = get_settings()
        original_db = settings.db_url
        original_load_provider = orchestrator.load_provider
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        settings.db_url = "sqlite+aiosqlite:///" + tmp.name
        orchestrator.load_provider = StubProvider
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn(tmp.name)
            admin = _principal()
            ds = "mysql-e2e"
            await ds_repo.insert(
                conn,
                ds,
                "端到端库",
                "MYSQL",
                "{}",
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            await conn.commit()

            for stage in _STAGE_ORDER:
                sdef = def_by_stage(stage)
                resp = await orchestrator.submit(conn, ds, stage, admin)
                assert resp.status == "RUNNING"
                await _drain()
                await conn.commit()  # 刷新快照，读到后台连接已提交的 emit 结果

                # 任务应成功、阶段进入 REVIEWING
                task = await orchestrator.get_task(conn, ds, stage, admin)
                assert task.status == "SUCCESS", f"{stage} 任务未成功: {task}"

                # emit 应产出至少 1 行 staging
                rows = await staging_repo.list_all(
                    conn, sdef.table, ds, admin.tenant_id, admin.active_workspace_id
                )
                assert rows, f"{stage} 未产出 staging"

                # 候选态阶段：批量确定后再完成
                if sdef.has_rows:
                    await row_service.batch_confirm(
                        conn, sdef, ds, [r["id"] for r in rows], admin
                    )
                    await conn.commit()

                result = await stage_service.complete(conn, ds, stage, admin)
                await conn.commit()
                assert result["status"] == "COMPLETED"

            # 总状态 COMPLETED
            status = await stage_service.get_status(conn, ds, admin)
            assert status.overall_status == OVERALL_COMPLETED

            # 正式实体表均已 promote
            assert (
                await _scalar(conn, "SELECT COUNT(*) FROM domain WHERE is_deleted=0")
                >= 1
            )
            assert (
                await _scalar(conn, "SELECT COUNT(*) FROM dataset WHERE is_deleted=0")
                >= 1
            )
            # DATASET_FIELDS 阶段确认后随 dataset 一起落库（物理表名 dataset_column_meta）
            assert (
                await _scalar(
                    conn, "SELECT COUNT(*) FROM dataset_column_meta WHERE is_deleted=0"
                )
                >= 1
            )
            assert (
                await _scalar(
                    conn, "SELECT COUNT(*) FROM dimension_dict WHERE is_deleted=0"
                )
                >= 1
            )
            assert (
                await _scalar(
                    conn, "SELECT COUNT(*) FROM dataset_dimension WHERE is_deleted=0"
                )
                >= 1
            )
            assert (
                await _scalar(
                    conn, "SELECT COUNT(*) FROM metric_dict WHERE is_deleted=0"
                )
                >= 1
            )
            assert (
                await _scalar(
                    conn, "SELECT COUNT(*) FROM metric_formula_lib WHERE is_deleted=0"
                )
                >= 1
            )
            # v2.1 NOT NULL 列零空值（promote 前置校验的落地证据）
            assert (
                await _scalar(
                    conn,
                    "SELECT COUNT(*) FROM dataset WHERE is_deleted=0 AND (source IS NULL OR source='')",
                )
                == 0
            )
            assert (
                await _scalar(
                    conn,
                    "SELECT COUNT(*) FROM dataset_dimension WHERE is_deleted=0 AND (expr IS NULL OR expr='')",
                )
                == 0
            )
            assert (
                await _scalar(
                    conn,
                    "SELECT COUNT(*) FROM metric_formula_lib WHERE is_deleted=0 "
                    "AND (expr IS NULL OR expr='' OR time_window IS NULL OR time_window='')",
                )
                == 0
            )

            await conn.close()
        finally:
            settings.db_url = original_db
            orchestrator.load_provider = original_load_provider
            Path(tmp.name).unlink(missing_ok=True)

    asyncio.run(_run())


def test_probe_prereq_blocks_downstream_start():
    """未完成上游阶段时下游 start 返回 400（BadRequestError）。"""

    async def _run():
        settings = get_settings()
        original_db = settings.db_url
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        settings.db_url = "sqlite+aiosqlite:///" + tmp.name
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn(tmp.name)
            admin = _principal()
            ds = "mysql-prereq"
            await ds_repo.insert(
                conn,
                ds,
                "库",
                "MYSQL",
                "{}",
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            await conn.commit()

            from semantic_config.errors import BadRequestError
            import pytest

            with pytest.raises(BadRequestError):
                await orchestrator.submit(conn, ds, "DATASET", admin)
            await conn.close()
        finally:
            settings.db_url = original_db
            Path(tmp.name).unlink(missing_ok=True)

    asyncio.run(_run())


def test_probe_routes_registered():
    app = FastAPI()
    app.include_router(probe_router.router)
    paths = set(app.openapi()["paths"].keys())
    base = "/api/semantic-config/probe"
    expected = {
        f"{base}/overview",
        f"{base}/{{datasource_id}}/status",
        f"{base}/{{datasource_id}}/{{stage}}/start",
        f"{base}/{{datasource_id}}/{{stage}}/stop",
        f"{base}/{{datasource_id}}/{{stage}}/skip",
        f"{base}/{{datasource_id}}/{{stage}}/rerun",
        f"{base}/{{datasource_id}}/{{stage}}/task",
        f"{base}/{{datasource_id}}/{{stage}}/results",
        f"{base}/{{datasource_id}}/{{stage}}/complete",
        f"{base}/{{datasource_id}}/{{stage}}/upload",
        f"{base}/{{datasource_id}}/{{stage}}/export",
        f"{base}/{{datasource_id}}/{{stage}}/row",
        f"{base}/{{datasource_id}}/{{stage}}/row/{{row_id}}",
        f"{base}/{{datasource_id}}/{{stage}}/row/confirm",
        f"{base}/{{datasource_id}}/{{stage}}/row/skip",
        f"{base}/{{datasource_id}}/{{stage}}/row/delete",
        f"{base}/{{datasource_id}}/import/excel",
    }
    missing = expected - paths
    assert not missing, f"缺失路由: {missing}"

    # 7 个阶段路径段齐全
    assert {d.path for d in STAGE_DEFS.values()} == {
        "biz-domain",
        "dataset",
        "dataset-fields",
        "dimension-caliber",
        "dimension-manage",
        "metric-caliber",
        "metric-manage",
    }


def test_probe_results_pending_first_and_stage_excel_roundtrip():
    async def _run():
        settings = get_settings()
        original_db = settings.db_url
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        settings.db_url = "sqlite+aiosqlite:///" + tmp.name
        try:
            await init_db()
            await init_probe_db()
            conn = await _open_conn(tmp.name)
            admin = _principal()
            ds = "mysql-excel-roundtrip"
            await ds_repo.insert(
                conn,
                ds,
                "Excel 往返库",
                "MYSQL",
                "{}",
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )
            await probe_stage_repo.ensure_stages(
                conn,
                ds,
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                created_by=admin.user_id,
            )

            sdef = def_by_stage("DIMENSION_MANAGE")
            common = {
                "datasource_id": ds,
                "tenant_id": admin.tenant_id,
                "workspace_id": admin.active_workspace_id,
                "created_by": admin.user_id,
                "task_id": None,
            }
            await staging_repo.insert_row(
                conn,
                sdef.table,
                {"domain_id": 1, "dimension_name": "地区", "description": "已确认"},
                probe_status=ROW_CONFIRMED,
                **common,
            )
            await staging_repo.insert_row(
                conn,
                sdef.table,
                {"domain_id": 1, "dimension_name": "渠道", "description": "待确认"},
                probe_status=ROW_PENDING,
                **common,
            )
            await staging_repo.insert_row(
                conn,
                sdef.table,
                {"domain_id": 1, "dimension_name": "活动", "description": "已跳过"},
                probe_status=ROW_SKIPPED,
                **common,
            )
            await probe_stage_repo.update_status(
                conn,
                ds,
                sdef.stage,
                "COMPLETED",
                tenant_id=admin.tenant_id,
                workspace_id=admin.active_workspace_id,
                updated_by=admin.user_id,
            )
            await conn.commit()

            page = await staging_repo.find_page(
                conn, sdef.table, ds, admin.tenant_id, admin.active_workspace_id, 20, 0
            )
            assert [row["probe_status"] for row in page] == [
                ROW_PENDING,
                ROW_SKIPPED,
                ROW_CONFIRMED,
            ]

            exported = await upload_service.export_stage(conn, sdef, ds, admin)
            result = await upload_service.upload_stage(
                conn, sdef, ds, "dimension-manage.xlsx", exported, admin
            )
            await conn.commit()
            assert result == {"success": True, "imported": 3}

            stage_row = await probe_stage_repo.find_one(
                conn, ds, sdef.stage, admin.tenant_id, admin.active_workspace_id
            )
            assert stage_row["status"] == "REVIEWING"
            rows = await staging_repo.list_all(
                conn, sdef.table, ds, admin.tenant_id, admin.active_workspace_id
            )
            assert {(row["domain_id"], row["dimension_name"]) for row in rows} == {
                (1, "地区"),
                (1, "渠道"),
                (1, "活动"),
            }
            await conn.close()
        finally:
            settings.db_url = original_db
            Path(tmp.name).unlink(missing_ok=True)

    asyncio.run(_run())


def test_probe_routes_registered_on_unified_context_app():
    """The frontend talks to the unified Context Manager port, not semantic_config.main."""
    app = FastAPI()
    mount_semantic_config(app)
    paths = set(app.openapi()["paths"].keys())
    assert "/api/semantic-config/probe/overview" in paths
    assert "/api/semantic-config/probe/{datasource_id}/{stage}/start" in paths

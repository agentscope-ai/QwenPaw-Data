"""批次 1：探查状态机 + 总览/状态的权限与推导单测（服务层直调）。

覆盖 SPEC §13.4/§13.6/§13.7 与 §3.1：
- 隔离读：跨工作空间数据源在总览不可见。
- 懒创建（系统写）：member 读状态即补齐 7 行；幂等。
- overall 推导：NOT_STARTED / IN_PROGRESS / COMPLETED。
- 越权：跨空间按 datasource_id 读状态 → 404（NotFoundNoBody）。

用 in-memory sqlite（单连接内建两套 schema），服务层直调，避免起 HTTP。
"""

from __future__ import annotations

import asyncio

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy import update

from _sc_dbutil import new_memory_session

from profiling.models.probe import (
    OVERALL_COMPLETED,
    OVERALL_IN_PROGRESS,
    OVERALL_NOT_STARTED,
    STAGES,
    STATUS_COMPLETED,
    STATUS_REVIEWING,
    STATUS_SKIPPED,
)
from profiling.repositories import probe_stage_repo as stage_repo
from profiling.services import overview_service, stage_service
from profiling.tables import probe_stage
from semantic_config.errors import NotFoundNoBody
from semantic_config.repositories import datasource_repo as ds_repo


def _principal(
    user: str, ws: str, role: str = "tenant_admin", tenant: str = "t1"
) -> Principal:
    return Principal(
        user_id=user,
        tenant_id=tenant,
        workspace_ids=[ws],
        active_workspace_id=ws,
        role=role,
    )


async def _new_db():
    return await new_memory_session()


async def _make_ds(conn, code: str, p: Principal) -> str:
    await ds_repo.insert(
        conn,
        code,
        f"名称-{code}",
        "MYSQL",
        "{}",
        tenant_id=p.tenant_id,
        workspace_id=p.active_workspace_id,
        created_by=p.user_id,
    )
    return code


def test_overview_isolated_by_workspace():
    async def _run():
        conn = await _new_db()
        try:
            admin_w1 = _principal("u1", "w1")
            admin_w2 = _principal("u2", "w2")
            await _make_ds(conn, "mysql-w1a", admin_w1)
            await _make_ds(conn, "mysql-w1b", admin_w1)
            await _make_ds(conn, "mysql-w2a", admin_w2)

            page_w1 = await overview_service.list_page(
                conn, None, None, None, 1, 20, admin_w1
            )
            codes = {r.datasource_id for r in page_w1.records}
            assert codes == {"mysql-w1a", "mysql-w1b"}
            assert page_w1.total == 2
            # 未探查过 → 各阶段与总状态均 NOT_STARTED
            assert all(r.overall_status == OVERALL_NOT_STARTED for r in page_w1.records)

            page_w2 = await overview_service.list_page(
                conn, None, None, None, 1, 20, admin_w2
            )
            assert {r.datasource_id for r in page_w2.records} == {"mysql-w2a"}
        finally:
            await conn.close()

    asyncio.run(_run())


def test_status_lazy_create_is_idempotent_system_write():
    async def _run():
        conn = await _new_db()
        try:
            member = _principal("m1", "w1", role="member")  # 非管理员也能触发懒创建
            await _make_ds(conn, "mysql-w1a", _principal("u1", "w1"))

            resp = await stage_service.get_status(conn, "mysql-w1a", member)
            assert resp.overall_status == OVERALL_NOT_STARTED
            assert [it.stage for it in resp.stages] == list(STAGES)
            assert all(it.status == "NOT_STARTED" for it in resp.stages)

            rows = await stage_repo.list_by_datasource(conn, "mysql-w1a", "t1", "w1")
            assert len(rows) == 7
            assert all(r["created_by"] == "m1" for r in rows)  # created_by 取当前用户

            # 幂等：再次调用不新增行
            await stage_service.get_status(conn, "mysql-w1a", member)
            rows2 = await stage_repo.list_by_datasource(conn, "mysql-w1a", "t1", "w1")
            assert len(rows2) == 7
        finally:
            await conn.close()

    asyncio.run(_run())


def test_overall_derivation_in_overview():
    async def _run():
        conn = await _new_db()
        try:
            admin = _principal("u1", "w1")
            await _make_ds(conn, "mysql-mix", admin)
            await _make_ds(conn, "mysql-done", admin)
            # 先懒创建 6 行
            await stage_service.get_status(conn, "mysql-mix", admin)
            await stage_service.get_status(conn, "mysql-done", admin)

            # mysql-mix：一个 REVIEWING → IN_PROGRESS
            await conn.execute(
                update(probe_stage)
                .where(
                    probe_stage.c.datasource_id == "mysql-mix",
                    probe_stage.c.stage == "DATASET",
                )
                .values(status=STATUS_REVIEWING)
            )
            # mysql-done：全部 COMPLETED/SKIPPED → COMPLETED
            await conn.execute(
                update(probe_stage)
                .where(probe_stage.c.datasource_id == "mysql-done")
                .values(status=STATUS_COMPLETED)
            )
            await conn.execute(
                update(probe_stage)
                .where(
                    probe_stage.c.datasource_id == "mysql-done",
                    probe_stage.c.stage == "BIZ_DOMAIN",
                )
                .values(status=STATUS_SKIPPED)
            )

            page = await overview_service.list_page(
                conn, None, None, None, 1, 20, admin
            )
            by_code = {r.datasource_id: r for r in page.records}
            assert by_code["mysql-mix"].overall_status == OVERALL_IN_PROGRESS
            assert by_code["mysql-mix"].dataset_status == STATUS_REVIEWING
            assert by_code["mysql-done"].overall_status == OVERALL_COMPLETED
            assert by_code["mysql-done"].biz_domain_status == STATUS_SKIPPED
        finally:
            await conn.close()

    asyncio.run(_run())


def test_status_cross_workspace_returns_404():
    async def _run():
        conn = await _new_db()
        try:
            await _make_ds(conn, "mysql-w1a", _principal("u1", "w1"))
            intruder = _principal("u2", "w2")
            with pytest.raises(NotFoundNoBody):
                await stage_service.get_status(conn, "mysql-w1a", intruder)
            # 且不会为越权者创建任何行
            rows = await stage_repo.list_by_datasource(conn, "mysql-w1a", "t1", "w2")
            assert rows == []
        finally:
            await conn.close()

    asyncio.run(_run())


@pytest.mark.parametrize(
    "fields_status,other_status,expected_overall",
    [
        ("NOT_STARTED", "NOT_STARTED", "NOT_STARTED"),
        ("PROBING", "NOT_STARTED", "IN_PROGRESS"),
        ("REVIEWING", "NOT_STARTED", "IN_PROGRESS"),
        ("COMPLETED", "NOT_STARTED", "IN_PROGRESS"),
        ("NOT_STARTED", "COMPLETED", "IN_PROGRESS"),
        ("REVIEWING", "COMPLETED", "IN_PROGRESS"),
        ("COMPLETED", "COMPLETED", "COMPLETED"),
        ("SKIPPED", "COMPLETED", "COMPLETED"),
    ],
)
def test_overview_matches_detail_including_dataset_fields(
    fields_status, other_status, expected_overall
):
    async def _run():
        conn = await _new_db()
        try:
            admin = _principal("u1", "w1")
            ds = await _make_ds(conn, "mysql-fields", admin)
            await stage_service.get_status(conn, ds, admin)
            for stage in STAGES:
                await stage_repo.update_status(
                    conn,
                    ds,
                    stage,
                    fields_status if stage == "DATASET_FIELDS" else other_status,
                    tenant_id="t1",
                    workspace_id="w1",
                    updated_by="u1",
                )

            overview = await overview_service.list_page(
                conn, ds, None, None, 1, 20, admin
            )
            detail = await stage_service.get_status(conn, ds, admin)
            record = overview.records[0]
            assert record.dataset_fields_status == fields_status
            assert record.overall_status == detail.overall_status == expected_overall
            for item in detail.stages:
                assert getattr(record, f"{item.stage.lower()}_status") == item.status
        finally:
            await conn.close()

    asyncio.run(_run())

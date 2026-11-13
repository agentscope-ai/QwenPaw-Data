"""真实任务/staging 落库与回读；会话额外模拟 PG 的失败事务门禁。

SQLite 的 SQL 错误不会自动禁止后续查询，因此不能只依靠普通 SQLite 异常
验证回滚顺序。这里显式模拟该门禁，不宣称替代真实 PostgreSQL 集成测试。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from asyncpg.exceptions import InFailedSQLTransactionError
from cm.infrastructure.access.support import authorization
from cm.infrastructure.models.support import FakeCaller
from context_manager.auth import bind_authorization, get_authorization
from context_manager.model.usage import (
    UsageContext,
    bind_usage_context,
    current_usage_context,
)
from qwenpaw_data_auth import Principal
from profiling.repositories import probe_stage_repo, probe_task_repo, staging_repo
from profiling.services import orchestrator, row_service
from profiling.stages import def_by_stage
from profiling.tables import PROFILING_TABLE_LIST
from semantic_config.repositories import datasource_repo
from semantic_config.tables import datasource, metadata
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


class _AbortingSession(AsyncSession):
    """真实 SQLite 会话，加上“SQL 失败后必须 rollback”的 PG 行为约束。"""

    def __init__(self, *args, events, faults, **kwargs):
        super().__init__(*args, **kwargs)
        self.events = events
        self.faults = faults
        self.aborted = False
        self.original_error = None

    async def execute(self, statement, *args, **kwargs):
        if self.aborted:
            self.events.append("query_before_rollback")
            raise DBAPIError(
                str(statement),
                None,
                InFailedSQLTransactionError("current transaction is aborted"),
            )
        try:
            return await super().execute(statement, *args, **kwargs)
        except SQLAlchemyError as exc:
            self.aborted = True
            self.original_error = exc
            raise

    async def rollback(self):
        self.events.append("rollback")
        if self.faults.get("rollback"):
            raise RuntimeError("rollback unavailable")
        await super().rollback()
        self.aborted = False

    async def close(self):
        self.events.append("close")
        await super().close()
        if self.faults.get("close"):
            raise RuntimeError("close unavailable")


@pytest.fixture
async def probe_env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'probe-errors.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = Principal(
        user_id="u1",
        tenant_id="t1",
        workspace_ids=["w1"],
        active_workspace_id="w1",
        role="tenant_admin",
    )
    env = SimpleNamespace(
        factory=factory,
        admin=admin,
        datasource_id="postgresql-error-test",
        events=[],
        sessions=[],
        faults={},
    )
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync: metadata.create_all(
                sync, tables=[datasource, *PROFILING_TABLE_LIST]
            )
        )
    async with factory() as conn:
        await datasource_repo.insert(
            conn,
            env.datasource_id,
            "异常恢复测试",
            "POSTGRESQL",
            "{}",
            tenant_id="t1",
            workspace_id="w1",
            created_by="u1",
        )
        row_id = await staging_repo.insert_row(
            conn,
            "staging_biz_domain",
            {"domain_name": "confirmed_domain"},
            datasource_id=env.datasource_id,
            tenant_id="t1",
            workspace_id="w1",
            created_by="u1",
            task_id="historical-task",
        )
        await row_service.batch_confirm(
            conn,
            def_by_stage("BIZ_DOMAIN"),
            env.datasource_id,
            [row_id],
            admin,
        )
        await conn.commit()

    async def open_conn():
        if not env.sessions:
            env.events.append("open_probe")
            if env.faults.pop("probe_open", False):
                # 下一次打开仍须成功，供失败状态独立落库。
                env.sessions.append(None)
                raise RuntimeError("probe connection unavailable")
            conn = _AbortingSession(
                bind=engine,
                expire_on_commit=False,
                events=env.events,
                faults=env.faults,
            )
        else:
            env.events.append("open_status")
            if env.faults.get("status_open"):
                raise RuntimeError("status connection unavailable")
            conn = factory()
        env.sessions.append(conn)
        return conn

    monkeypatch.setattr(orchestrator, "open_probe_conn", open_conn)
    try:
        yield env
    finally:
        key = orchestrator._key("t1", "w1", env.datasource_id, "BIZ_DOMAIN")
        runner = orchestrator._REGISTRY.get(key)
        if runner is not None:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        orchestrator._REGISTRY.pop(key, None)
        orchestrator._TOKENS.pop(key, None)
        await engine.dispose()


async def _submit_and_wait(env):
    async with env.factory() as conn:
        response = await orchestrator.submit(
            conn, env.datasource_id, "BIZ_DOMAIN", env.admin
        )
    key = orchestrator._key("t1", "w1", env.datasource_id, "BIZ_DOMAIN")
    runner = orchestrator._REGISTRY.get(key)
    if runner is not None:
        await asyncio.wait_for(runner, timeout=5)
    assert key not in orchestrator._REGISTRY
    assert key not in orchestrator._TOKENS
    return response.task_id


async def _readback(env):
    async with env.factory() as conn:
        task = await orchestrator.get_task(
            conn, env.datasource_id, "BIZ_DOMAIN", env.admin
        )
        stage = await probe_stage_repo.find_one(
            conn, env.datasource_id, "BIZ_DOMAIN", "t1", "w1"
        )
        rows = await staging_repo.list_all(
            conn, "staging_biz_domain", env.datasource_id, "t1", "w1"
        )
        latest = await probe_task_repo.find_latest(
            conn, env.datasource_id, "BIZ_DOMAIN", "t1", "w1"
        )
    return task, stage, rows, latest


@pytest.mark.parametrize("wait_for_provider", [False, True])
async def test_shutdown_settles_running_probe_in_sqlite(
    probe_env, monkeypatch, wait_for_provider
):
    from unittest.mock import AsyncMock

    from profiling.provider import ProbeContext

    entered = asyncio.Event()

    class Provider:
        async def probe_biz_domains(self, ctx, sink):
            entered.set()
            await asyncio.Event().wait()

    ctx = ProbeContext(probe_env.datasource_id, "POSTGRESQL", {}, probe_env.admin)
    monkeypatch.setattr(
        orchestrator.ctx_builder, "build_context", AsyncMock(return_value=ctx)
    )
    monkeypatch.setattr(orchestrator, "load_provider", Provider)
    async with probe_env.factory() as conn:
        await orchestrator.submit(conn, probe_env.datasource_id, "BIZ_DOMAIN", probe_env.admin)
        if wait_for_provider:
            await asyncio.wait_for(entered.wait(), 5)
        async with asyncio.timeout(5):
            await orchestrator.shutdown_probes()

    task, stage, _, latest = await _readback(probe_env)
    assert task.status == "STOPPED"
    assert latest["status"] == "STOPPED"
    assert stage["status"] != "PROBING"
    assert not orchestrator._REGISTRY
    assert not orchestrator._TOKENS


@pytest.mark.parametrize("mode", ["create", "rerun", "refine"])
async def test_probe_usage_detaches_session_but_preserves_auth(
    probe_env, monkeypatch, mode
):
    from unittest.mock import AsyncMock

    caller = FakeCaller()
    seen = []
    entered, release = asyncio.Event(), asyncio.Event()
    complete = caller.complete

    async def observed_complete(*args, **kwargs):
        entered.set()
        await release.wait()
        seen.append((current_usage_context(), get_authorization()))
        return await complete(*args, **kwargs)

    class Provider:
        async def probe_biz_domains(self, ctx, sink):
            await caller.complete(())

    from profiling.provider import ProbeContext

    ctx = ProbeContext(probe_env.datasource_id, "POSTGRESQL", {}, probe_env.admin)
    monkeypatch.setattr(caller, "complete", observed_complete)
    monkeypatch.setattr(
        orchestrator.ctx_builder, "build_context", AsyncMock(return_value=ctx)
    )
    monkeypatch.setattr(orchestrator, "load_provider", Provider)
    auth = authorization("u1", "t1", "w1", role="tenant_admin")
    parent = UsageContext(session_id="host-session", chat_id="host-chat", probe=True)
    before = current_usage_context()
    runner = None
    try:
        with bind_authorization(auth), bind_usage_context(parent):
            async with probe_env.factory() as conn:
                response = await orchestrator.submit(
                    conn,
                    probe_env.datasource_id,
                    "BIZ_DOMAIN",
                    probe_env.admin,
                    mode=mode,
                )
            assert current_usage_context() == parent
        runner = orchestrator._REGISTRY[
            orchestrator._key("t1", "w1", probe_env.datasource_id, "BIZ_DOMAIN")
        ]
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        release.set()
        if runner is not None:
            await asyncio.wait_for(runner, 5)
    assert current_usage_context() == before
    assert response.task_id.startswith("PROBE_")
    assert seen == [
        (
            UsageContext(
                source="语义探查", source_step=f"semantic_profiling#{response.task_id}"
            ),
            auth,
        )
    ]
    task, _, _, _ = await _readback(probe_env)
    assert task.status == "SUCCESS"


async def _sql_failure(conn, *args):
    await staging_repo.insert_row(
        conn,
        "staging_biz_domain",
        {"domain_name": "uncommitted_domain"},
        datasource_id="postgresql-error-test",
        tenant_id="t1",
        workspace_id="w1",
        created_by="u1",
        task_id="uncommitted-task",
    )
    await conn.execute(text("SELECT missing_probe_column"))


async def test_sql_failure_rolls_back_then_records_original_error(
    probe_env, monkeypatch, caplog
):
    monkeypatch.setattr(orchestrator.ctx_builder, "build_context", _sql_failure)
    task_id = await _submit_and_wait(probe_env)
    task, stage, rows, latest = await _readback(probe_env)

    assert task.status == "FAILED"
    assert "missing_probe_column" in task.error_msg
    assert "transaction is aborted" not in task.error_msg
    assert latest["finished_at"] is not None
    assert stage["status"] == "REVIEWING"
    assert [(r["domain_name"], r["probe_status"]) for r in rows] == [
        ("confirmed_domain", "CONFIRMED")
    ]
    assert probe_env.events == ["open_probe", "rollback", "close", "open_status"]
    record = next(
        r for r in caplog.records if r.getMessage().startswith("probe failed ")
    )
    assert record.exc_info[1] is probe_env.sessions[0].original_error
    assert task_id in record.getMessage()
    assert (
        "datasource_id=postgresql-error-test stage=BIZ_DOMAIN tenant_id=t1 workspace_id=w1"
        in record.getMessage()
    )


@pytest.mark.parametrize("action", ["cancel", "stopped", "skipped", "skipped_only"])
async def test_interruptions_preserve_stop_and_skip(probe_env, monkeypatch, action):
    async def interrupt(conn, *args):
        if action != "cancel":
            async with probe_env.factory() as request:
                latest = await probe_task_repo.find_latest(
                    request,
                    probe_env.datasource_id,
                    "BIZ_DOMAIN",
                    "t1",
                    "w1",
                )
                if action != "skipped_only":
                    await probe_task_repo.finish(request, latest["task_id"], "STOPPED")
                if action.startswith("skipped"):
                    await probe_stage_repo.update_status(
                        request,
                        probe_env.datasource_id,
                        "BIZ_DOMAIN",
                        "SKIPPED",
                        tenant_id="t1",
                        workspace_id="w1",
                        updated_by="u1",
                    )
                await request.commit()
        if action == "cancel":
            try:
                await _sql_failure(conn)
            except SQLAlchemyError:
                raise asyncio.CancelledError() from None
        await _sql_failure(conn)

    monkeypatch.setattr(orchestrator.ctx_builder, "build_context", interrupt)
    await _submit_and_wait(probe_env)
    task, stage, rows, latest = await _readback(probe_env)
    assert task.status == "STOPPED"
    assert task.error_msg is None
    assert latest["finished_at"] is not None
    assert stage["status"] == (
        "SKIPPED" if action.startswith("skipped") else "REVIEWING"
    )
    assert [r["domain_name"] for r in rows] == ["confirmed_domain"]
    assert probe_env.events == ["open_probe", "rollback", "close", "open_status"]


@pytest.mark.parametrize("fault", ["rollback", "close", "status_open", "status_write"])
async def test_recovery_failure_keeps_original_log_and_releases_registry(
    probe_env,
    monkeypatch,
    caplog,
    fault,
):
    probe_env.faults[fault] = True
    monkeypatch.setattr(orchestrator.ctx_builder, "build_context", _sql_failure)
    original_update = probe_stage_repo.update_status

    async def update_status(conn, *args, **kwargs):
        if fault == "status_write" and conn in probe_env.sessions[1:]:
            raise RuntimeError("status write unavailable")
        return await original_update(conn, *args, **kwargs)

    monkeypatch.setattr(probe_stage_repo, "update_status", update_status)
    await _submit_and_wait(probe_env)
    task, _, rows, _ = await _readback(probe_env)
    assert task.status == ("RUNNING" if fault.startswith("status_") else "FAILED")
    # status_write 在 finish 写入之后失败；独立事务整体回滚，不能半套提交。
    assert [r["domain_name"] for r in rows] == ["confirmed_domain"]
    assert probe_env.events == ["open_probe", "rollback", "close", "open_status"]
    records = [r for r in caplog.records if r.exc_info]
    assert records[0].exc_info[1] is probe_env.sessions[0].original_error
    assert "missing_probe_column" in str(records[0].exc_info[1])
    assert any(
        (
            "terminal status update failed"
            if fault.startswith("status_")
            else f"{fault} failed"
        )
        in r.getMessage()
        for r in records[1:]
    )


async def test_opening_probe_session_failure_is_also_recorded(probe_env, caplog):
    probe_env.faults["probe_open"] = True
    await _submit_and_wait(probe_env)
    task, _, _, latest = await _readback(probe_env)
    assert task.status == "FAILED"
    assert task.error_msg == "probe connection unavailable"
    assert latest["finished_at"] is not None
    assert probe_env.events == ["open_probe", "open_status"]
    assert "probe connection unavailable" in caplog.text

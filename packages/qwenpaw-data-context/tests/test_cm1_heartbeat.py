"""Verify real heartbeat leases and scoped orphan SQL without external services."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import context_manager.heartbeat.refresh as refresh_module
import context_manager.heartbeat.sweep as sweep_module
from context_manager.heartbeat import Heartbeat, is_fresh, lock_scope, lock_scope_sync, stale_cutoff, sweep_connection
from context_manager.schema.relational import documents, kg_user_claims, metadata


@pytest.fixture(autouse=True)
def heartbeat_settings(monkeypatch):
    settings = SimpleNamespace(heartbeat=SimpleNamespace(stale_after_seconds=90, interval_seconds=60, sweep_interval_seconds=60))
    monkeypatch.setattr(refresh_module, "get_settings", lambda: settings)
    monkeypatch.setattr(sweep_module, "get_settings", lambda: settings)


def test_freshness_uses_deployment_window_and_null_is_abandoned():
    assert stale_cutoff(now=1000) == 910
    assert not is_fresh(None, now=1000) and not is_fresh(909, now=1000)
    assert is_fresh(910, now=1000) and is_fresh(1000, now=1000)


async def test_heartbeat_retries_failed_refresh_and_stop_drains_inflight_touch():
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def touch():
        calls.append("touch")
        if len(calls) == 1:
            raise RuntimeError("temporary SQL failure")
        entered.set()
        await release.wait()

    heartbeat = Heartbeat(touch, interval=0)
    heartbeat.start()
    with pytest.raises(RuntimeError, match="already started"):
        heartbeat.start()
    await asyncio.wait_for(entered.wait(), 2)
    stopped = asyncio.create_task(heartbeat.stop())
    await asyncio.sleep(0)
    assert not stopped.done()
    release.set()
    await asyncio.wait_for(stopped, 2)
    assert calls == ["touch", "touch"] and heartbeat._task.done()
    await heartbeat.stop()


async def test_heartbeat_accepts_sync_touch_and_stop_before_start():
    calls = []

    def touch():
        calls.append("touch")
        heartbeat._stop.set()

    heartbeat = Heartbeat(touch)
    heartbeat.start()
    await asyncio.wait_for(heartbeat._task, 2)
    await heartbeat.stop()
    assert calls == ["touch"]
    unstarted = Heartbeat(lambda: pytest.fail("unstarted heartbeat touched SQL"))
    await unstarted.stop()


def test_sync_sqlite_scope_lock_is_idempotent_and_parameterized():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            lock_scope_sync(connection, "tenant:workspace:claim")
            lock_scope_sync(connection, "tenant:workspace:claim")
            assert connection.execute(text("SELECT name FROM cm_claim_lock")).scalars().all() == ["tenant:workspace:claim"]
    finally:
        engine.dispose()


async def test_async_sqlite_scope_lock_runs_on_owning_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with AsyncSession(engine) as session, session.begin():
            await lock_scope(session, "tenant:workspace:claim")
            await lock_scope(session, "tenant:workspace:claim")
            assert (await session.execute(text("SELECT name FROM cm_claim_lock"))).scalars().all() == ["tenant:workspace:claim"]
    finally:
        await engine.dispose()


async def test_postgresql_scope_locks_use_transaction_advisory_locks():
    connection = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), execute=Mock())
    lock_scope_sync(connection, "quoted'key")
    statement, parameters = connection.execute.call_args.args
    assert "pg_advisory_xact_lock" in str(statement) and parameters == {"key": "quoted'key"}
    session = SimpleNamespace(connection=AsyncMock(return_value=connection), execute=AsyncMock())
    await lock_scope(session, "quoted'key")
    assert "pg_advisory_xact_lock" in str(session.execute.await_args.args[0])
    assert session.execute.await_args.args[1] == parameters


def test_orphan_release_fails_only_documents_of_exact_tenant_workspace_user_epoch(monkeypatch):
    monkeypatch.setattr(sweep_module, "_cutoff", lambda: 910)
    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[documents, kg_user_claims])
    try:
        with engine.begin() as connection:
            connection.execute(kg_user_claims.insert(), [
                {"tenant_id": "tenant", "workspace_id": "workspace", "user_id": "alice", "run_epoch": 3, "heartbeat_at": 900, "executor_pod": "old"},
                {"tenant_id": "tenant", "workspace_id": "other", "user_id": "alice", "run_epoch": 3, "heartbeat_at": 950, "executor_pod": "live"},
                {"tenant_id": "other", "workspace_id": "workspace", "user_id": "alice", "run_epoch": 3, "heartbeat_at": None, "executor_pod": None},
            ])
            connection.execute(documents.insert(), [
                {"tenant_id": tenant, "workspace_id": workspace, "user_id": user, "filename": name,
                 "document_id": name, "byte_size": 1, "status": status, "created_at": "now", "updated_at": "now", "run_epoch": epoch}
                for tenant, workspace, user, name, status, epoch in (
                    ("tenant", "workspace", "alice", "stale", "processing", 3),
                    ("tenant", "workspace", "alice", "new-epoch", "processing", 4),
                    ("tenant", "workspace", "alice", "finished", "completed", 3),
                    ("tenant", "workspace", "bob", "other-user", "processing", 3),
                    ("tenant", "other", "alice", "live", "processing", 3),
                    ("other", "workspace", "alice", "null-lease", "processing", 3),
                )
            ])
            sweep_connection(connection)
            sweep_connection(connection)
            rows = {row["filename"]: row for row in connection.execute(select(documents)).mappings()}
            assert rows["stale"]["status"] == "failed" and rows["stale"]["ingest_error"] == "执行中断"
            assert rows["finished"]["status"] == "completed"
            assert all(rows[name]["status"] == "processing" for name in ("new-epoch", "other-user", "live", "null-lease"))
            claims = {row["workspace_id"]: row for row in connection.execute(select(kg_user_claims).where(kg_user_claims.c.tenant_id == "tenant")).mappings()}
            assert claims["workspace"]["heartbeat_at"] is None and claims["workspace"]["executor_pod"] is None
            assert claims["other"]["executor_pod"] == "live"
    finally:
        engine.dispose()


async def test_sweep_isolates_each_pool_failure_and_stops_after_current_pass(monkeypatch, caplog):
    visits = []

    def sync():
        visits.append("sync")
        raise RuntimeError("sync failed")

    async def asynchronous():
        visits.append("async")
        raise RuntimeError("async failed")

    def csv():
        visits.append("csv")

    monkeypatch.setattr(sweep_module, "_sweep_sync_engine", sync)
    monkeypatch.setattr(sweep_module, "_sweep_async_engine", asynchronous)
    monkeypatch.setattr(sweep_module, "_sweep_expired_csv", csv)
    await sweep_module._sweep_once()
    assert visits == ["sync", "async", "csv"]
    assert "sync scan failed" in caplog.text and "async scan failed" in caplog.text
    stop = asyncio.Event()

    async def once():
        visits.append("pass")
        stop.set()

    monkeypatch.setattr(sweep_module, "_sweep_once", once)
    await sweep_module.run_orphan_sweep(stop)
    assert visits[-1] == "pass"
    await sweep_module.run_orphan_sweep(stop)
    assert visits.count("pass") == 1


async def test_sweep_does_not_open_async_pool_after_success(monkeypatch):
    visits = []

    def sync():
        visits.append("sync")

    async def asynchronous():
        visits.append("async")

    monkeypatch.setattr(sweep_module, "_sweep_sync_engine", sync)
    monkeypatch.setattr(sweep_module, "_sweep_async_engine", asynchronous)
    monkeypatch.setattr(sweep_module, "_sweep_expired_csv", lambda: visits.append("csv"))
    await sweep_module._sweep_once()
    assert visits == ["sync", "csv"]


def test_csv_cleanup_is_retention_bounded_and_never_follows_symlinks(monkeypatch):
    # Path doubles ensure this test never writes or deletes a real file.
    root = Mock()
    root.resolve.return_value = root
    root.is_dir.return_value = True
    directory, outside = Mock(), Mock()
    for item in (directory, outside):
        item.resolve.return_value = item
        item.is_dir.return_value = True
    directory.is_relative_to.return_value = True
    outside.is_relative_to.return_value = False
    root.rglob.return_value = (directory, outside)
    old, fresh, symlink = Mock(), Mock(), Mock()
    for item, modified in ((old, 800), (fresh, 950), (symlink, 800)):
        item.is_file.return_value = True
        item.is_symlink.return_value = item is symlink
        item.stat.return_value = SimpleNamespace(st_mtime=modified)
    directory.glob.return_value = (old, fresh, symlink)
    monkeypatch.setattr("context_manager.settings.get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(home=root), sql=SimpleNamespace(retention_seconds=100)))
    monkeypatch.setattr(sweep_module.time, "time", lambda: 1000)
    sweep_module._sweep_expired_csv()
    old.unlink.assert_called_once_with()
    fresh.unlink.assert_not_called()
    symlink.unlink.assert_not_called()
    outside.glob.assert_not_called()

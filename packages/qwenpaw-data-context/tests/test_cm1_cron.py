"""Exercise persisted user-ingestion scheduling and process lifecycle offline."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

import context_manager.cron.repository as repository_module
from context_manager.cron import CronJobRepository, CronManager, CronSettings, ScheduleSpec
from context_manager.schema.relational import cron_jobs

NOW = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)  # Monday.


@pytest.fixture
def jobs(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    cron_jobs.create(engine)
    monkeypatch.setattr(repository_module, "get_database", lambda: SimpleNamespace(engine=engine))
    yield CronJobRepository()
    engine.dispose()


@pytest.mark.parametrize("weekday", ["0", "7", "sun", "0,7"])
def test_crontab_sunday_numbers_are_not_apscheduler_monday(weekday):
    schedule = ScheduleSpec(cron=f"0 0 * * {weekday}", timezone="UTC")
    assert schedule.next_run(NOW) == datetime(2026, 1, 11, tzinfo=UTC)
    assert ScheduleSpec.model_validate_json(schedule.model_dump_json()) == schedule


def test_schedules_are_aware_strictly_after_and_validate_finite_shape():
    assert ScheduleSpec(cron="0 8 * * 1-5", timezone="Asia/Shanghai").next_run(NOW) == NOW + timedelta(days=1)
    once = ScheduleSpec(type="once", run_at=NOW + timedelta(seconds=1), timezone="UTC")
    assert once.next_run(NOW) == NOW + timedelta(seconds=1)
    assert once.next_run(NOW + timedelta(seconds=1)) is None
    for fields in ({"cron": "* * * *"}, {"cron": "0 0 * * 8"}, {"cron": "0 0 * * 0/0"},
                   {"cron": "0 0 * * *", "timezone": "Not/AZone"}, {"type": "once", "run_at": datetime(2026, 1, 1)}):
        with pytest.raises(ValueError):
            ScheduleSpec(**fields)
    with pytest.raises(ValueError, match="timezone-aware"):
        once.next_run(datetime(2026, 1, 1))
    assert ScheduleSpec._weekdays("*/2") == "sun,tue,thu,sat"


def test_repository_preserves_seed_and_one_conditional_due_claim(jobs, monkeypatch):
    schedule = ScheduleSpec(cron="0 0 * * *", timezone="UTC")
    assert jobs.create("job", "user_ingest", schedule, now=NOW)
    assert not jobs.create("job", "changed", ScheduleSpec(cron="0 1 * * *", timezone="UTC"), now=NOW)
    row = jobs.get("job")
    assert row["handler"] == "user_ingest" and row["schedule_json"] == schedule.model_dump_json()
    assert jobs.try_start("job", NOW) is None
    due = NOW + timedelta(days=1)
    stale = jobs.get("job")
    assert jobs.try_start("job", due)["started_at"] == jobs._stamp(due)
    current = jobs.get("job")
    assert current["next_run_at"] == jobs._stamp(due + timedelta(days=1))
    monkeypatch.setattr(jobs, "get", lambda _job: stale)
    assert jobs.try_start("job", due) is None  # A competing process observed the same due row.
    assert jobs.list_scheduled(("unknown",)) == []


def test_manual_requests_coalesce_and_disabled_once_runs_are_not_repeated(jobs):
    once = ScheduleSpec(type="once", run_at=NOW + timedelta(days=1), timezone="UTC")
    assert jobs.create("manual", "user_ingest", once, enabled=False, now=NOW)
    assert jobs.list_scheduled(("user_ingest",)) == []
    jobs.run_now("manual")
    jobs.run_now("manual")
    assert jobs.get("manual")["run_requested"] is True
    assert len(jobs.list_scheduled(("user_ingest",))) == 1
    assert jobs.try_start("manual", datetime.now(UTC) + timedelta(seconds=1)) is not None
    assert jobs.get("manual")["next_run_at"] is None and jobs.get("manual")["run_requested"] is False
    assert jobs.try_start("manual", datetime.now(UTC) + timedelta(days=1)) is None
    jobs.update("manual", ScheduleSpec(cron="0 1 * * *", timezone="UTC"))
    assert jobs.get("manual")["enabled"] and jobs.get("manual")["next_run_at"] is not None
    jobs.delete("manual")
    assert jobs.get("manual") is None
    with pytest.raises(ValueError, match="timezone-aware"):
        jobs._stamp(datetime(2026, 1, 1))


async def test_disabled_manager_never_touches_repository_or_ingestion(jobs, monkeypatch):
    ingest = SimpleNamespace(ingest=AsyncMock())
    manager = CronManager(ingest, settings=CronSettings(enabled=False))
    monkeypatch.setattr(manager.jobs, "create", lambda *_args: pytest.fail("disabled scheduler accessed SQL"))
    await manager.start()
    await manager.refresh()
    await manager.shutdown()
    assert not manager.started and not manager._runs and not manager._timers
    ingest.ingest.assert_not_awaited()


async def test_start_seeds_once_preserves_saved_plan_and_cancels_timers(jobs):
    schedule = ScheduleSpec(cron="0 12 * * *", timezone="UTC")
    jobs.create("user_ingest", "user_ingest", schedule)
    manager = CronManager(SimpleNamespace(ingest=AsyncMock()), settings=CronSettings(timezone="UTC"))
    await manager.start()
    try:
        timer = manager._timers["user_ingest"]
        await manager.start()
        assert manager.started and jobs.get("user_ingest")["schedule_json"] == schedule.model_dump_json()
        assert manager._timers["user_ingest"] is timer
    finally:
        await manager.shutdown()
    assert timer.cancelled() and not manager._timers and not manager._runs


async def test_refresh_failure_rolls_back_started_state(jobs, monkeypatch):
    manager = CronManager(SimpleNamespace(ingest=AsyncMock()), settings=CronSettings())
    monkeypatch.setattr(manager, "refresh", AsyncMock(side_effect=RuntimeError("SQL read failed")))
    with pytest.raises(RuntimeError, match="SQL read failed"):
        await manager.start()
    assert not manager.started and not manager._timers and not manager._runs


async def test_dispatch_calls_real_injected_handler_with_aware_time_and_reschedules(jobs):
    jobs.create("job", "user_ingest", ScheduleSpec(cron="0 0 * * *", timezone="UTC"), now=NOW)
    ingest = SimpleNamespace(ingest=AsyncMock())
    manager = CronManager(ingest, settings=CronSettings())
    manager._started = True
    manager._launch("job")
    await asyncio.wait_for(manager._runs["job"], 2)
    assert ingest.ingest.await_count == 1
    assert ingest.ingest.await_args.args[0].tzinfo is UTC
    assert "job" in manager._timers and not manager._runs
    await manager.shutdown()


async def test_ingestion_failure_logs_and_keeps_next_schedule(jobs, caplog):
    jobs.create("job", "user_ingest", ScheduleSpec(cron="0 0 * * *", timezone="UTC"), now=NOW)
    manager = CronManager(SimpleNamespace(ingest=AsyncMock(side_effect=RuntimeError("ingestion failed"))), settings=CronSettings())
    manager._started = True
    manager._launch("job")
    await asyncio.wait_for(manager._runs["job"], 2)
    assert "cron job failed: job" in caplog.text
    assert "job" in manager._timers and jobs.get("job")["next_run_at"] is not None
    await manager.shutdown()


async def test_concurrency_budget_and_shutdown_cancel_drain_ingestion(jobs):
    entered, cleanup_started, release, cleaned = (asyncio.Event() for _ in range(4))
    calls = []

    async def ingest(now):
        calls.append(now)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await release.wait()
            cleaned.set()

    for job in ("first", "second"):
        jobs.create(job, "user_ingest", ScheduleSpec(cron="0 0 * * *", timezone="UTC"), now=NOW)
    manager = CronManager(SimpleNamespace(ingest=ingest), settings=CronSettings(max_threads=1))
    manager._started = True
    manager._launch("first")
    manager._launch("second")
    await asyncio.wait_for(entered.wait(), 2)
    assert len(calls) == 1 and len(manager._runs) == 2
    shutdown = asyncio.create_task(manager.shutdown())
    await asyncio.wait_for(cleanup_started.wait(), 2)
    assert not shutdown.done()
    release.set()
    await asyncio.wait_for(shutdown, 2)
    assert cleaned.is_set() and not manager._runs and not manager._timers and not manager.started
    assert len(calls) == 1

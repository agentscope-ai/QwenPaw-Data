"""Exercise persisted schedules, execution claims and worker cleanup."""

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url

from context_manager.cron import (
    CronJobRepository,
    CronManager,
    CronSettings,
    ScheduleSpec,
)
from context_manager.db import close_database, get_database, initialize_database
from context_manager.settings.db import DbSettings


@pytest.fixture(params=["sqlite", "postgresql"])
def database_settings(request, tmp_path):
    url = f"sqlite:///{tmp_path / 'cron.db'}"
    admin, schema = None, None
    if request.param == "postgresql":
        url = os.environ.get("CM_CRON_TEST_PG_URL")
        if not url:
            pytest.skip(
                "Set CM_CRON_TEST_PG_URL to an isolated PostgreSQL test database"
            )
        admin = create_engine(url)
        schema = "cron_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = (
            make_url(url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
    settings = DbSettings(url=url)
    try:
        initialize_database(settings)
        yield settings
    finally:
        close_database()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def test_schedule_boundaries_and_one_shot():
    schedule = ScheduleSpec(cron="0 23 * * *")
    instant = datetime(2026, 10, 13, 15, tzinfo=UTC)
    assert schedule.next_run(instant - timedelta(seconds=1)) == instant
    assert schedule.next_run(instant) == instant + timedelta(days=1)
    once = ScheduleSpec(type="once", run_at=instant)
    assert once.next_run(instant - timedelta(seconds=1)) == instant
    assert once.next_run(instant) is None
    weekdays = ScheduleSpec(cron="0 23 * * 1-5")
    friday = datetime(2026, 10, 16, 15, tzinfo=UTC)
    assert weekdays.next_run(friday) == friday + timedelta(days=3)


def test_restart_preserves_edited_plan_and_pause(database_settings):
    repo = CronJobRepository()
    assert repo.create("user_ingest", "user_ingest", ScheduleSpec(cron="0 23 * * *"))
    edited = ScheduleSpec(cron="15 8 * * mon-fri", timezone="UTC")
    repo.update("user_ingest", edited, enabled=False)
    close_database()
    initialize_database(database_settings)
    restarted = CronJobRepository()
    assert not restarted.create(
        "user_ingest", "user_ingest", ScheduleSpec(cron="0 23 * * *")
    )
    row = restarted.get("user_ingest")
    assert ScheduleSpec.model_validate_json(row["schedule_json"]) == edited
    assert row["enabled"] is False
    assert row["next_run_at"] is None
    restarted.run_now("user_ingest")
    now = datetime.now(UTC)
    claim = restarted.try_start("user_ingest", now)
    assert claim is not None
    assert restarted.get("user_ingest")["next_run_at"] is None
    assert restarted.try_start("user_ingest", now) is None


@pytest.mark.asyncio
async def test_manager_seeds_configured_schedule_and_preserves_saved_edits(
    database_settings,
):
    manager = CronManager(
        SimpleNamespace(ingest=AsyncMock()),
        settings=CronSettings(hour=8, minute=5, timezone="UTC"),
    )
    await manager.start()
    try:
        row = manager.jobs.get("user_ingest")
        assert ScheduleSpec.model_validate_json(row["schedule_json"]) == ScheduleSpec(
            cron="5 8 * * *",
            timezone="UTC",
        )
        edited = ScheduleSpec(cron="15 9 * * mon-fri", timezone="UTC")
        manager.jobs.update("user_ingest", edited, enabled=False)
    finally:
        await manager.shutdown()
    restarted = CronManager(
        SimpleNamespace(ingest=AsyncMock()), settings=CronSettings(hour=12)
    )
    await restarted.start()
    try:
        row = restarted.jobs.get("user_ingest")
        assert ScheduleSpec.model_validate_json(row["schedule_json"]) == edited
        assert row["enabled"] is False
    finally:
        await restarted.shutdown()


def test_one_due_trigger_is_taken_by_one_repository(database_settings):
    repo, other = CronJobRepository(), CronJobRepository()
    now = datetime(2026, 10, 13, tzinfo=UTC)
    repo.create("job", "user_ingest", ScheduleSpec(cron="* * * * *"), now=now)
    due = now + timedelta(minutes=1)
    with ThreadPoolExecutor(2) as pool:
        claims = list(
            pool.map(lambda store: store.try_start("job", due), (repo, other))
        )
    assert sum(claim is not None for claim in claims) == 1
    row = repo.get("job")
    assert datetime.fromisoformat(row["next_run_at"]) > due
    assert other.try_start("job", due) is None
    second = other.try_start("job", datetime.fromisoformat(row["next_run_at"]))
    assert second is not None
    assert other.try_start("job", datetime.fromisoformat(row["next_run_at"])) is None


@pytest.mark.asyncio
async def test_two_workers_share_claim_and_shutdown_releases_it(database_settings):
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def ingest(_):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    adapter = SimpleNamespace(ingest=AsyncMock(side_effect=ingest))
    settings = CronSettings()
    first = CronManager(adapter, settings=settings)
    second = CronManager(adapter, settings=settings)
    await first.start()
    await second.start()
    try:
        first.jobs.run_now("user_ingest")
        await asyncio.gather(first.refresh(), second.refresh())
        await asyncio.wait_for(entered.wait(), timeout=2)
        assert adapter.ingest.await_count == 1
    finally:
        await first.shutdown()
        await second.shutdown()
    assert cancelled.is_set()
    assert "running_token" not in first.jobs.get("user_ingest")
    assert not first.started and not second.started


@pytest.mark.parametrize("kind", ["cron", "once"])
def test_run_now_after_a_trigger_can_run_again(database_settings, kind):
    repo = CronJobRepository()
    now = datetime.now(UTC)
    schedule = (
        ScheduleSpec(cron="* * * * *")
        if kind == "cron"
        else ScheduleSpec(type="once", run_at=now)
    )
    repo.create("job", "user_ingest", schedule, now=now - timedelta(minutes=1))
    assert repo.try_start("job", now) is not None
    repo.run_now("job")
    requested = repo.get("job")["next_run_at"]
    assert repo.get("job")["run_requested"] is True
    released_at = datetime.now(UTC) + timedelta(minutes=3)
    following = repo.try_start("job", released_at)
    assert following is not None
    assert repo.get("job")["run_requested"] is False
    assert repo.get("job")["next_run_at"] != requested or kind == "cron"
    assert repo.try_start("job", released_at) is None


def test_run_now_after_trigger_stays_requested_until_the_next_start(
    database_settings,
):
    repo = CronJobRepository()
    now = datetime.now(UTC)
    repo.create(
        "job",
        "user_ingest",
        ScheduleSpec(cron="* * * * *"),
        now=now - timedelta(minutes=1),
    )
    assert repo.try_start("job", now) is not None
    repo.run_now("job")
    row = repo.get("job")
    assert row["run_requested"] is True
    assert repo.try_start("job", now + timedelta(minutes=3)) is not None
    assert repo.get("job")["run_requested"] is False


@pytest.mark.asyncio
async def test_one_shot_failure_releases_claim_and_allows_explicit_retry(
    database_settings,
):
    adapter = SimpleNamespace(
        ingest=AsyncMock(side_effect=[RuntimeError("failed"), None])
    )
    manager = CronManager(adapter, settings=CronSettings(max_threads=1))
    now = datetime.now(UTC)
    manager.jobs.create(
        "once",
        "user_ingest",
        ScheduleSpec(type="once", run_at=now + timedelta(seconds=0.2)),
    )
    await manager.start()
    try:
        for attempt in (1, 2):
            async with asyncio.timeout(2):
                while adapter.ingest.await_count < attempt:
                    await asyncio.sleep(0.01)
            assert manager.jobs.get("once")["next_run_at"] is None
            if attempt == 1:
                manager.jobs.run_now("once")
                await manager.refresh()
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_idle_manager_does_not_query_sql(database_settings):
    manager = CronManager(SimpleNamespace(ingest=AsyncMock()))
    manager.jobs.create(
        "user_ingest",
        "user_ingest",
        ScheduleSpec(type="once", run_at=datetime.now(UTC) + timedelta(days=1)),
    )
    await manager.start()
    statements = []

    def record_statement(
        connection, cursor, statement, parameters, context, executemany
    ):
        statements.append(statement)

    engine = get_database().engine
    event.listen(engine, "before_cursor_execute", record_statement)
    try:
        await asyncio.sleep(1.1)
        assert statements == []
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
        await manager.shutdown()

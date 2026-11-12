# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from qwenpaw_data.host.core.api.models.cron import CronJobWrite, ScheduleSpec
from qwenpaw_data.host.core.cron.manager import CronManager
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.db.tables import CronJobRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.cron import CronJobRepository
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.time import utcnow

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def _mgr() -> CronManager:
    return CronManager(get_session_factory(), MagicMock())


async def _wait_runs(mgr: CronManager) -> None:
    if mgr._runs:
        await asyncio.gather(*mgr._runs, return_exceptions=True)


def _job(*, session_id: str | None = None) -> dict:
    job = {
        "id": "cron_test",
        "user_id": TEST_USER_ID,
        "tenant_id": TEST_TENANT_ID,
        "workspace_id": TEST_WORKSPACE_ID,
        "name": "agent-job",
        "enabled": True,
        "message": "hello cron",
        "datasource_id": "ds_1",
        "channel": "console",
        "schedule": {"type": "cron", "cron": "0 9 * * *", "timezone": "UTC"},
    }
    if session_id is not None:
        job["session_id"] = session_id
    return job


@pytest.mark.asyncio
async def test_agent_run_requires_datasource() -> None:
    await reset_engine()
    await init_db()
    job = _job()
    job["datasource_id"] = ""
    with pytest.raises(ValueError, match="datasource_id"):
        await _mgr()._run_console(job)


@pytest.mark.asyncio
async def test_agent_run_mints_temporary_token() -> None:
    await reset_engine()
    await init_db()
    runtime = MagicMock()
    runtime.run = AsyncMock()

    with (
        patch(
            "qwenpaw_data.host.core.cron.manager.generate_temporary_token",
            return_value="tok-minted",
        ) as mint,
        patch(
            "qwenpaw_data.host.core.cron.manager.ChatRuntime",
            return_value=runtime,
        ),
    ):
        await _mgr()._run_console(_job())

    mint.assert_called_once()
    identity = mint.call_args.args[0]
    assert identity.user_id == TEST_USER_ID
    runtime.run.assert_awaited_once()
    assert runtime.run.await_args.kwargs["access_token"] == "tok-minted"


@pytest.mark.asyncio
async def test_run_console_creates_session_when_job_has_no_session_id() -> None:
    await reset_engine()
    await init_db()
    runtime = MagicMock()
    runtime.run = AsyncMock()
    with patch("qwenpaw_data.host.core.cron.manager.ChatRuntime", return_value=runtime):
        await _mgr()._run_console(_job())
        await _mgr()._run_console(_job())
    async with get_session_factory()() as db:
        items, total = await SessionRepository(db, TEST_IDENTITY).list()
    assert total == 2
    assert items[0][0].id != items[1][0].id


@pytest.mark.asyncio
async def test_run_console_reuses_existing_session() -> None:
    await reset_engine()
    await init_db()
    existing = Session.create(
        identity=TEST_IDENTITY,
        title="keep",
        channel="console",
        datasource_id="ds_1",
    )
    async with get_session_factory()() as db:
        await SessionRepository(db, TEST_IDENTITY).add(existing)
        await db.commit()

    runtime = MagicMock()
    runtime.run = AsyncMock()
    with patch("qwenpaw_data.host.core.cron.manager.ChatRuntime", return_value=runtime):
        await _mgr()._run_console(_job(session_id=existing.id))
    async with get_session_factory()() as db:
        items, total = await SessionRepository(db, TEST_IDENTITY).list()
    assert total == 1
    assert items[0][0].id == existing.id
    assert items[0][0].chat_count == 1


@pytest.mark.asyncio
async def test_run_console_does_not_create_when_session_id_missing() -> None:
    await reset_engine()
    await init_db()
    with pytest.raises(ValueError, match="session not found"):
        await _mgr()._run_console(_job(session_id="ses_missing"))


async def _insert_once_due() -> dict:
    due_at = utcnow() - timedelta(seconds=1)
    body = CronJobWrite(
        name="once-job",
        enabled=True,
        message="hello once",
        datasource_id="ds_1",
        channel="console",
        schedule=ScheduleSpec(type="once", run_at=due_at, timezone="UTC"),
    )
    async with get_session_factory()() as db:
        job = await CronJobRepository(db, TEST_IDENTITY).create(body)
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        row.next_run_at = due_at
        await db.commit()
        return job


async def _insert_job(
    *,
    session_id: str | None = None,
    due: bool = False,
    enabled: bool = True,
) -> dict:
    body = CronJobWrite(
        name="agent-job",
        enabled=enabled,
        message="hello cron",
        datasource_id="ds_1",
        channel="console",
        session_id=session_id,
        schedule=ScheduleSpec(type="cron", cron="0 9 * * *", timezone="UTC"),
    )
    async with get_session_factory()() as db:
        job = await CronJobRepository(db, TEST_IDENTITY).create(body)
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        if due:
            row.next_run_at = utcnow() - timedelta(seconds=1)
        await db.commit()
        return job


async def _job_times(job_id: str):
    async with get_session_factory()() as db:
        row = await db.get(CronJobRow, job_id)
        assert row is not None
        return row.next_run_at, row.running_at


@pytest.mark.asyncio
async def test_tick_skips_when_session_busy() -> None:
    await reset_engine()
    await init_db()
    existing = Session.create(
        identity=TEST_IDENTITY,
        title="busy",
        channel="console",
        datasource_id="ds_1",
    )
    chat = existing.open_chat(
        text="current",
        datasource_id="ds_1",
        has_active_chat=False,
    )
    async with get_session_factory()() as db:
        await SessionRepository(db, TEST_IDENTITY).add(existing)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
    job = await _insert_job(session_id=existing.id, due=True)

    runtime = MagicMock()
    runtime.run = AsyncMock()
    entered = asyncio.Event()
    original = CronManager._run_console

    async def watch(self, fired: dict) -> None:
        entered.set()
        await original(self, fired)

    with (
        patch.object(CronManager, "_run_console", watch),
        patch("qwenpaw_data.host.core.cron.manager.ChatRuntime", return_value=runtime),
    ):
        mgr = _mgr()
        await mgr.tick()
        assert not entered.is_set()
        next_at, running_at = await _job_times(job["id"])
        assert next_at is not None and next_at <= utcnow()
        assert running_at is None

        async with get_session_factory()() as db:
            chat_repo = ChatRepository(db, TEST_IDENTITY)
            loaded = await chat_repo.get(chat.id)
            loaded.mark_status("completed")
            await chat_repo.save(loaded)
            await db.commit()

        await mgr.tick()
        await _wait_runs(mgr)
        assert entered.is_set()
        runtime.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_once_job_starts_only_once() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_once_due()
    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == [job["id"]]
    next_at, running_at = await _job_times(job["id"])
    assert next_at is None
    assert running_at is None


@pytest.mark.asyncio
async def test_two_managers_try_start_job_once() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job(due=True)
    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr_a, mgr_b = _mgr(), _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await asyncio.gather(mgr_a.tick(), mgr_b.tick())
        await asyncio.gather(_wait_runs(mgr_a), _wait_runs(mgr_b))
    assert calls == [job["id"]]


@pytest.mark.asyncio
async def test_run_now_pulls_next_run_at_to_now() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job()
    before, _ = await _job_times(job["id"])
    assert before is not None

    async with get_session_factory()() as db:
        await CronJobRepository(db, TEST_IDENTITY).run_now(job["id"])
        await db.commit()
    pulled, running_at = await _job_times(job["id"])
    assert pulled is not None
    assert pulled < before
    assert running_at is None

    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == [job["id"]]
    next_at, running_at = await _job_times(job["id"])
    assert next_at == before
    assert running_at is None


@pytest.mark.asyncio
async def test_clear_running_skips_slot_passed_during_run() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job()
    async with get_session_factory()() as db:
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        started = utcnow() - timedelta(minutes=10)
        row.running_at = started
        row.heartbeat_at = started
        row.next_run_at = utcnow() - timedelta(seconds=1)
        await db.commit()

    async with get_session_factory()() as db:
        await CronJobRepository.clear_running(db, job["id"], started)
        await db.commit()
    next_at, running_at = await _job_times(job["id"])
    assert running_at is None
    assert next_at is not None and next_at > utcnow()


@pytest.mark.asyncio
async def test_tick_skips_job_that_is_already_running() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job(due=True)
    async with get_session_factory()() as db:
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        now = utcnow()
        row.running_at = now
        row.heartbeat_at = now
        await db.commit()

    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == []
    _, running_at = await _job_times(job["id"])
    assert running_at is not None


@pytest.mark.asyncio
async def test_tick_clears_expired_running_without_starting() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job(due=True)
    async with get_session_factory()() as db:
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        stale = utcnow() - timedelta(minutes=10)
        row.running_at = stale
        row.heartbeat_at = stale
        row.next_run_at = utcnow() - timedelta(seconds=1)
        await db.commit()

    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == []
    next_at, running_at = await _job_times(job["id"])
    assert running_at is None
    assert next_at is not None and next_at > utcnow()
    async with get_session_factory()() as db:
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        assert row.heartbeat_at is None


@pytest.mark.asyncio
async def test_tick_clears_expired_running_and_keeps_future_slot() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job()
    scheduled, _ = await _job_times(job["id"])
    assert scheduled is not None and scheduled > utcnow()
    async with get_session_factory()() as db:
        row = await db.get(CronJobRow, job["id"])
        assert row is not None
        stale = utcnow() - timedelta(minutes=10)
        row.running_at = stale
        row.heartbeat_at = stale
        await db.commit()

    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == []
    next_at, running_at = await _job_times(job["id"])
    assert running_at is None
    assert next_at == scheduled


@pytest.mark.asyncio
async def test_tick_does_not_start_second_run_while_first_is_in_progress() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job(due=True)
    release = asyncio.Event()
    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])
        await release.wait()

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await asyncio.sleep(0)
        assert calls == [job["id"]]
        _, running_at = await _job_times(job["id"])
        assert running_at is not None

        async with get_session_factory()() as db:
            await CronJobRepository(db, TEST_IDENTITY).run_now(job["id"])
            await db.commit()
        await mgr.tick()
        await asyncio.sleep(0)
        assert calls == [job["id"]]

        release.set()
        await _wait_runs(mgr)


@pytest.mark.asyncio
async def test_tick_skips_paused_due_job() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job(due=True)
    async with get_session_factory()() as db:
        await CronJobRepository(db, TEST_IDENTITY).set_enabled(job["id"], False)
        await db.commit()

    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == []


@pytest.mark.asyncio
async def test_run_now_starts_paused_job() -> None:
    await reset_engine()
    await init_db()
    job = await _insert_job()
    async with get_session_factory()() as db:
        repo = CronJobRepository(db, TEST_IDENTITY)
        await repo.set_enabled(job["id"], False)
        await repo.run_now(job["id"])
        await db.commit()

    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await _wait_runs(mgr)
    assert calls == [job["id"]]
    next_at, running_at = await _job_times(job["id"])
    assert next_at is None
    assert running_at is None


@pytest.mark.asyncio
async def test_tick_runs_different_jobs_in_parallel() -> None:
    await reset_engine()
    await init_db()
    first = await _insert_job(due=True)
    second = await _insert_job(due=True)
    started = {first["id"]: asyncio.Event(), second["id"]: asyncio.Event()}
    release = asyncio.Event()

    async def watch(self, fired: dict) -> None:
        started[fired["id"]].set()
        await release.wait()

    mgr = _mgr()
    with patch.object(CronManager, "_run_console", watch):
        await mgr.tick()
        await asyncio.wait_for(
            asyncio.gather(started[first["id"]].wait(), started[second["id"]].wait()),
            timeout=1,
        )
        release.set()
        await _wait_runs(mgr)


@pytest.mark.asyncio
async def test_tick_stops_claiming_at_max_cron_threads() -> None:
    await reset_engine()
    await init_db()
    first = await _insert_job(due=True)
    second = await _insert_job(due=True)
    release = asyncio.Event()
    calls: list[str] = []

    async def watch(self, fired: dict) -> None:
        calls.append(fired["id"])
        await release.wait()

    mgr = _mgr()
    with (
        patch.object(get_settings().cron, "max_threads", 1),
        patch.object(CronManager, "_run_console", watch),
    ):
        await mgr.tick()
        await asyncio.sleep(0)
        assert len(calls) == 1
        other_id = second["id"] if calls[0] == first["id"] else first["id"]
        async with get_session_factory()() as db:
            due = await CronJobRepository.list_ready_to_run(db)
        assert any(item["id"] == other_id for item in due)
        release.set()
        await _wait_runs(mgr)
        await mgr.tick()
        await _wait_runs(mgr)
        assert set(calls) == {first["id"], second["id"]}

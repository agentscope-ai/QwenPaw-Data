"""Business-row heartbeats replace the unpublished execution leases."""

import asyncio
import os
from uuid import uuid4

import pytest
from qwenpaw_data_auth import Principal
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from context_manager.schema.relational import recovery_runs
from profiling.repositories import probe_task_repo
from profiling.services import orchestrator
from profiling.tables import PROFILING_TABLE_LIST
from semantic_config.errors import BadRequestError
from semantic_config.models.weave_task import WeaveTaskSubmit
from semantic_config.repositories import datasource_repo, explore_repo
from semantic_config.services import weave_task_service
from semantic_config.tables import SC_TABLE_LIST, domain, metadata


@pytest.fixture(params=["sqlite", "postgresql"])
async def env(request, tmp_path, monkeypatch):
    schema, admin_engine = None, None
    options = {}
    url = f"sqlite+aiosqlite:///{tmp_path / 'execution.db'}"
    if request.param == "postgresql":
        url = os.environ.get("SC_EXECUTION_TEST_PG_URL")
        if not url:
            pytest.skip(
                "SC_EXECUTION_TEST_PG_URL must point to an isolated test database"
            )
        admin_engine = create_async_engine(url)
        schema = "execution_" + uuid4().hex
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        options = {"connect_args": {"server_settings": {"search_path": schema}}}
    engine = create_async_engine(url, **options)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda db: metadata.create_all(
                db, tables=[*SC_TABLE_LIST, *PROFILING_TABLE_LIST]
            )
        )
        await conn.run_sync(lambda db: recovery_runs.create(db, checkfirst=True))
    admin = Principal(
        user_id="u",
        tenant_id="t",
        workspace_ids=["w"],
        active_workspace_id="w",
        role="tenant_admin",
    )
    async with factory() as db:
        await datasource_repo.insert(
            db,
            "ds",
            "source",
            "SQLITE",
            "{}",
            tenant_id="t",
            workspace_id="w",
            created_by="u",
        )
        await db.execute(
            text(
                "INSERT INTO domain (domain_name, tenant_id, workspace_id, is_deleted) "
                "VALUES ('retail', 't', 'w', 0)"
            )
        )
        await db.commit()

    async def new_session():
        return factory()

    async def no_hook(**kwargs):
        return None

    async def hang(**kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(orchestrator, "open_probe_conn", new_session)
    monkeypatch.setattr(orchestrator, "_run_probe", hang)
    monkeypatch.setattr(weave_task_service, "_new_session", new_session)
    monkeypatch.setattr(weave_task_service, "_weave_worker", hang)
    try:
        yield factory, admin
    finally:
        for task in list(weave_task_service._BACKGROUND_TASKS):
            task.cancel()
        await asyncio.gather(
            *weave_task_service._BACKGROUND_TASKS, return_exceptions=True
        )
        await engine.dispose()
        if admin_engine is not None:
            async with admin_engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            await admin_engine.dispose()


@pytest.mark.asyncio
async def test_two_weaves_in_one_workspace_both_stay_running(env):
    factory, admin = env

    async def submit():
        async with factory() as db:
            row = await weave_task_service.submit(
                db,
                WeaveTaskSubmit(
                    task_name="weave",
                    datasource_id="ds",
                    weave_mode="INCREMENTAL",
                ),
                admin,
                graph_access=None,
            )
            return row.task_id

    first, second = await asyncio.gather(submit(), submit())
    assert first != second
    async with factory() as db:
        rows = [
            await weave_task_service.get(db, task_id, admin)
            for task_id in (first, second)
        ]
    assert {row.status for row in rows} == {"RUNNING"}


@pytest.mark.asyncio
async def test_killing_a_finished_weave_does_not_report_success(env):
    factory, admin = env
    async with factory() as db:
        created = await weave_task_service.submit(
            db,
            WeaveTaskSubmit(
                task_name="weave", datasource_id="ds", weave_mode="INCREMENTAL"
            ),
            admin,
            graph_access=None,
        )
        await db.execute(
            text("UPDATE weave_task SET status = 'SUCCESS' WHERE task_id = :task_id"),
            {"task_id": created.task_id},
        )
        await db.commit()
        with pytest.raises(BadRequestError):
            await weave_task_service.kill(db, created.task_id, admin)
        fresh = await weave_task_service.get(db, created.task_id, admin)
    assert fresh.status == "SUCCESS"


@pytest.mark.asyncio
async def test_killing_a_running_weave_cancels_the_local_task(env):
    factory, admin = env
    async with factory() as db:
        created = await weave_task_service.submit(
            db,
            WeaveTaskSubmit(
                task_name="weave", datasource_id="ds", weave_mode="INCREMENTAL"
            ),
            admin,
            graph_access=None,
        )
        local = weave_task_service._LOCAL_WEAVES[created.task_id]
        killed = await weave_task_service.kill(db, created.task_id, admin)
    done, pending = await asyncio.wait({local}, timeout=2)
    assert local in done and not pending
    assert local.cancelled()
    assert killed.status == "RUNNING"
    assert killed.cancel_requested is True
    async with factory() as db:
        fresh = await weave_task_service.get(db, created.task_id, admin)
    assert fresh.status == "RUNNING"
    assert fresh.cancel_requested is True


@pytest.mark.asyncio
async def test_one_fresh_probe_per_stage(env):
    factory, admin = env

    async def submit():
        async with factory() as db:
            try:
                row = await orchestrator.submit(db, "ds", "BIZ_DOMAIN", admin)
            except BadRequestError:
                return None
            return row.task_id

    results = await asyncio.gather(submit(), submit())
    assert sum(item is not None for item in results) == 1


@pytest.mark.asyncio
async def test_stale_probe_does_not_block_a_new_submit(env):
    factory, admin = env
    async with factory() as db:
        first = await orchestrator.submit(db, "ds", "BIZ_DOMAIN", admin)
        await db.execute(
            text("UPDATE probe_task SET heartbeat_at = 0 WHERE task_id = :task_id"),
            {"task_id": first.task_id},
        )
        await db.commit()
        second = await orchestrator.submit(db, "ds", "BIZ_DOMAIN", admin)
        old = await probe_task_repo.find_latest(db, "ds", "BIZ_DOMAIN", "t", "w")
    assert second.task_id != first.task_id
    assert old["task_id"] == second.task_id
    async with factory() as db:
        previous = await db.execute(
            text("SELECT status FROM probe_task WHERE task_id = :task_id"),
            {"task_id": first.task_id},
        )
    assert previous.scalar_one() == "FAILED"


@pytest.mark.asyncio
async def test_fresh_explore_claim_rejects_the_second_round(env):
    factory, _admin = env
    async with factory() as db:
        first = await explore_repo.claim_run(db, "t", "w", "ds")
        await db.commit()
    async with factory() as db:
        second = await explore_repo.claim_run(db, "t", "w", "ds")
    assert first == 1
    assert second is None


@pytest.mark.asyncio
async def test_mark_running_does_not_revive_a_finished_round(env):
    factory, _admin = env
    async with factory() as db:
        first = await explore_repo.claim_run(db, "t", "w", "ds")
        await db.commit()
        await explore_repo.finish_run(db, "t", "w", "ds", status="ok", epoch=int(first))
        await db.commit()
        second = await explore_repo.claim_run(db, "t", "w", "ds")
        await explore_repo.finish_run(
            db, "t", "w", "ds", status="ok", epoch=int(second)
        )
        changed = await explore_repo.mark_running(db, "t", "w", "ds", int(first))
        await db.commit()
        row = await explore_repo.get_scope(db, "t", "w", "ds")
        live = await explore_repo.claim_run(db, "t", "w", "ds")
        assert await explore_repo.mark_running(db, "t", "w", "ds", int(live)) == 1
    assert changed == 0
    assert row["last_status"] == "ok"
    assert int(row["run_epoch"]) == int(second)
    assert int(live) == int(second) + 1

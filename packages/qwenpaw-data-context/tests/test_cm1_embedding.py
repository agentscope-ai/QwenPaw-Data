"""Offline rebuild coordination tests; missing adjacent ports are explicit skips."""

import asyncio
import importlib
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from context_manager.db import CMDatabase
from context_manager.model import ModelConfigError, ResolvedModel
from context_manager.model.usage import bind_usage_context, current_usage_context
from context_manager.schema.relational import embedding_jobs, metadata, recovery_runs
from context_manager.settings.embedding import EmbeddingSettings

OWNER = ("tenant", "workspace")
SNAPSHOT = {"model": "embed-model", "base_url": "https://models.example/v1", "dim": 2}


@pytest.fixture
def embedding_modules():
    modules = {}
    for name in ("manager", "repository", "service", "rebuild"):
        try:
            modules[name] = importlib.import_module(f"context_manager.embedding.{name}")
        except ModuleNotFoundError as exc:
            pytest.skip(f"Adjacent dependency unavailable: {exc.name}")
    return SimpleNamespace(**modules)


@pytest.fixture
def jobs_database(embedding_modules, monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    database = CMDatabase(engine)
    metadata.create_all(engine, tables=[embedding_jobs, recovery_runs])
    monkeypatch.setattr(embedding_modules.repository, "get_database", lambda: database)
    monkeypatch.setattr(embedding_modules.manager, "get_settings", lambda: SimpleNamespace(
        embedding=EmbeddingSettings(), heartbeat=SimpleNamespace(interval_seconds=60),
    ))
    yield engine
    database.close()


async def drain(jobs):
    await asyncio.gather(*tuple(jobs._tasks))


async def test_rebuild_workspace_reservation_is_shared_and_snapshot_has_no_secrets(embedding_modules, jobs_database):
    manager = embedding_modules.manager.EmbeddingJobManager()
    other = embedding_modules.manager.EmbeddingJobManager()
    entered, release = asyncio.Event(), asyncio.Event()
    usage = []

    async def work(progress, job_id, rid, previous_rid):
        usage.append(current_usage_context())
        await progress({"phase": "embedding", "current_label": "Entity", "labels_done": 0, "labels_total": 1})
        entered.set()
        await release.wait()
        return 1

    try:
        with bind_usage_context(session_id="request", chat_id="chat", probe=True):
            job_id = await manager.submit(OWNER, SNAPSHOT | {"api_key": "synthetic-secret"}, work, created_by="admin")
        await asyncio.wait_for(entered.wait(), 2)
        assert (await other.get(OWNER, job_id))["status"] == "running"
        with pytest.raises(embedding_modules.repository.RebuildBusy):
            await other.submit(OWNER, SNAPSHOT, work, created_by="admin")
        second_owner = ("tenant", "other-workspace")
        second = await other.submit(second_owner, SNAPSHOT, work, created_by="admin")
        assert await other.get(second_owner, job_id) is None
        release.set()
        await asyncio.wait_for(asyncio.gather(drain(manager), drain(other)), 2)
        job = await manager.get(OWNER, job_id)
        assert job["status"] == "success" and job["progress"]["phase"] == "done"
        assert job["config_snapshot"] == SNAPSHOT
        assert "synthetic-secret" not in json.dumps(job)
        assert (await other.get(second_owner, second))["status"] == "success"
        assert all(not context.session_id and not context.chat_id and not context.probe for context in usage)
        assert all(context.source_step.startswith("vector_index_rebuild#") for context in usage)
    finally:
        release.set()
        await manager.aclose()
        await other.aclose()


async def test_rebuild_failure_and_shutdown_are_secret_safe_and_drain_children(embedding_modules, jobs_database, caplog):
    manager = embedding_modules.manager.EmbeddingJobManager()

    async def fail(progress, *_attempt):
        raise RuntimeError("synthetic-secret provider error")

    job_id = await manager.submit(OWNER, SNAPSHOT, fail, created_by="admin")
    await asyncio.wait_for(drain(manager), 2)
    job = await manager.get(OWNER, job_id)
    assert job["status"] == "failed"
    assert "synthetic-secret" not in json.dumps(job) and "synthetic-secret" not in caplog.text
    entered, finished = asyncio.Event(), asyncio.Event()

    async def blocked(progress, *_attempt):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    retry = await manager.submit(OWNER, SNAPSHOT, blocked, created_by="admin", job_id=job_id)
    await asyncio.wait_for(entered.wait(), 2)
    await manager.aclose()
    assert retry == job_id and finished.is_set() and not manager._tasks
    assert (await manager.get(OWNER, job_id))["status"] == "failed"
    with pytest.raises(RuntimeError, match="closed"):
        await manager.submit(OWNER, SNAPSHOT, blocked, created_by="admin")


async def test_shutdown_settles_workers_cancelled_before_first_step(embedding_modules, jobs_database):
    manager = embedding_modules.manager.EmbeddingJobManager()
    repository = manager._repository
    attempted_at = datetime.now(UTC).isoformat()
    job_id = "unstarted"
    repository.reserve({
        "tenant_id": OWNER[0], "workspace_id": OWNER[1], "job_id": job_id,
        "created_by": "admin", "status": "pending", "created_at": attempted_at,
        "attempted_at": attempted_at, "started_at": None, "finished_at": None,
        "error": None, "progress": {"phase": "pending"}, "config_snapshot": SNAPSHOT,
    }, retry=False)
    work = AsyncMock(return_value=0)
    manager.adopt(OWNER, job_id, 0, work, repository_module_rid(embedding_modules, job_id, attempted_at))
    await manager.aclose()
    work.assert_not_awaited()
    assert (await manager.get(OWNER, job_id))["status"] == "failed"
    assert not manager._tasks


def repository_module_rid(modules, job_id, attempted_at):
    return modules.repository.rebuild_rid(job_id, attempted_at)


async def test_expired_retry_fences_previous_worker(embedding_modules, jobs_database):
    manager = embedding_modules.manager.EmbeddingJobManager()
    other = embedding_modules.manager.EmbeddingJobManager()
    old_entered, old_release = asyncio.Event(), asyncio.Event()
    new_entered, new_release = asyncio.Event(), asyncio.Event()

    async def old_work(progress, *_attempt):
        old_entered.set()
        await old_release.wait()
        return 0

    async def new_work(progress, *_attempt):
        new_entered.set()
        await new_release.wait()
        return 0

    try:
        job_id = await manager.submit(OWNER, SNAPSHOT, old_work, created_by="admin")
        await asyncio.wait_for(old_entered.wait(), 2)
        with jobs_database.begin() as conn:
            conn.execute(update(embedding_jobs).where(embedding_jobs.c.job_id == job_id).values(heartbeat_at=0))
        await other.submit(OWNER, SNAPSHOT, new_work, created_by="admin", job_id=job_id)
        await asyncio.wait_for(new_entered.wait(), 2)
        old_release.set()
        await asyncio.wait_for(drain(manager), 2)
        assert (await other.get(OWNER, job_id))["status"] == "running"
        new_release.set()
        await asyncio.wait_for(drain(other), 2)
        assert (await other.get(OWNER, job_id))["status"] == "success"
    finally:
        old_release.set()
        new_release.set()
        await manager.aclose()
        await other.aclose()


async def test_lost_progress_lease_does_not_finish_another_attempt(embedding_modules, jobs_database, monkeypatch):
    manager = embedding_modules.manager.EmbeddingJobManager()
    finish = AsyncMock()
    real_finish = manager._repository.finish
    calls = []

    def record_finish(*args, **kwargs):
        calls.append((args, kwargs))
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(manager._repository, "finish", record_finish)
    monkeypatch.setattr(manager._repository, "update_progress", lambda *args: False)

    async def work(progress, *_attempt):
        await progress({"phase": "embedding"})
        await finish()

    try:
        await manager.submit(OWNER, SNAPSHOT, work, created_by="admin")
        await asyncio.wait_for(drain(manager), 2)
        assert not calls
        finish.assert_not_awaited()
    finally:
        await manager.aclose()


async def test_sql_cancellation_drains_thread_and_preserves_contextvars(embedding_modules, jobs_database):
    manager = embedding_modules.manager.EmbeddingJobManager()
    entered, release, finished = Event(), Event(), Event()
    seen = []

    def operation():
        entered.set()
        try:
            assert release.wait(2)
            seen.append(current_usage_context().session_id)
        finally:
            finished.set()

    with bind_usage_context(session_id="request"):
        task = asyncio.create_task(manager._db(operation))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished.is_set() and seen == ["request"]
    await manager.aclose()


@pytest.mark.parametrize("result_status,expected", [("SUCCESS", "success"), ("DEGRADED", "success"), ("FAILED", "failed")])
async def test_closed_recovery_receipt_is_projected_and_private_message_redacted(embedding_modules, jobs_database, result_status, expected):
    manager = embedding_modules.manager.EmbeddingJobManager()
    now = datetime.now(UTC).isoformat()
    job_id = "recovery-job"
    manager._repository.reserve({
        "tenant_id": OWNER[0], "workspace_id": OWNER[1], "job_id": job_id,
        "created_by": "admin", "status": "pending", "created_at": now,
        "attempted_at": now, "started_at": None, "finished_at": None,
        "error": None, "progress": {"phase": "pending"}, "config_snapshot": SNAPSHOT,
    }, retry=False)
    with jobs_database.begin() as conn:
        conn.execute(recovery_runs.insert().values(
            tenant_id=OWNER[0], workspace_id=OWNER[1], task_type="embedding", task_id=job_id,
            rid=repository_module_rid(embedding_modules, job_id, now), recovery_state="CLOSED",
            result_status=result_status, message="synthetic-secret recovery detail",
        ))
    job = await manager.get(OWNER, job_id)
    assert job["status"] == expected
    assert "synthetic-secret" not in json.dumps(job)
    with jobs_database.connect() as conn:
        assert conn.execute(select(embedding_jobs.c.status)).scalar_one() == expected
    await manager.aclose()


@pytest.mark.parametrize("dimension,store_dimensions,error", [
    (None, {2}, ModelConfigError), (3, {2}, ValueError),
])
async def test_rebuild_dimension_validation_precedes_submission(embedding_modules, dimension, store_dimensions, error):
    async def run(operation, *args):
        return operation(*args)

    clients = SimpleNamespace(resolve_for_use=lambda channel: ResolvedModel(
        "provider", SNAPSHOT["model"], SNAPSHOT["base_url"], "synthetic-secret", dimension),
        embedder=AsyncMock())
    jobs = SimpleNamespace(submit=AsyncMock())

    def scopes():
        return SimpleNamespace(user_id="admin"), None

    service = embedding_modules.service.EmbeddingRebuildService(
        SimpleNamespace(embedding_dimensions=store_dimensions), lambda user: clients, jobs,
        executor=SimpleNamespace(run=run), recovery=SimpleNamespace(require_ready=AsyncMock()), scopes_for=scopes,
    )
    scope, write_scope = scopes()
    with pytest.raises(error):
        await service._prepare(scope, write_scope)
    jobs.submit.assert_not_awaited()
    clients.embedder.assert_not_called()


async def test_rebuild_uses_fixed_embedding_config_and_recovery_scope(embedding_modules, monkeypatch):
    async def run(operation, *args):
        return operation(*args)

    config = ResolvedModel("provider", SNAPSHOT["model"], SNAPSHOT["base_url"], "synthetic-secret", 2)
    embedder = SimpleNamespace(embed=AsyncMock(), aclose=AsyncMock())
    borrowed = []

    @asynccontextmanager
    async def open_embedder(selected):
        borrowed.append(selected)
        try:
            yield embedder
        finally:
            await embedder.aclose()

    session = object()
    scopes = []

    @asynccontextmanager
    async def recovery_run(scope, **options):
        scopes.append((scope, options))
        yield session

    clients = SimpleNamespace(resolve_for_use=lambda channel: config, embedder=open_embedder)
    store = SimpleNamespace(embedding_dimensions={2})
    scope = SimpleNamespace(user_id="admin")
    execute = AsyncMock(return_value=5)
    monkeypatch.setattr(embedding_modules.service, "embed_all", execute)
    service = embedding_modules.service.EmbeddingRebuildService(
        store, lambda user: clients, SimpleNamespace(), executor=SimpleNamespace(run=run),
        recovery=SimpleNamespace(run=recovery_run), scopes_for=lambda: (scope, None),
    )
    snapshot, work = await service._prepare(scope, None)
    assert snapshot == SNAPSHOT and "synthetic-secret" not in repr(snapshot)
    progress = AsyncMock()
    assert await work(progress, "job", "rid", "previous-rid") == 5
    assert borrowed == [config]
    assert scopes == [(scope, {"task_type": "embedding", "task_id": "job", "rid": "rid", "previous_rid": "previous-rid"})]
    assert execute.await_args.kwargs["recovery_session"] is session
    embedder.aclose.assert_awaited_once()


def test_public_job_hides_attempt_and_owner_metadata(embedding_modules):
    public = {name: None for name in (
        "job_id", "status", "created_at", "started_at", "finished_at", "error", "progress", "config_snapshot",
    )}
    job = public | {"tenant_id": "tenant", "workspace_id": "workspace", "created_by": "admin",
                    "resume_count": 2, "heartbeat_at": 123, "executor_pod": "pod"}
    assert embedding_modules.service.public_job(job) == public
    assert embedding_modules.service.public_job(None) is None

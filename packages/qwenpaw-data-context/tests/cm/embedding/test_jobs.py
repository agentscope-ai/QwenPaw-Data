"""Verify workspace job progress, mutual exclusion and explicit retries after interruption."""

import asyncio
import json
import os
from uuid import uuid4

import pytest
from context_manager.db import close_database, initialize_database
from context_manager.embedding import EmbeddingJobManager, RebuildBusy
from context_manager.settings.db import DbSettings
from context_manager.settings.embedding import EmbeddingSettings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

OWNER = ("tenant-1", "workspace-1")
SNAPSHOT = {"model": "test-model", "base_url": "https://model.invalid/v1", "dim": 1024}


@pytest.fixture(autouse=True, params=["sqlite", "postgresql"])
def shared_database(request, tmp_path):
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    admin, schema = None, None
    if request.param == "postgresql":
        url = os.environ.get("CM_EMBED_TEST_PG_URL")
        if not url:
            pytest.skip(
                "Set CM_EMBED_TEST_PG_URL to an isolated PostgreSQL test database"
            )
        admin = create_engine(url)
        schema = "embedding_" + uuid4().hex
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = (
            make_url(url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
    try:
        initialize_database(DbSettings(url=url))
        yield
    finally:
        close_database()
        if admin is not None:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


async def finished(jobs, owner, job_id):
    for _ in range(200):
        job = await jobs.get(owner, job_id)
        if job["status"] in {"failed", "success"}:
            return job
        await asyncio.sleep(0.005)
    raise AssertionError("job did not finish")


@pytest.mark.asyncio
async def test_workspace_lock_is_shared_by_runners_while_other_workspaces_can_run(
    tmp_path,
):
    jobs, other = EmbeddingJobManager(), EmbeddingJobManager()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def work(progress, *_attempt):
        await progress(
            {
                "phase": "embedding",
                "current_label": "UserMemory",
                "labels_done": 0,
                "labels_total": 1,
            }
        )
        entered.set()
        await release.wait()
        return 1

    job_id = await jobs.submit(
        OWNER, {**SNAPSHOT, "api_key": "secret"}, work, created_by="user-1"
    )
    await entered.wait()
    assert (await other.get(OWNER, job_id))["status"] == "running"
    with pytest.raises(RebuildBusy) as error:
        await other.submit(OWNER, SNAPSHOT, work, created_by="user-1")
    assert error.value.job_id == job_id
    user2 = ("tenant-1", "workspace-2")
    other_id = await other.submit(user2, SNAPSHOT, work, created_by="user-1")
    assert (await other.get(user2, job_id)) is None
    release.set()
    result = await finished(jobs, OWNER, job_id)
    assert result["status"] == "success" and result["progress"]["phase"] == "done"
    assert result["config_snapshot"] == SNAPSHOT
    assert (await finished(other, user2, other_id))["status"] == "success"
    assert (await EmbeddingJobManager().latest(OWNER)) == result
    assert "secret" not in json.dumps(result)
    await jobs.aclose()
    await other.aclose()


@pytest.mark.asyncio
async def test_live_heartbeat_prevents_retry_between_progress_reports():
    settings = EmbeddingSettings()
    jobs, other = (
        EmbeddingJobManager(settings=settings),
        EmbeddingJobManager(settings=settings),
    )
    entered = asyncio.Event()

    async def work(progress, *_attempt):
        entered.set()
        await asyncio.Event().wait()

    try:
        job_id = await jobs.submit(OWNER, SNAPSHOT, work, created_by="admin")
        await entered.wait()
        await asyncio.sleep(0.9)
        with pytest.raises(RebuildBusy):
            await other.submit(OWNER, SNAPSHOT, work, created_by="admin", job_id=job_id)
        assert (await other.get(OWNER, job_id))["status"] == "running"
    finally:
        await other.aclose()
        await jobs.aclose()


@pytest.mark.asyncio
async def test_explicit_retry_takes_expired_job_and_old_worker_cannot_finish_it():
    from context_manager.db import get_database
    from context_manager.schema.relational import embedding_jobs
    from sqlalchemy import update

    jobs = EmbeddingJobManager()
    other = EmbeddingJobManager()
    old_release, new_release, resumed = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    entered = asyncio.Event()

    async def old_work(progress, *_attempt):
        entered.set()
        await old_release.wait()
        return 0

    async def new_work(progress, *_attempt):
        resumed.set()
        await new_release.wait()
        return 0

    try:
        job_id = await jobs.submit(OWNER, SNAPSHOT, old_work, created_by="admin")
        await entered.wait()
        # Simulate a disconnected worker whose execution lease has elapsed.
        with get_database().engine.begin() as connection:
            connection.execute(
                update(embedding_jobs)
                .where(embedding_jobs.c.job_id == job_id)
                .values(heartbeat_at=0)
            )
        await other.submit(OWNER, SNAPSHOT, new_work, created_by="admin", job_id=job_id)
        await asyncio.wait_for(resumed.wait(), timeout=2)
        old_release.set()
        await jobs.aclose()
        assert (await other.get(OWNER, job_id))["status"] == "running"
        new_release.set()
        assert (await finished(other, OWNER, job_id))["status"] == "success"
    finally:
        await jobs.aclose()
        await other.aclose()

"""Stateless contracts on one local SQLite file. Graph and model clients are not used."""

import time

import pytest
from sqlalchemy import insert, select

from context_manager.db import close_database, get_database, initialize_database
from context_manager.embedding.repository import EmbeddingJobRepository, rebuild_rid
from context_manager.knowledge.document import DocumentStatus
from context_manager.knowledge.repository import DocumentRepository
from context_manager.heartbeat import sweep_connection
from context_manager.schema.relational import (
    documents,
    embedding_jobs,
    kg_user_claims,
    recovery_runs,
    user_ingest_claims,
)
from context_manager.settings.db import DbSettings
from context_manager.user.loader import UserHistoryScope
from context_manager.user.repository import UserIngestRepository
from profiling.models.probe import STAGE_BIZ_DOMAIN
from profiling.tables import PROFILING_TABLE_LIST, probe_stage, probe_task
from semantic_config.tables import (
    SC_TABLE_LIST,
    explore_scope,
    metadata,
    weave_task,
)

from cm.knowledge.test_document_storage import document_for


@pytest.fixture
def control(tmp_path):
    database = initialize_database(
        DbSettings(url=f"sqlite:///{tmp_path / 'stateless.db'}")
    )
    with database.engine.begin() as connection:
        metadata.create_all(connection, tables=[*SC_TABLE_LIST, *PROFILING_TABLE_LIST])
    try:
        yield database.engine
    finally:
        close_database()


def _job(job_id: str, *, attempted_at: str = "2026-10-28T00:00:00+00:00") -> dict:
    return {
        "tenant_id": "t",
        "workspace_id": "w",
        "job_id": job_id,
        "created_by": "admin",
        "created_at": attempted_at,
        "attempted_at": attempted_at,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "error": None,
        "progress": {
            "phase": "pending",
            "current_label": "",
            "labels_done": 0,
            "labels_total": 0,
        },
        "config_snapshot": {
            "model": "m",
            "base_url": "https://model.invalid",
            "dim": 8,
        },
    }


def test_sweep_fails_only_stale_rows_and_leaves_rebuilds_and_ingest(control):
    now = time.time()
    with control.begin() as connection:
        connection.execute(
            insert(weave_task),
            [
                {
                    "task_id": "fresh-weave",
                    "status": "RUNNING",
                    "heartbeat_at": now,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
                {
                    "task_id": "stale-weave",
                    "status": "RUNNING",
                    "heartbeat_at": 0,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
                {
                    "task_id": "recovering-weave",
                    "status": "RUNNING",
                    "heartbeat_at": 0,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
                {
                    "task_id": "closed-recovery-weave",
                    "status": "RUNNING",
                    "heartbeat_at": 0,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
            ],
        )
        connection.execute(
            insert(recovery_runs),
            [
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "task_type": "weave",
                    "task_id": "recovering-weave",
                    "rid": "recovering-weave",
                    "recovery_state": "RECOVERING",
                    "heartbeat_at": now,
                    "executor_pod": "pod-a",
                },
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "task_type": "weave",
                    "task_id": "closed-recovery-weave",
                    "rid": "closed-recovery-weave",
                    "recovery_state": "CLOSED",
                    "result_status": "SUCCESS",
                    "heartbeat_at": now,
                    "executor_pod": "pod-a",
                },
            ],
        )
        connection.execute(
            insert(probe_task),
            [
                {
                    "task_id": "fresh-probe",
                    "datasource_id": "ds",
                    "stage": STAGE_BIZ_DOMAIN,
                    "status": "RUNNING",
                    "heartbeat_at": now,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
                {
                    "task_id": "stale-probe",
                    "datasource_id": "ds",
                    "stage": STAGE_BIZ_DOMAIN,
                    "status": "RUNNING",
                    "heartbeat_at": 0,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
            ],
        )
        connection.execute(
            insert(probe_stage),
            [
                {
                    "datasource_id": "ds",
                    "stage": STAGE_BIZ_DOMAIN,
                    "status": "PROBING",
                    "current_task_id": "fresh-probe",
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
                {
                    "datasource_id": "ds2",
                    "stage": STAGE_BIZ_DOMAIN,
                    "status": "PROBING",
                    "current_task_id": "stale-probe",
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
            ],
        )
        connection.execute(
            insert(explore_scope),
            [
                {
                    "datasource_id": "fresh",
                    "last_status": "running",
                    "heartbeat_at": now,
                    "run_epoch": 1,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
                {
                    "datasource_id": "stale",
                    "last_status": "running",
                    "heartbeat_at": 0,
                    "run_epoch": 1,
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "is_deleted": 0,
                },
            ],
        )
        connection.execute(
            insert(kg_user_claims),
            [
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "user_id": "fresh-user",
                    "heartbeat_at": now,
                    "run_epoch": 1,
                },
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "user_id": "stale-user",
                    "heartbeat_at": 0,
                    "run_epoch": 2,
                },
            ],
        )
        connection.execute(
            insert(documents),
            [
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "user_id": "fresh-user",
                    "filename": "fresh.txt",
                    "document_id": "document:fresh",
                    "byte_size": 1,
                    "status": "processing",
                    "created_at": "t0",
                    "updated_at": "t0",
                    "run_epoch": 1,
                },
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "user_id": "stale-user",
                    "filename": "stale.txt",
                    "document_id": "document:stale",
                    "byte_size": 1,
                    "status": "processing",
                    "created_at": "t0",
                    "updated_at": "t0",
                    "run_epoch": 2,
                },
                {
                    "tenant_id": "t",
                    "workspace_id": "w",
                    "user_id": "stale-user",
                    "filename": "deleting.txt",
                    "document_id": "document:deleting",
                    "byte_size": 1,
                    "status": "deleting",
                    "created_at": "t0",
                    "updated_at": "t0",
                    "run_epoch": 2,
                },
            ],
        )
        connection.execute(
            insert(embedding_jobs).values(
                tenant_id="t",
                workspace_id="w",
                job_id="rebuild",
                created_by="admin",
                status="running",
                created_at="t0",
                attempted_at="t0",
                progress="{}",
                config_snapshot="{}",
                heartbeat_at=0,
                resume_count=1,
            )
        )
        connection.execute(
            insert(user_ingest_claims).values(
                tenant_id="t",
                workspace_id="w",
                user_id="ingest",
                heartbeat_at=0,
                run_epoch=1,
            )
        )
        sweep_connection(connection)

    def status(table, column, **keys):
        with control.connect() as connection:
            return connection.execute(
                select(table.c[column]).where(
                    *(table.c[name] == value for name, value in keys.items())
                )
            ).scalar_one()

    assert status(weave_task, "status", task_id="fresh-weave") == "RUNNING"
    assert status(weave_task, "status", task_id="stale-weave") == "FAILED"
    assert status(weave_task, "error_msg", task_id="stale-weave") == "执行中断"
    assert status(weave_task, "status", task_id="recovering-weave") == "RUNNING"
    assert status(weave_task, "status", task_id="closed-recovery-weave") == "FAILED"
    assert status(probe_task, "status", task_id="fresh-probe") == "RUNNING"
    assert status(probe_task, "status", task_id="stale-probe") == "FAILED"
    assert (
        status(probe_stage, "status", datasource_id="ds", stage=STAGE_BIZ_DOMAIN)
        == "PROBING"
    )
    assert (
        status(probe_stage, "status", datasource_id="ds2", stage=STAGE_BIZ_DOMAIN)
        == "NOT_STARTED"
    )
    assert status(explore_scope, "last_status", datasource_id="fresh") == "running"
    assert status(explore_scope, "last_status", datasource_id="stale") == "failed"
    assert status(documents, "status", document_id="document:fresh") == "processing"
    assert status(documents, "status", document_id="document:stale") == "failed"
    assert status(documents, "ingest_error", document_id="document:stale") == "执行中断"
    assert status(documents, "status", document_id="document:deleting") == "deleting"
    assert status(embedding_jobs, "status", job_id="rebuild") == "running"
    assert status(user_ingest_claims, "heartbeat_at", user_id="ingest") == 0
    assert status(kg_user_claims, "heartbeat_at", user_id="fresh-user") == now
    assert status(kg_user_claims, "heartbeat_at", user_id="stale-user") is None


def test_embedding_fence_retry_window_and_recovery_receipt(control):
    del control
    repository = EmbeddingJobRepository()
    count, previous = repository.reserve(_job("job"), retry=False)
    assert (count, previous) == (0, None)
    with get_database().engine.begin() as connection:
        connection.execute(
            embedding_jobs.update()
            .where(embedding_jobs.c.job_id == "job")
            .values(status="failed", error="执行中断", heartbeat_at=0)
        )
    assert not repository.mark_running(("t", "w"), "job", 0)
    assert not repository.finish(
        ("t", "w"),
        "job",
        0,
        status="success",
        progress={"phase": "done"},
    )
    stored = repository.get(("t", "w"), "job")
    assert stored["status"] == "failed"
    assert stored["error"] == "执行中断"

    with get_database().engine.begin() as connection:
        connection.execute(
            embedding_jobs.update()
            .where(embedding_jobs.c.job_id == "job")
            .values(resume_count=2)
        )
    resumed, _ = repository.reserve(_job("job"), retry=True)
    assert resumed == 0
    with get_database().engine.begin() as connection:
        connection.execute(
            embedding_jobs.update()
            .where(embedding_jobs.c.job_id == "job")
            .values(heartbeat_at=0, status="running")
        )
        connection.execute(
            insert(recovery_runs).values(
                tenant_id="t",
                workspace_id="w",
                task_type="embedding",
                task_id="job",
                rid=rebuild_rid("job", "2026-10-28T00:00:00+00:00"),
                recovery_state="ACTIVE",
            )
        )
    assert repository.claim_stale() == []
    assert repository.get(("t", "w"), "job")["status"] == "running"
    with get_database().engine.begin() as connection:
        connection.execute(
            recovery_runs.update()
            .where(recovery_runs.c.task_id == "job")
            .values(recovery_state="CLOSED", result_status="SUCCESS")
        )
    projected = repository.get(("t", "w"), "job")
    assert projected["status"] == "success"
    assert projected["progress"]["phase"] == "done"


def test_document_delete_keeps_a_row_whose_status_changed(control):
    repository = DocumentRepository(
        control, tenant_id="t", workspace_id="w", user_id="owner"
    )
    document = document_for()
    token = repository.claim(document.document_id)
    with repository.transaction() as connection:
        repository.create(connection, document, token=token)
    repository.set_status(document.document_id, DocumentStatus.PROCESSING, token=token)
    repository.delete(document.document_id, token=token, status="ready")
    assert (
        repository.get(document.document_id).metadata.status
        is DocumentStatus.PROCESSING
    )
    repository.delete(document.document_id, token=token, status="processing")
    with pytest.raises(FileNotFoundError):
        repository.get(document.document_id)


def test_user_ingest_claims_are_per_user_and_release_blocks_refresh(control):
    del control
    users = UserIngestRepository()
    first = UserHistoryScope("t", "w", "alice")
    second = UserHistoryScope("t", "w", "bob")
    alice = users.claim(first)
    bob = users.claim(second)
    assert alice and bob
    assert users.claim(first) is None
    assert users.renew(first, alice)
    users.release(first, alice)
    assert not users.renew(first, alice)
    assert users.claim(second) is None
    again = users.claim(first)
    assert again and again != alice

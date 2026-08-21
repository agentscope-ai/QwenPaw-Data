from __future__ import annotations

import multiprocessing
import time
from pathlib import Path

from qwenpaw_data.context.job_store import PersistentJobStore


def _write_job(path: str, job_id: str) -> None:
    PersistentJobStore(Path(path)).put(
        "imports",
        job_id,
        status="succeeded",
        payload={"job_id": job_id},
    )


def test_jobs_survive_store_recreation(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    PersistentJobStore(path).put(
        "imports",
        "job-1",
        status="queued",
        payload={"value": 1},
        idempotency_key="request-1",
        max_attempts=2,
    )

    restored = PersistentJobStore(path).get("imports", "job-1")
    assert restored is not None
    assert restored.payload == {"value": 1}
    assert restored.idempotency_key == "request-1"


def test_idempotency_key_returns_original_job(tmp_path: Path) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    original = store.put(
        "imports",
        "job-1",
        status="queued",
        payload={"value": 1},
        idempotency_key="request-1",
    )
    duplicate = store.put(
        "imports",
        "job-2",
        status="queued",
        payload={"value": 2},
        idempotency_key="request-1",
    )
    assert duplicate.job_id == original.job_id
    assert duplicate.payload == {"value": 1}


def test_multiple_processes_do_not_overwrite_each_other(tmp_path: Path) -> None:
    path = tmp_path / "jobs.db"
    PersistentJobStore(path)
    processes = [
        multiprocessing.Process(target=_write_job, args=(str(path), f"job-{index}"))
        for index in range(8)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    assert len(PersistentJobStore(path).list("imports")) == 8


def test_expired_plan_is_evicted(tmp_path: Path) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    store.put(
        "plans",
        "expired",
        status="ready",
        payload={},
        ttl_seconds=0.01,
    )
    time.sleep(0.02)
    assert store.get("plans", "expired") is None


def test_lease_recovery_requeues_then_exhausts(tmp_path: Path) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    store.put(
        "jobs",
        "retryable",
        status="queued",
        payload={},
        max_attempts=2,
    )
    first = store.claim("jobs", worker_id="worker-1", lease_seconds=0.01)
    assert first is not None and first.attempt == 1
    time.sleep(0.02)
    assert store.recover_stale("jobs") == (1, 0)

    second = store.claim("jobs", worker_id="worker-2", lease_seconds=0.01)
    assert second is not None and second.attempt == 2
    time.sleep(0.02)
    assert store.recover_stale("jobs") == (0, 1)
    final = store.get("jobs", "retryable")
    assert final is not None and final.status == "failed"


def test_plan_consumption_is_single_winner(tmp_path: Path) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    store.put("plans", "plan-1", status="ready", payload={})
    assert store.transition(
        "plans",
        "plan-1",
        expected={"ready"},
        status="consumed",
    )
    assert not store.transition(
        "plans",
        "plan-1",
        expected={"ready"},
        status="consumed",
    )


def test_restart_marks_unleased_running_work_failed(tmp_path: Path) -> None:
    store = PersistentJobStore(tmp_path / "jobs.db")
    store.put("imports", "job-1", status="running", payload={})
    assert store.recover_interrupted() == 1
    restored = store.get("imports", "job-1")
    assert restored is not None
    assert restored.status == "failed"
    assert restored.error == "service restarted during execution"

"""Protect receipt ownership, replacement and bounded discovery with SQLite."""

import time
from types import SimpleNamespace

import pytest
from context_manager.executor import BoundedExecutor
from context_manager.settings import get_settings
from context_manager.settings.execution import PoolSettings
from context_manager.store import StoreError
from context_manager.transaction.recovery import RecoveryService


def identity(task_id="task-1", rid="attempt-1", workspace_id="workspace-1"):
    return {
        "tenant_id": "tenant-1",
        "workspace_id": workspace_id,
        "task_type": "test",
        "task_id": task_id,
        "rid": rid,
    }


def test_discovery_pages_and_admission_distinguish_live_work_from_recovery(runs):
    for index in range(103):
        runs.register(identity(task_id=f"task-{index:03}", rid=f"attempt-{index:03}"))
    first = runs.discover()
    second = runs.discover(
        after=tuple(
            first[-1][name]
            for name in ("tenant_id", "workspace_id", "task_type", "task_id")
        )
    )
    assert len(first) == 100
    assert [row["task_id"] for row in second] == ["task-100", "task-101", "task-102"]
    assert runs.has_pending("tenant-1", "workspace-1")
    assert not runs.has_pending("tenant-1", "other-workspace")
    now = time.time()
    for row in (*first, *second):
        runs.update(row, values={"heartbeat_at": now}, states=("REGISTERED",))
    assert not runs.has_pending("tenant-1", "workspace-1")
    interrupted = identity(task_id="task-000", rid="attempt-000")
    runs.update(
        interrupted,
        values={"recovery_state": "RECOVERING", "heartbeat_at": now},
        states=("REGISTERED",),
    )
    assert runs.has_pending("tenant-1", "workspace-1")
    runs.update(
        interrupted, values={"recovery_state": "CLOSED"}, states=("RECOVERING",)
    )
    assert not runs.has_pending("tenant-1", "workspace-1")
    assert all(row["task_id"] != "task-000" for row in runs.discover())


async def test_admission_uses_shared_heartbeat_across_services(runs):
    scope = SimpleNamespace(tenant_id="tenant-1", workspace_id="workspace-1")
    executor = BoundedExecutor("admission-test", PoolSettings(workers=1))
    owner = RecoveryService(runs, executor, lambda identity: None)
    other = RecoveryService(runs, executor, lambda identity: None)
    live = identity(task_id="live", rid="live-rid")
    key = ("tenant-1", "workspace-1", "test", "live")
    try:
        runs.register(live, live=True)
        # This process's in-memory execution must not hide the shared row.
        owner._active[key] = "execution"
        await other.require_ready(scope)
        runs.update(live, values={"recovery_state": "ACTIVE"}, states=("REGISTERED",))
        await owner.require_ready(scope)

        stale = time.time() - get_settings().heartbeat.stale_after_seconds - 1
        runs.update(live, values={"heartbeat_at": stale}, states=("ACTIVE",))
        with pytest.raises(StoreError, match="unfinished recovery") as blocked:
            await other.require_ready(scope)
        assert blocked.value.code == "recovery_workspace_busy"
        with pytest.raises(StoreError, match="unfinished recovery"):
            await owner.require_ready(scope)

        runs.update(
            live,
            values={"recovery_state": "RECOVERING", "heartbeat_at": time.time()},
            states=("ACTIVE",),
        )
        with pytest.raises(StoreError, match="unfinished recovery"):
            await other.require_ready(scope)
        runs.update(
            live,
            values={"recovery_state": "CLEANING", "heartbeat_at": time.time()},
            states=("RECOVERING",),
        )
        with pytest.raises(StoreError, match="unfinished recovery"):
            await other.require_ready(scope)
        runs.update(live, values={"recovery_state": "CLOSED"}, states=("CLEANING",))
        await other.require_ready(scope)

        runs.register(identity(task_id="abandoned", rid="abandoned-rid"))
        with pytest.raises(StoreError, match="unfinished recovery"):
            await other.require_ready(scope)
    finally:
        owner._active.pop(key, None)
        await owner.aclose()
        await other.aclose()
        await executor.aclose()

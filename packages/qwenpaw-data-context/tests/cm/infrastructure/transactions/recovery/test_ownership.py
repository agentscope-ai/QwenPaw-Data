"""Keep one process's recovery close from sharing a stack with another process."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from context_manager.executor import BoundedExecutor
from context_manager.schema.relational import recovery_runs
from context_manager.settings import get_settings
from context_manager.settings.execution import PoolSettings
from context_manager.store import StoreError
from context_manager.transaction.recovery.session import RecoveryStackSession
from sqlalchemy import update


class _Stack:
    """Stand in for the graph stack. SQL ownership is the behavior under test."""

    def __init__(self, on_first_undo=None):
        self.undos = []
        self.on_first_undo = on_first_undo

    def create(self, *, state="ACTIVE", result_status=None):
        del state, result_status

    def head(self):
        done = len(self.undos) >= 2
        return SimpleNamespace(
            properties={
                "recovery_state": "ROLLED_BACK" if done else "ACTIVE",
                "head_seq": 0 if done else 2,
                "result_status": "FAILED",
                "message": None,
            }
        )

    def decide(self, status, message):
        return {
            "recovery_state": "RECOVERING",
            "head_seq": 2,
            "result_status": status,
            "message": message,
        }

    def undo_batch(self, sequence):
        self.undos.append(sequence)
        if sequence == 2 and self.on_first_undo is not None:
            self.on_first_undo()

    def cleanup_batch(self):
        return True


def _identity():
    return {
        "tenant_id": "tenant-1",
        "workspace_id": "workspace-1",
        "task_type": "weave",
        "task_id": "task-1",
        "rid": "task-1",
    }


@pytest.mark.asyncio
async def test_close_holds_the_run_past_the_stale_window(runs, monkeypatch):
    heartbeat = get_settings().heartbeat
    monkeypatch.setattr(heartbeat, "interval_seconds", 0.05)
    monkeypatch.setattr(heartbeat, "stale_after_seconds", 0.2)
    identity = _identity()
    started = threading.Event()

    def outlast_the_window():
        started.set()
        time.sleep(0.6)

    stack = _Stack(on_first_undo=outlast_the_window)
    executor = BoundedExecutor("recovery-owner", PoolSettings(workers=2))
    session = RecoveryStackSession(stack, runs, executor, identity, None)

    async def claim_during_close():
        assert await asyncio.to_thread(started.wait, 5)
        await asyncio.sleep(0.35)
        assert await asyncio.to_thread(runs.claim_abandoned, identity) is False

    watcher = asyncio.create_task(claim_during_close())
    try:
        await session.__aenter__()
        await session.__aexit__(None, None, None)
        await watcher
    finally:
        await executor.aclose()
    assert stack.undos == [2, 1]
    assert runs.get(identity)["recovery_state"] == "CLOSED"


@pytest.mark.asyncio
async def test_lost_owner_stops_before_the_next_undo(runs):
    identity = _identity()

    def steal():
        with runs.engine.begin() as connection:
            connection.execute(
                update(recovery_runs)
                .where(recovery_runs.c.task_id == identity["task_id"])
                .values(executor_pod="other-pod", heartbeat_at=time.time())
            )

    stack = _Stack(on_first_undo=steal)
    executor = BoundedExecutor("recovery-owner", PoolSettings(workers=2))
    session = RecoveryStackSession(stack, runs, executor, identity, None)
    try:
        await session.__aenter__()
        with pytest.raises(StoreError) as error:
            await session.__aexit__(None, None, None)
    finally:
        await executor.aclose()
    assert error.value.code == "recovery_fenced"
    assert stack.undos == [2]
    row = runs.get(identity)
    assert row["recovery_state"] == "RECOVERING"
    assert row["executor_pod"] == "other-pod"

"""Exercise Runtime recovery wiring with real SQLite and an empty graph driver.

The driver substitutes graph I/O only; Scope issuance, routing, SQL receipts,
the recovery scanner and shutdown use their production implementations.
"""

import asyncio

import pytest
from context_manager.db import get_database, initialize_database
from context_manager.runtime import WorkspaceRuntime
from context_manager.runtime.startup import (
    create_context_runtime,
)
from context_manager.settings import get_settings
from context_manager.settings.cron import CronSettings
from context_manager.transaction.recovery.repository import RecoveryRunRepository
from qwenpaw_data_auth.database import init_schema

from cm.infrastructure.lifecycle.test_resolver import _DriverFactory, _seed_workspace


def pending_cleanup(runs, *, tenant_id, workspace_id, task_type="unknown-task"):
    identity = {
        "tenant_id": tenant_id,
        "workspace_id": workspace_id,
        "task_type": task_type,
        "task_id": "task",
        "rid": "attempt",
    }
    runs.register(identity)
    # Model the durable window after graph logs were deleted but before CLOSED.
    runs.update(
        identity,
        values={"recovery_state": "CLEANING", "result_status": "SUCCESS"},
        states=("REGISTERED",),
    )
    return identity


@pytest.mark.asyncio
async def test_startup_recovers_registered_targets_without_users_or_models(
    tmp_path, monkeypatch
):
    dsn = f"sqlite:///{tmp_path / 'control.db'}"
    get_settings().db.url = dsn
    init_schema(dsn)
    database = initialize_database()
    secret = tmp_path / "graph.json"
    secret.write_text('{"user":"neo4j","password":"test-password"}')
    runs = RecoveryRunRepository(database.engine)
    identities = []
    for suffix in ("one", "two"):
        _seed_workspace(
            dsn,
            tenant_id=f"tenant-{suffix}",
            workspace_id=f"workspace-{suffix}",
            endpoint_ref=f"bolt://{suffix}.invalid",
            secret_ref=f"file://{secret}",
        )
        identities.append(
            pending_cleanup(
                runs, tenant_id=f"tenant-{suffix}", workspace_id=f"workspace-{suffix}"
            )
        )
    wrong_target = pending_cleanup(
        runs, tenant_id="tenant-one", workspace_id="workspace-two"
    )
    completed = set()
    finished = asyncio.Event()
    loop = asyncio.get_running_loop()
    update = RecoveryRunRepository.update

    def observe_receipt(self, identity, *, values, states, owner=None):
        update(self, identity, values=values, states=states, owner=owner)
        if values["recovery_state"] == "CLOSED":
            completed.add((identity["tenant_id"], identity["workspace_id"]))
            if len(completed) == 2:
                loop.call_soon_threadsafe(finished.set)

    def reject_models(*args, **kwargs):
        pytest.fail("background recovery must not load user model configuration")

    monkeypatch.setattr(RecoveryRunRepository, "update", observe_receipt)
    monkeypatch.setattr(WorkspaceRuntime, "models", reject_models)
    factory = _DriverFactory()
    runtime = create_context_runtime(
        driver_factory=factory, cron_settings=CronSettings(enabled=False)
    )
    try:
        await runtime.start()
        await asyncio.wait_for(finished.wait(), 5)
        for identity in identities:
            row = runs.get(identity)
            assert (row["recovery_state"], row["result_status"]) == (
                "CLOSED",
                "SUCCESS",
            )
        assert runs.get(wrong_target)["recovery_state"] == "CLEANING"
        assert [uri for uri, _ in factory.calls] == [
            "bolt://one.invalid",
            "bolt://two.invalid",
        ]
        assert [
            driver.session_options[-1]["database"] for driver in factory.drivers
        ] == ["tenant-one-database", "tenant-two-database"]
    finally:
        await runtime.aclose()
    assert get_database() is database  # Embedded Runtime borrows the SQL pool.
    assert all(driver.close_count == 1 for driver in factory.drivers)

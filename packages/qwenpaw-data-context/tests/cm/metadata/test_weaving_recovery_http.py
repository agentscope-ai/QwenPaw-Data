"""Drive HTTP -> SQL -> weaving -> recovery with synthetic data and graph/model fakes.

Runtime, authorization, leases, recovery sessions and Undo use production code.
The in-memory graph does not establish real database locking or crash durability.
"""

import asyncio

import pytest
from context_manager.auth import GLOBAL_OWNER_ID
from context_manager.db import get_database
from context_manager.schema import DATASET, DATASET_FIELD, UNDO_BATCH
from context_manager.schema.relational import sync_log
from context_manager.settings import clear_settings
from context_manager.transaction.recovery import RecoveryService
from context_manager.transaction.recovery.repository import RecoveryRunRepository
from semantic_config import tables
from semantic_config.services import weave_task_service

from cm.metadata.recovery_support import assert_closed, facts, rows
from cm.metadata.test_weaving import (
    editor as editor,  # noqa: PLC0414 -- pytest fixture export
)
from cm.metadata.test_weaving import (
    editor_database as editor_database,  # noqa: PLC0414 -- pytest fixture export
)
from cm.metadata.test_weaving import seed


@pytest.fixture
async def recovered_editor(editor, monkeypatch):  # noqa: F811 - imported pytest fixture
    # Small batches expose committed intermediate work with a compact fixture.
    monkeypatch.setenv("CM_GRAPH_WRITE_BATCH_MAX_ROWS", "2")
    clear_settings()
    # Drive scans explicitly so injected persistence failures have no timer races.
    monkeypatch.setattr(RecoveryService, "start", lambda self: None)
    await editor.runtime.start()
    editor.publisher = editor.runtime.metadata_weaving(editor.scope)
    editor.engine = get_database().engine
    editor.control = editor.store.access.issue_admin_scope(
        editor.identity["value"], owner_id=GLOBAL_OWNER_ID, zone="_internal"
    )
    return editor


async def finish_tasks():
    await asyncio.wait_for(
        asyncio.gather(*tuple(weave_task_service._BACKGROUND_TASKS)), 10
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["model", "graph"])
async def test_http_rekey_failure_keeps_sql_and_prior_commits_then_full_retry_succeeds(
    recovered_editor, monkeypatch, failure
):
    w = recovered_editor
    data = await seed(w)
    before = facts(w)
    count = len(rows(w))
    embed, create = w.caller.embed, w.store.create_nodes
    calls = 0
    injected = False

    async def fail_model(texts, **options):
        nonlocal calls, injected
        calls += 1
        if calls == 2:
            injected = True
            assert facts(w) != before
            raise RuntimeError("synthetic later model failure")
        return await embed(texts, **options)

    def fail_graph(spec, properties, *, scope):
        nonlocal injected
        result = create(spec, properties, scope=scope)
        if spec is DATASET_FIELD and not injected:
            injected = True
            assert not w.store.list_nodes(UNDO_BATCH, scope=w.control)
            raise RuntimeError("synthetic graph failure after writing rows")
        return result

    path = f"/dataset-meta/{data['dataset']['dataset_id']}"
    with monkeypatch.context() as patch:
        if failure == "model":
            patch.setattr(w.caller, "embed", fail_model)
        else:
            patch.setattr(w.store, "create_nodes", fail_graph)
        response = await w.request(
            "PUT", path, json={"dataset_name": "orders_v2"}, expected=503
        )
    assert injected
    assert "配置已保存" in response["detail"]
    assert (await w.request("GET", path))["dataset_name"] == "orders_v2"
    assert facts(w) != before
    assert (await w.rows(sync_log))[-1]["status"] == "failed"
    assert_closed(w, *(["SUCCESS"] * count))

    task = await w.request(
        "POST",
        "/weave-task/submit",
        json={
            "datasource_id": data["datasource"],
            "task_name": "synthetic-retry",
            "weave_mode": "FULL",
        },
    )
    await finish_tasks()
    assert (await w.request("GET", f"/weave-task/{task['task_id']}"))[
        "status"
    ] == "SUCCESS"
    assert [
        row.properties["name"] for row in w.store.list_nodes(DATASET, scope=w.scope)
    ] == ["orders_v2"]
    assert {
        row.properties["dataset"]
        for row in w.store.list_nodes(DATASET_FIELD, scope=w.scope)
    } == {"orders_v2"}
    assert_closed(w, *(["SUCCESS"] * count), "SUCCESS")


@pytest.mark.asyncio
async def test_http_kill_reverses_committed_batches_before_terminal_status(
    recovered_editor, monkeypatch
):
    w = recovered_editor
    data = await seed(w)
    before = facts(w)
    closed_before = w.caller.close_count
    count = len(rows(w))
    entered, release = asyncio.Event(), asyncio.Event()
    embed = w.caller.embed
    calls = 0

    async def hold_later(texts, **options):
        nonlocal calls
        calls += 1
        if calls == 2:
            assert facts(w) != before
            entered.set()
            await release.wait()
        return await embed(texts, **options)

    monkeypatch.setattr(w.caller, "embed", hold_later)
    task = await w.request(
        "POST",
        "/weave-task/submit",
        json={
            "datasource_id": data["datasource"],
            "task_name": "synthetic-cancel",
            "weave_mode": "FULL",
        },
    )
    try:
        await asyncio.wait_for(entered.wait(), 5)
        response = await w.request("POST", f"/weave-task/{task['task_id']}/kill")
        assert response["status"] == "RUNNING"
        assert response["cancel_requested"] is True
    finally:
        release.set()
        await finish_tasks()
    assert (await w.request("GET", f"/weave-task/{task['task_id']}"))[
        "status"
    ] == "KILLED"
    assert facts(w) == before
    assert w.caller.close_count == closed_before + 1
    assert_closed(w, *(["SUCCESS"] * count), "KILLED")

    listed = await w.request(
        "GET", "/weave-task", params={"task_name": "synthetic-cancel"}
    )
    assert listed["total"] == 1 and listed["records"][0]["status"] == "KILLED"
    await w.request("POST", f"/weave-task/{task['task_id']}/kill", expected=400)


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeded", [True, False])
async def test_task_waits_for_recovery_then_projects_its_durable_result(
    recovered_editor, monkeypatch, succeeded
):
    w = recovered_editor
    data = await seed(w)
    before = facts(w)
    update, embed = RecoveryRunRepository.update, w.caller.embed
    calls = 0

    def unavailable(self, identity, *, values, states, owner=None):
        if values["recovery_state"] in {"RECOVERING", "CLEANING"}:
            raise RuntimeError("recovery SQL temporarily unavailable")
        return update(self, identity, values=values, states=states, owner=owner)

    async def fail_later(texts, **options):
        nonlocal calls
        calls += 1
        if calls == 2 and not succeeded:
            assert facts(w) != before
            raise RuntimeError("later provider failure")
        return await embed(texts, **options)

    monkeypatch.setattr(RecoveryRunRepository, "update", unavailable)
    monkeypatch.setattr(w.caller, "embed", fail_later)
    request = {
        "datasource_id": data["datasource"],
        "task_name": "pending-recovery",
        "weave_mode": "FULL",
    }
    task = await w.request("POST", "/weave-task/submit", json=request)
    await finish_tasks()
    task_id = task["task_id"]
    committed = facts(w)
    saved = await w.request("GET", f"/weave-task/{task_id}")
    assert saved["status"] == "RUNNING" and "图恢复尚未完成" in saved["error_msg"]
    assert (await w.request("GET", "/weave-task"))["records"][0]["status"] == "RUNNING"
    assert rows(w, tables.weave_task)[0]["status"] == "RUNNING"
    await w.request("POST", "/weave-task/submit", json=request, expected=400)
    assert len(rows(w, tables.weave_task)) == 1

    monkeypatch.setattr(RecoveryRunRepository, "update", update)

    async def no_models(*args, **kwargs):
        raise AssertionError("recovery must not call models")

    monkeypatch.setattr(w.caller, "embed", no_models)
    # A fresh service knows only persistent SQL/graph state, not the old Session.
    # This simulates service restart; it does not establish OS crash durability.
    recovery = RecoveryService(
        RecoveryRunRepository(w.engine),
        w.runtime._execution.background,
        lambda identity: w.runtime._workspace_target(
            identity["tenant_id"], identity["workspace_id"]
        ).recovery_stack(identity["rid"]),
    )
    try:
        await recovery.recover_pending()
    finally:
        await recovery.aclose()
    [receipt] = [row for row in rows(w) if row["task_id"] == task_id]
    expected = "SUCCESS" if succeeded else "FAILED"
    assert receipt["recovery_state"] == "CLOSED"
    assert receipt["result_status"] == expected
    assert rows(w, tables.weave_task)[0]["status"] == "RUNNING"
    # Pagination itself consumes the receipt, including without a detail GET.
    saved = (await w.request("GET", "/weave-task"))["records"][0]
    assert saved["status"] == expected
    assert saved["error_msg"] == (None if succeeded else "later provider failure")
    assert rows(w, tables.weave_task)[0]["status"] == expected
    assert (await w.request("GET", f"/weave-task/{task_id}"))["status"] == expected
    assert facts(w) == (committed if succeeded else before)

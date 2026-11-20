"""Exercise HTTP rebuild recovery with real SQLite and mocked graph/model I/O."""

import asyncio
from copy import deepcopy

import pytest
from context_manager.auth.repository import OwnerRepository
from context_manager.db import get_database
from context_manager.model.embedding import OpenAIEmbedCaller
from context_manager.schema.relational import (
    embedding_jobs,
    recovery_runs,
)
from context_manager.settings import clear_settings
from context_manager.transaction.recovery.repository import RecoveryRunRepository
from sqlalchemy import select

from cm.embedding.test_http_jobs import CURRENT, setup_client, wait_job


@pytest.fixture
def rebuild_http(tmp_path, monkeypatch):
    monkeypatch.setenv("CM_GRAPH_WRITE_BATCH_MAX_ROWS", "1")
    clear_settings()
    client, store, _auth, control = setup_client(tmp_path, monkeypatch)
    for index, values in enumerate(store.nodes.values()):
        values.update(embedding=(9.0 + index,) * 1024, embedding_hash=f"old-{index}")
    before = deepcopy(list(store.nodes.values()))
    return client, store, before, control


def rows(table):
    with get_database().engine.connect() as connection:
        return [dict(row) for row in connection.execute(select(table)).mappings()]


def business_nodes(store):
    return [
        values
        for (spec, _), values in store.nodes.items()
        if spec.zone.value != "_internal"
    ]


async def drain_workers(runtime):
    await asyncio.wait_for(asyncio.gather(*tuple(runtime.embedding_jobs._tasks)), 5)


def assert_closed(store, job_id, result):
    [receipt] = rows(recovery_runs)
    assert receipt["task_id"] == job_id
    assert receipt["recovery_state"] == "CLOSED"
    assert receipt["result_status"] == result
    assert all(spec.zone.value != "_internal" for spec, _ in store.nodes)
    assert rows(embedding_jobs)[0]["status"] in {"success", "failed", "running"}
    return receipt


@pytest.mark.parametrize("succeeded", [False, True])
def test_http_pending_recovery_blocks_new_jobs_and_survives_runtime_restart(
    rebuild_http, monkeypatch, tmp_path, succeeded
):
    client, store, before, _ = rebuild_http
    update = RecoveryRunRepository.update
    embed = OpenAIEmbedCaller.embed
    calls = 0

    def fail_receipt(self, identity, *, values, states, owner=None):
        if values["recovery_state"] in {"RECOVERING", "CLEANING"}:
            raise RuntimeError("SQL receipt update unavailable")
        return update(self, identity, values=values, states=states, owner=owner)

    async def fail_model(self, texts, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2 and not succeeded:
            raise RuntimeError("second page model failure")
        return await embed(self, texts, **kwargs)

    monkeypatch.setattr(RecoveryRunRepository, "update", fail_receipt)
    monkeypatch.setattr(OpenAIEmbedCaller, "embed", fail_model)
    with client:
        response = client.post(f"{CURRENT}/embed-all")
        assert response.status_code == 200, response.text
        job_id = response.json()["job_id"]
        client.portal.call(drain_workers, client.app.state.context_runtime)
        job = client.get(f"{CURRENT}/jobs/{job_id}").json()
        assert job["status"] == "running"
        assert job["progress"]["phase"] == "recovering"
        assert rows(embedding_jobs)[0]["status"] == "running"
        [receipt] = rows(recovery_runs)
        response = client.post(f"{CURRENT}/embed-all")
        assert response.status_code == 409
        assert response.json()["detail"]["error"]["code"] == "recovery_workspace_busy"
        assert client.post(f"{CURRENT}/jobs/{job_id}/retry").status_code == 400
        assert len(rows(embedding_jobs)) == 1
        assert rows(recovery_runs)[0]["rid"] == receipt["rid"]
        expected = deepcopy(business_nodes(store)) if succeeded else before

    # Simulate persisted graph contents across a new app/Runtime instance. SQLite
    # is reopened from disk; the graph substitute is reloaded, not an OS crash.
    saved_nodes = {key: deepcopy(values) for key, values in store.nodes.items()}
    monkeypatch.setattr(RecoveryRunRepository, "update", update)
    monkeypatch.setattr(OpenAIEmbedCaller, "embed", embed)
    restarted, restored, _, control = setup_client(tmp_path, monkeypatch)
    restored.nodes = saved_nodes
    control["missing"] = True

    def forbid_user_lookup(*args):
        pytest.fail("system recovery must not reload the initiating user")

    monkeypatch.setattr(OwnerRepository, "list_workspace_roles", forbid_user_lookup)
    with restarted:
        result = wait_job(restarted, job_id)
        assert result["status"] == ("success" if succeeded else "failed")
        assert business_nodes(restored) == expected
        closed = assert_closed(restored, job_id, "SUCCESS" if succeeded else "FAILED")
        assert closed["rid"] == receipt["rid"]
        assert rows(embedding_jobs)[0]["status"] == result["status"]
        assert control["resolved"] == control["calls"] == []

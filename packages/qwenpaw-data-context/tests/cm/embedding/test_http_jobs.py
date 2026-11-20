"""Run the legacy and canonical job APIs through real embedding orchestration."""

import asyncio
import time
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from context_manager.api.app import create_api_app
from context_manager.api.dependencies import (
    require_context_authorization,
    require_context_runtime,
)
from context_manager.auth import WriteOwner
from context_manager.db import close_database, initialize_database
from context_manager.model import (
    ModelConfigError,
    ModelConfigRepository,
    ModelService,
    ResolvedModel,
)
from context_manager.model.embedding_cache import EmbeddingCache
from context_manager.runtime.resolver import WorkspaceResolver
from context_manager.runtime.startup import create_context_runtime
from context_manager.runtime.workspace import WorkspaceRuntime
from context_manager.schema import ENTITY
from context_manager.settings.db import DbSettings
from fastapi.testclient import TestClient

from cm.embedding.support import RecoveryMemoryStore, node
from cm.embedding.test_jobs import OWNER, SNAPSHOT
from cm.infrastructure.access.support import authorization
from cm.retrieval.mcp_support import build_app, rpc

LEGACY = "/api/system/model-config/embedding"
CURRENT = "/api/v1/embed"


def setup_client(tmp_path, monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(
        "context_manager.model.embedding.embedding_cache",
        EmbeddingCache(),
    )
    auth = {"context": authorization(role="ws_admin")}
    control = {
        "blocked": False,
        "fail": False,
        "dim": 1024,
        "config": ResolvedModel(
            "provider", "model-a", "https://provider.invalid/v1", "private-secret", 1024
        ),
        "calls": [],
        "model_entered": Event(),
        "closed": 0,
        "resolved": [],
    }
    store = RecoveryMemoryStore(
        node(name="mine"),
        node("other", owner="user-2", name="theirs"),
        node("shared", owner="__global__", name="shared"),
    )

    def resolve(config_store, channel):
        assert channel in {"llm", "embedding"}
        control["resolved"].append(
            (
                config_store.repository.tenant_id,
                config_store.repository.user_id,
                channel,
            )
        )
        if control.get("missing"):
            raise ModelConfigError("private-secret")
        return control["config"]

    def sdk_client(**config):
        async def embed(*, input, dimensions, model):
            control["calls"].append((model, tuple(input), dimensions))
            control["model_entered"].set()
            while control["blocked"]:
                await asyncio.sleep(0.005)
            if control["fail"]:
                raise RuntimeError("private-secret and private graph text")
            response = SimpleNamespace(
                data=[
                    SimpleNamespace(index=index, embedding=(1.0,) * control["dim"])
                    for index in range(len(input))
                ]
            )
            return SimpleNamespace(parse=lambda: response)

        async def close():
            control["closed"] += 1

        return SimpleNamespace(
            embeddings=SimpleNamespace(
                with_raw_response=SimpleNamespace(create=embed),
            ),
            close=close,
        )

    def workspace(resolver, tenant_id, workspace_id):
        binding = SimpleNamespace(
            store=store,
            _store=store,
            access=store.access,
            target=SimpleNamespace(tenant_id=tenant_id, workspace_id=workspace_id),
            models=lambda user_id: ModelService(
                ModelConfigRepository(
                    tenant_id,
                    user_id,
                    workspace_id=workspace_id,
                )
            ),
        )

        binding.recovery_stack = lambda rid: WorkspaceRuntime.recovery_stack(
            binding, rid
        )
        return binding

    monkeypatch.setattr(WorkspaceResolver, "resolve", workspace)

    runtime = create_context_runtime()
    runtime.issue_graph_read_scope = store.access.issue_read_scope
    runtime.issue_graph_write_scope = store.access.issue_write_scope
    close_runtime = runtime.aclose
    start_runtime = runtime.start

    async def initialize():
        initialize_database(DbSettings(url=f"sqlite:///{tmp_path / 'shared.db'}"))
        await start_runtime()

    async def close():
        try:
            await close_runtime()
        finally:
            close_database()

    runtime.start = initialize
    runtime.aclose = close
    app = create_api_app()
    app.state.context_runtime = runtime
    app.state.auth_config = SimpleNamespace(control_plane_dsn="test-dsn")
    app.dependency_overrides[require_context_runtime] = lambda: runtime
    app.dependency_overrides[require_context_authorization] = lambda: auth["context"]
    monkeypatch.setattr(ModelService, "resolve", resolve)
    monkeypatch.setattr(ModelService, "read_embedding_space", lambda self: None)
    monkeypatch.setattr("context_manager.model.embedding.AsyncOpenAI", sdk_client)
    return TestClient(app), store, auth, control


def wait_job(client, job_id, *, terminal=True):
    for _ in range(200):
        response = client.get(f"{LEGACY}/jobs/{job_id}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] in ({"success", "failed"} if terminal else {"running"}):
            return job
        time.sleep(0.005)
    raise AssertionError("job did not reach expected state")


def test_admin_rebuilds_all_workspace_owners_and_exposes_safe_progress(
    tmp_path, monkeypatch
):
    client, store, auth, control = setup_client(tmp_path, monkeypatch)
    # A live import awaiting preparation must not be mistaken for abandoned recovery.
    with (
        client,
        client.portal.wrap_async_context_manager(
            client.app.state.context_runtime.recovery.run(
                store.access.issue_write_scope(
                    auth["context"], owner=WriteOwner.WORKSPACE, zone="metadata"
                ),
                task_type="import",
            )
        ),
    ):
        assert client.get(f"{LEGACY}/jobs/latest").json() is None
        response = client.post(f"{LEGACY}/rebuild")
        assert response.status_code == 200, response.text
        assert response.headers["deprecation"] == "true"
        assert "/api/v1/embed/embed-all" in response.headers["link"]
        job_id = response.json()["job_id"]
        job = wait_job(client, job_id)
        assert job["status"] == "success"
        assert set(job) == {
            "job_id",
            "status",
            "created_at",
            "started_at",
            "finished_at",
            "error",
            "progress",
            "config_snapshot",
        }
        assert set(job["progress"]) == {
            "phase",
            "current_label",
            "labels_done",
            "labels_total",
        }
        assert job["progress"]["phase"] == "done"
        assert job["progress"]["labels_done"] == job["progress"]["labels_total"]
        assert set(job["config_snapshot"]) == {"model", "base_url", "dim"}
        assert client.get(f"{CURRENT}/jobs/latest").json() == job
        assert client.get(f"{CURRENT}/jobs/{job_id}").json() == job
        for suffix in ("/jobs/latest", f"/jobs/{job_id}"):
            legacy = client.get(f"{LEGACY}{suffix}")
            assert legacy.json() == job
            assert legacy.headers["deprecation"] == "true"
            assert (
                legacy.headers["link"]
                == f'<{CURRENT}{suffix}>; rel="successor-version"'
            )
        assert client.post(f"{LEGACY}/jobs/{job_id}/retry").status_code == 400
        assert control["resolved"] == [("tenant-1", "user-1", "embedding")]
        assert "embedding" in store.nodes[ENTITY, "ent-1"]
        assert "embedding" in store.nodes[ENTITY, "other"]
        assert "embedding" in store.nodes[ENTITY, "shared"]
        assert "private-secret" not in str(job)
    assert control["closed"] == 1


@pytest.mark.parametrize(
    "context",
    [
        authorization(workspace_id="workspace-2"),
        authorization(tenant_id="tenant-2"),
    ],
)
def test_jobs_are_invisible_across_tenants_and_workspaces(
    tmp_path, monkeypatch, context
):
    client, _, auth, _ = setup_client(tmp_path, monkeypatch)
    with client:
        job_id = client.post(f"{LEGACY}/rebuild").json()["job_id"]
        wait_job(client, job_id)
        auth["context"] = context
        assert client.get(f"{LEGACY}/jobs/latest").json() is None
        assert client.get(f"{LEGACY}/jobs/{job_id}").status_code == 404
        assert client.post(f"{LEGACY}/jobs/{job_id}/retry").status_code == 404


def test_members_can_read_workspace_jobs_but_cannot_start_or_retry(
    tmp_path, monkeypatch
):
    client, _, auth, control = setup_client(tmp_path, monkeypatch)
    control["fail"] = True
    with client:
        job_id = client.post(f"{LEGACY}/rebuild").json()["job_id"]
        wait_job(client, job_id)
        auth["context"] = authorization(user_id="user-2")
        assert client.get(f"{LEGACY}/jobs/{job_id}").status_code == 200
        assert client.get(f"{LEGACY}/jobs/latest").json()["job_id"] == job_id
        assert client.post(f"{LEGACY}/rebuild").status_code == 403
        assert client.post(f"{LEGACY}/jobs/{job_id}/retry").status_code == 403
        auth["context"] = authorization()
        assert client.post(f"{LEGACY}/jobs/{job_id}/retry").status_code == 403
        # An administrator of a DIFFERENT workspace has no elevated rights here.
        auth["context"] = authorization(user_id="user-2", tenant_role="ws_admin")
        assert client.post(f"{LEGACY}/rebuild").status_code == 403
        assert client.post(f"{LEGACY}/jobs/{job_id}/retry").status_code == 403


@pytest.mark.asyncio
async def test_active_rebuild_keeps_authenticated_graph_and_mcp_requests_available(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path))
    app, _, _, _ = build_app(model_config_dsn=f"sqlite:///{tmp_path / 'jobs.db'}")

    async def wait(progress, *_attempt):
        await asyncio.Event().wait()

    async with app.router.lifespan_context(app):
        jobs = app.state.context_runtime.embedding_jobs
        job_id = await jobs.submit(OWNER, SNAPSHOT, wait, created_by="admin")
        async with asyncio.timeout(3):
            while (await jobs.get(OWNER, job_id))["status"] != "running":
                await asyncio.sleep(0.005)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url="http://cm",
            headers={"Authorization": "Bearer user-1"},
        ) as client:
            response = await client.get("/api/v1/graph/get_schema")
            assert response.status_code == 200, response.text
            response = await client.post(
                "/mcp/v1/cm",
                headers={"Accept": "application/json, text/event-stream"},
                json=rpc(
                    "tools/call", params={"name": "get_graph_schema", "arguments": {}}
                ),
            )
            assert response.status_code == 200, response.text
            assert response.json()["result"]["isError"] is False
        assert (await jobs.get(OWNER, job_id))["status"] == "running"

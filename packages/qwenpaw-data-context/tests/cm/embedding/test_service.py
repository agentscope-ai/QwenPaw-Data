"""Retry rebuilds using the current administrator's identity and model settings."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from context_manager.db import get_database, initialize_database
from context_manager.embedding import EmbeddingJobManager
from context_manager.embedding.service import EmbeddingDimensionMismatch
from context_manager.schema import ENTITY
from context_manager.settings.db import DbSettings
from context_manager.sql import RdbConnector
from qwenpaw_data_auth.database import (
    get_db,
    init_schema,
    role_bindings,
    tenants,
    users,
    workspaces,
)

from cm.embedding.support import RecoveryMemoryStore, node
from cm.embedding.test_jobs import SNAPSHOT, finished
from cm.infrastructure.access.support import authorization
from cm.infrastructure.lifecycle.support import bound_context_runtime
from cm.infrastructure.models.support import mock_model_service
from cm.infrastructure.storage.schema_support import make_glossary


@pytest.fixture
def auth_database(tmp_path):
    dsn = f"sqlite:///{tmp_path / 'auth.db'}"
    init_schema(dsn)
    initialize_database(DbSettings(url=dsn))
    with get_db(dsn) as connection:
        connection.execute(
            tenants.insert().values(
                id="tenant", name="Synthetic", neo4j_database="neo4j"
            )
        )
        connection.execute(
            workspaces.insert().values(
                id="workspace", tenant_id="tenant", name="Synthetic", status="active"
            )
        )
        connection.execute(
            users.insert().values(id="admin", username="admin", tenant_id="tenant")
        )
        connection.execute(
            role_bindings.insert().values(
                id="role",
                user_id="admin",
                scope_type="workspace",
                scope_id="workspace",
                role="ws_admin",
            )
        )
        connection.commit()
    return dsn


@pytest_asyncio.fixture
async def failed_rebuild(request, auth_database):
    owner = ("tenant", "workspace")
    jobs = EmbeddingJobManager()

    async def fail(progress, *_attempt):
        raise RuntimeError("interrupted rebuild")

    job_id = await jobs.submit(owner, SNAPSHOT, fail, created_by="admin")
    assert (await finished(jobs, owner, job_id))["status"] == "failed"
    store = RecoveryMemoryStore(
        node(name="retried entity", owner="admin", workspace="workspace")
    )
    model_users = []
    embedder = SimpleNamespace(
        dimensions=1024,
        config_snapshot=SNAPSHOT,
        embed=AsyncMock(side_effect=lambda texts: ((1.0,) * 1024,) * len(texts)),
        aclose=AsyncMock(),
    )
    clients = mock_model_service(None, embedder)
    clients.resolve_for_use.return_value = SimpleNamespace(
        model=SNAPSHOT["model"],
        base_url=SNAPSHOT["base_url"],
        dim=SNAPSHOT["dim"],
    )

    def models(user_id):
        model_users.append(user_id)
        return clients

    context = authorization(
        "other-admin", *owner, role=getattr(request, "param", "ws_admin")
    )
    engine = get_database().engine
    runtime = bound_context_runtime(
        store,
        RdbConnector(),
        make_glossary(store.schema, engine=engine, workspace_id=owner[1]),
        model_service_factory=models,
        database_engine=engine,
        tenant_id=owner[0],
        workspace_id=owner[1],
    )
    await jobs.aclose()
    jobs = runtime.embedding_jobs
    await runtime.start()
    service = await runtime.embedding_rebuild(context)
    try:
        yield service, jobs, context, job_id, model_users, store, clients
    finally:
        await runtime.aclose()


@pytest.mark.asyncio
async def test_retry_uses_current_admin_even_if_original_user_is_disabled(
    failed_rebuild, auth_database
):
    with get_db(auth_database) as connection:
        connection.execute(users.update().values(status="disabled"))
        connection.commit()
    service, jobs, context, job_id, model_users, store, clients = failed_rebuild
    clients.resolve_for_use.return_value.model = "changed-model"
    assert await service.start(context, job_id=job_id) == job_id
    result = await finished(
        jobs, (context.tenant_id, context.active_workspace_id), job_id
    )
    assert result["status"] == "success"
    assert result["config_snapshot"]["model"] == "changed-model"
    assert result["created_by"] == "admin"
    assert model_users == ["other-admin"]
    assert all(scope.user_id == "other-admin" for scope in store.scopes)
    assert all("embedding" in item for item in store.nodes.values())


@pytest.mark.asyncio
async def test_rebuild_checks_store_dimensions_before_submitting(failed_rebuild):
    service, jobs, context, job_id, _, store, clients = failed_rebuild
    store._vector_dimensions = {ENTITY: 1536}
    with pytest.raises(EmbeddingDimensionMismatch):
        await service.start(context, job_id=job_id)
    job = await jobs.get((context.tenant_id, context.active_workspace_id), job_id)
    assert job["status"] == "failed"
    clients.embedder.assert_not_called()

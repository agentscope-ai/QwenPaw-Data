"""Check committed batches and recovery with real Neo4j and synthetic embeddings."""

import pytest
from context_manager.auth import WriteOwner
from context_manager.embedding import embed_all
from context_manager.metadata import MetadataImportService
from context_manager.schema import HAS_METRIC, METRIC
from context_manager.store import StoreError
from context_manager.store.neo4j import Neo4jGraphStore

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.infrastructure.processing.test_embedding import RecordingEmbedder
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    graph as graph,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    local_driver as local_driver,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.transactions.recovery.conftest import (
    recovery as recovery,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.transactions.recovery.conftest import (
    runs as runs,  # noqa: PLC0414 - intentional test fixture
)
from cm.metadata.test_write_batches import large_snapshot


@pytest.fixture
def publication(graph, recovery):  # noqa: F811 - imported pytest fixture
    store, _, (private, _) = graph
    scope = store.access.issue_write_scope(
        authorization(workspace_id=private.workspace_id, role="tenant_admin"),
        owner=WriteOwner.WORKSPACE,
        datasource_id="warehouse",
        zone="metadata",
    )
    service = MetadataImportService(
        store,
        StaticModelService(FakeCaller()),
        executor=executor,
        recovery=recovery.service,
    )
    return store, service, scope


@pytest.mark.asyncio
async def test_real_mg_failure_reverts_prior_commits_and_fresh_full_succeeds(
    publication, monkeypatch
):
    store, service, scope = publication
    create = Neo4jGraphStore.create_nodes

    def fail_second(tx, spec, properties, *, scope):
        result = create(tx, spec, properties, scope=scope)
        if spec is METRIC and any(row["name"] == "metric-139" for row in properties):
            raise StoreError("injected second batch failure", operation="create_nodes")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Neo4jGraphStore, "create_nodes", fail_second)
        with pytest.raises(StoreError, match="second batch failure"):
            await service.import_snapshot(large_snapshot(), scope)
    assert not store.list_nodes(METRIC, scope=scope, limit=1000)
    assert not store.list_edges(HAS_METRIC, scope=scope)
    await service.import_snapshot(large_snapshot(full=True), scope)
    metrics = store.list_nodes(METRIC, scope=scope, limit=1000)
    assert len(metrics) == 230
    assert {
        row.target_key for row in store.list_edges(HAS_METRIC, scope=scope, limit=1000)
    } == {row.key for row in metrics}


@pytest.mark.asyncio
async def test_real_rebuild_failure_and_retry_preserve_committed_vectors(
    publication, monkeypatch
):
    store, service, scope = publication
    await service.import_snapshot(large_snapshot(), scope)
    original = {
        row.key: row for row in store.list_nodes(METRIC, scope=scope, limit=1000)
    }
    patch_nodes = Neo4jGraphStore.patch_nodes
    atomic = store.atomic
    commits = []

    def record(work):
        result = atomic(work)
        commits.append(result.stats["nodes"]["updates_submitted"])
        return result

    def fail_after_first(tx, spec, patches, *, scope):
        result = patch_nodes(tx, spec, patches, scope=scope)
        if len(commits) == 1:
            raise StoreError("injected rebuild batch failure", operation="patch_nodes")
        return result

    monkeypatch.setattr(store, "atomic", record)
    with monkeypatch.context() as patch:
        patch.setattr(Neo4jGraphStore, "patch_nodes", fail_after_first)
        with pytest.raises(StoreError, match="rebuild batch failure"):
            await embed_all(store, scope, RecordingEmbedder(), executor=executor)
    assert commits == [1]
    changed = [
        row
        for row in store.list_nodes(METRIC, scope=scope, limit=1000)
        if row.properties["embedding"] != original[row.key].properties["embedding"]
    ]
    # The Domain page committed; the first Metric page rolled back.
    assert len(changed) == 0
    assert await embed_all(store, scope, RecordingEmbedder(), executor=executor) == 231
    assert commits == [1, 1, 100, 100, 30]
    for row in store.list_nodes(METRIC, scope=scope, limit=1000):
        assert {
            key: value
            for key, value in row.properties.items()
            if not key.startswith("embedding")
        } == {
            key: value
            for key, value in original[row.key].properties.items()
            if not key.startswith("embedding")
        }
    assert len(store.list_edges(HAS_METRIC, scope=scope, limit=1000)) == 230

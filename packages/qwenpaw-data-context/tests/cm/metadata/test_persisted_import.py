"""Exercise business batch Plans against a disposable local Neo4j database."""

import pytest
from context_manager.auth import WriteOwner
from context_manager.metadata import MetadataImportService
from context_manager.schema import (
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DIMENSION,
    DOMAIN,
    METRIC,
)

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import FakeCaller, StaticModelService
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
from cm.metadata.test_snapshot_updates import ordered_domains_snapshot


@pytest.mark.asyncio
async def test_metadata_batches_create_patch_and_rebuild_both_domains_in_neo4j(
    graph, recovery  # noqa: F811 - imported pytest fixture
):
    store, _, (workspace_scope, _) = graph
    scope = store.access.issue_write_scope(
        authorization(workspace_id=workspace_scope.workspace_id, role="tenant_admin"),
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
    first = await service.import_snapshot(ordered_domains_snapshot(), scope)
    keys = tuple(record.key for record in store.list_nodes(METRIC, scope=scope))
    assert len(keys) == 4
    # Dataset references add one datasource anchor and four bound fields.
    assert first.stats["nodes"]["created"] == 19
    assert {
        spec.name: len(store.list_nodes(spec, scope=scope))
        for spec in (DATA_SOURCE, DOMAIN, DATASET, DATASET_FIELD, DIMENSION, METRIC)
    } == {
        "DataSource": 1,
        "Domain": 2,
        "Dataset": 4,
        "DatasetField": 4,
        "Dimension": 4,
        "Metric": 4,
    }
    datasource = store.list_nodes(DATA_SOURCE, scope=scope)[0]
    fields = tuple(store.list_nodes(DATASET_FIELD, scope=scope))

    updated = await service.import_snapshot(
        ordered_domains_snapshot(revision="修订"), scope
    )
    assert updated.stats["nodes"]["created"] == 0
    assert updated.stats["nodes"]["updates_submitted"] == 14
    assert store.list_nodes(DATA_SOURCE, scope=scope) == (datasource,)
    assert tuple(store.list_nodes(DATASET_FIELD, scope=scope)) == fields
    assert tuple(record.key for record in store.list_nodes(METRIC, scope=scope)) == keys
    assert all(
        record.properties["description"] == "修订"
        for record in store.get_nodes(METRIC, keys, scope=scope)
    )

    rebuilt = await service.import_snapshot(
        ordered_domains_snapshot(revision="重建", full=True), scope
    )
    # FULL replaces semantic nodes and fields, retaining the datasource anchor.
    assert (
        rebuilt.stats["nodes"]["deletes_submitted"]
        == rebuilt.stats["nodes"]["created"]
        == 18
    )
    assert store.list_nodes(DATA_SOURCE, scope=scope) == (datasource,)
    assert tuple(node.key for node in store.list_nodes(DATASET_FIELD, scope=scope)) == (
        tuple(node.key for node in fields)
    )
    assert tuple(record.key for record in store.list_nodes(METRIC, scope=scope)) == keys

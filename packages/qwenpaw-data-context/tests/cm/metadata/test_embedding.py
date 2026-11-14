"""Verify final Metadata facts and their vectors commit as one prepared graph."""

from copy import deepcopy
from hashlib import sha256

import pytest
from context_manager.auth import WriteOwner
from context_manager.metadata import MetadataImportService
from context_manager.model import LlmError
from context_manager.schema import METRIC

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.metadata.support import UnstackedImports
from cm.metadata.test_snapshot_updates import TransactionStore, snapshot


class TextVectorCaller(FakeCaller):
    """Give different final text different deterministic vectors, outside atomic."""

    def __init__(self, store):
        super().__init__()
        self.store = store
        self.fail = False

    async def embed(self, texts, **options):
        assert not self.store.transaction_open
        self.embed_calls.append((tuple(texts), options))
        if self.fail:
            raise LlmError("simulated embedding failure")
        return tuple(
            tuple(float(value) for value in sha256(text.encode()).digest()[:3])
            for text in texts
        )


@pytest.fixture
def graph():
    store = TransactionStore()
    scope = store.access.issue_write_scope(
        authorization(role="ws_admin"),
        owner=WriteOwner.WORKSPACE,
        datasource_id="warehouse",
        zone="metadata",
    )
    caller = TextVectorCaller(store)
    manager = StaticModelService(caller)
    service = MetadataImportService(
        store, manager, executor=executor, recovery=UnstackedImports(store)
    )
    return store, service, scope, caller, manager


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
async def test_changed_metadata_replaces_vector_and_hash_without_changing_node_keys(
    graph, full
):
    store, service, scope, _caller, _ = graph
    await service.import_snapshot(snapshot(), scope)
    keys = set(store.nodes)
    key = "met:__global__:sales:Revenue"
    before = store.get_nodes(METRIC, (key,), scope=scope)[0]
    await service.import_snapshot(
        snapshot(description="扣除退款后的实际成交额", full=full), scope
    )
    after = store.get_nodes(METRIC, (key,), scope=scope)[0]

    assert set(store.nodes) == keys
    assert before.key == after.key
    assert after.properties["embedding"] != before.properties["embedding"]
    assert after.properties["embedding_hash"] != before.properties["embedding_hash"]


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [False, True])
async def test_embedding_failure_never_opens_a_write_or_deletes_existing_metadata(
    graph, full
):
    store, service, scope, caller, _ = graph
    await service.import_snapshot(snapshot(), scope)
    before = tuple(
        {key: deepcopy(value) for key, value in rows.items()}
        for rows in (store.nodes, store.edges)
    )
    caller.fail = True

    with pytest.raises(LlmError, match="embedding failure"):
        await service.import_snapshot(
            snapshot(description="修改后的口径", full=full), scope
        )

    assert (store.nodes, store.edges) == before

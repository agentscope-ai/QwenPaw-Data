"""Share batch capacity across MG publication, imports and embedding rebuilds."""

import asyncio

import pytest
from context_manager.auth import WriteOwner
from context_manager.embedding import embed_all
from context_manager.metadata import MetadataImportService

from cm.embedding.support import MemoryStore, node
from cm.infrastructure.access.support import authorization
from cm.infrastructure.models.support import FakeCaller, StaticModelService
from cm.metadata.support import UnstackedImports
from cm.metadata.test_snapshot_updates import TransactionStore
from cm.metadata.test_weaving import (
    editor as editor,  # noqa: PLC0414 - intentional test fixture
)
from cm.metadata.test_weaving import (
    editor_database as editor_database,  # noqa: PLC0414 - intentional test fixture
)
from cm.metadata.test_write_batches import large_snapshot
from cm.metadata.test_write_batches import (
    batch_budget as batch_budget,  # noqa: PLC0414 - intentional test fixture
)


@pytest.mark.asyncio
async def test_eight_mixed_jobs_share_two_background_workers_across_workspaces(editor):  # noqa: F811 - imported pytest fixture
    pools = editor.runtime._execution
    release, occupied = asyncio.Event(), asyncio.Event()
    started = []
    active = peak = 0

    class Caller(FakeCaller):
        async def embed(self, texts, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            started.append(len(texts))
            if active == 2:
                occupied.set()
            try:
                await release.wait()
                return await super().embed(texts, **kwargs)
            finally:
                active -= 1

    caller = Caller()
    editor.caller.embed = caller.embed
    tasks = [
        asyncio.create_task(
            editor.publisher.import_snapshot(large_snapshot(), editor.scope)
        )
    ]
    stores = []
    try:
        for i in range(7):
            tenant, workspace = f"tenant-{i % 2}", f"workspace-{i}"
            if i % 2:
                store = TransactionStore()
                scope = store.access.issue_write_scope(
                    authorization(
                        tenant_id=tenant, workspace_id=workspace, role="ws_admin"
                    ),
                    owner=WriteOwner.WORKSPACE,
                    datasource_id="warehouse",
                    zone="metadata",
                )
                service = MetadataImportService(
                    store,
                    StaticModelService(caller),
                    executor=pools.background,
                    recovery=UnstackedImports(store),
                )
                work = service.import_snapshot(large_snapshot(), scope)
                expected = 231
            else:
                store = MemoryStore(
                    *(
                        node(f"ent-{j}", workspace=workspace, name=f"entity-{j}")
                        for j in range(230)
                    )
                )
                work = embed_all(
                    store,
                    store.scope(workspace=workspace),
                    caller,
                    executor=pools.background,
                )
                expected = 230
            stores.append((store, expected))
            tasks.append(asyncio.create_task(work))
        await asyncio.wait_for(occupied.wait(), 5)
        assert await asyncio.wait_for(pools.graph_read.run(lambda: 42), 1) == 42
        # Standalone writes share the same queue, even during model waits.
        writing = asyncio.create_task(pools.background.run(lambda: 7))
        await asyncio.sleep(0)
        assert not writing.done()
        release.set()
        assert await asyncio.wait_for(writing, 5) == 7
        results = await asyncio.wait_for(asyncio.gather(*tasks), 15)
        assert results[0].stats["nodes"]["created"] == 231
        for (store, expected), result in zip(stores, results[1:], strict=True):
            assert len(store.nodes) == expected
            assert (
                result if isinstance(result, int) else result.stats["nodes"]["created"]
            ) == expected
        assert peak == 2 and len(started) > 8

        async def nested():
            return await pools.background.run_sync(lambda: 42)

        assert await asyncio.wait_for(pools.background.run(nested), 2) == 42
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

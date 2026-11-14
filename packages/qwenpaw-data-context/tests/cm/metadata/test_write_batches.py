"""Verify ordinary MG plan batching with the recovery boundary substituted."""

import asyncio
from threading import Event

import pytest
from context_manager.executor import BoundedExecutor
from context_manager.metadata import SemanticImportRequest
from context_manager.schema import DOMAIN, HAS_METRIC, METRIC
from context_manager.settings import get_settings
from context_manager.settings.execution import PoolSettings

from cm.metadata.support import UnstackedImports
from cm.metadata.test_snapshot_updates import (
    graph as graph,  # noqa: PLC0414 - intentional test fixture
)


@pytest.fixture(autouse=True)
def batch_budget(isolated_settings, monkeypatch):
    monkeypatch.setattr(get_settings().graph, "write_batch_max_rows", 100)


def large_snapshot(*, count=230, description="first", full=False):
    return SemanticImportRequest.model_validate(
        {
            "datasource_id": "warehouse",
            "drop_semantic_first": full,
            "semantic": {
                "domains": [
                    {
                        "domain_id": "sales",
                        "name": "Sales",
                        "metrics": [
                            {"name": f"metric-{i:03}", "description": description}
                            for i in range(count)
                        ],
                    }
                ]
            },
        }
    )


@pytest.mark.asyncio
async def test_import_incremental_and_full_preserve_facts_across_bounded_commits(
    graph, monkeypatch  # noqa: F811 - imported pytest fixture
):
    store, importer, scope = graph
    sizes = []
    atomic = store.atomic

    def record(work):
        result = atomic(work)
        sizes.append(sum(sum(counts.values()) for counts in result.stats.values()))
        return result

    monkeypatch.setattr(store, "atomic", record)
    result = await importer.import_snapshot(large_snapshot(), scope)
    assert len(sizes) > 1 and max(sizes) <= 100
    assert result.stats["nodes"]["created"] == 231
    assert result.stats["edges"]["created"] == 230
    assert len(result.node_keys) == 231
    assert sum(len(values) for values in result.operation_results) == 461
    for full in (False, True):
        await importer.import_snapshot(
            large_snapshot(count=210, description="revised", full=full), scope
        )
        metrics = store.list_nodes(METRIC, scope=scope, limit=1000)
        assert len(metrics) == (210 if full else 230)
        assert {
            row.properties["name"]
            for row in metrics
            if row.properties["description"] == "revised"
        } == {f"metric-{i:03}" for i in range(210)}
        domain = store.list_nodes(DOMAIN, scope=scope)[0]
        links = store.list_edges(HAS_METRIC, scope=scope, limit=1000)
        assert {edge.target_key for edge in links} == {row.key for row in metrics}
        assert all(edge.source_key == domain.key for edge in links)
    assert max(sizes) <= 100


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_batch_releases_background_capacity_and_cancel_stops_later_commits(
    graph, monkeypatch, cancel  # noqa: F811 - imported pytest fixture
):
    store, importer, scope = graph
    pool = BoundedExecutor("test-background", PoolSettings(workers=1))
    importer = type(importer)(
        store, importer._models, executor=pool, recovery=UnstackedImports(store)
    )
    started, release = Event(), Event()
    atomic = store.atomic

    def hold_first(work):
        if not started.is_set():
            started.set()
            assert release.wait(5)
        return atomic(work)

    monkeypatch.setattr(store, "atomic", hold_first)
    task = asyncio.create_task(importer.import_snapshot(large_snapshot(), scope))
    observer = None
    try:
        assert await asyncio.to_thread(started.wait, 3)
        observer = asyncio.create_task(pool.run(lambda: len(store.nodes)))
        await asyncio.sleep(0)
        if cancel:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        assert await observer == 100
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(store.nodes) == 100 and not store.edges
        else:
            await task
            assert len(store.nodes) == 231 and len(store.edges) == 230
    finally:
        release.set()
        await asyncio.gather(
            task, *([observer] if observer else []), return_exceptions=True
        )
        await pool.aclose()

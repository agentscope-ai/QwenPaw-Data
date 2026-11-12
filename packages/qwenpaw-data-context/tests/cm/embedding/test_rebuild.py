"""Verify full re-embedding, exact ownership and bounded atomic writeback."""

import asyncio
from threading import Event

import pytest
from context_manager.embedding import embed_all
from context_manager.schema import ENTITY, USER, USER_MEMORY

from cm.embedding.support import MemoryStore, node
from cm.infrastructure.execution.support import executor
from cm.infrastructure.processing.test_embedding import RecordingEmbedder


@pytest.mark.asyncio
async def test_embed_all_pages_private_nodes_and_only_replaces_embedding_properties(
    monkeypatch,
):
    records = (
        node(
            name="one",
            identity_hash="unchanged",
            embedding=(9.0, 9.0),
            embedding_hash="old",
        ),
        node("ent-2", name="two"),
        node("mem-1", spec=USER_MEMORY, description="short answers", id="legacy-id"),
        node(
            "mem-empty",
            spec=USER_MEMORY,
            description=" ",
            embedding=(9.0, 9.0),
            embedding_hash="old",
        ),
        node("shared", owner="__global__", name="shared"),
        node("other", owner="user-2", name="private"),
        node("elsewhere", workspace="workspace-2", name="private"),
        node("usr-1", spec=USER, user_id="user-1"),
    )
    store = MemoryStore(*records)
    monkeypatch.setattr(
        store, "get_nodes", lambda *args, **kwargs: pytest.fail("unexpected readback")
    )
    embedder = RecordingEmbedder()
    progress = []

    async def record_progress(value):
        progress.append(value)

    count = await embed_all(
        store,
        store.scope(),
        embedder,
        page_size=1,
        on_progress=record_progress,
        executor=executor,
    )

    assert count == 4
    assert len(embedder.calls) == 3
    assert {key for _, key, _ in store.writes} == {
        "ent-1",
        "ent-2",
        "mem-1",
        "mem-empty",
    }
    for original in records:
        current = store.nodes[original.spec, original.key]
        assert {
            k: v for k, v in current.properties.items() if not k.startswith("embedding")
        } == {
            k: v
            for k, v in original.properties.items()
            if not k.startswith("embedding")
        }
    assert store.nodes[USER_MEMORY, "mem-empty"].properties["embedding"] is None
    assert all(filters == {"owner_id": "user-1"} for _, filters, _, _ in store.pages)
    assert any(offset == 2 for spec, _, _, offset in store.pages if spec is ENTITY)
    assert progress[-1]["phase"] == "committing"
    assert {entry["phase"] for entry in progress} == {
        "scanning",
        "embedding",
        "committing",
    }

    # A content hash matches even when the selected model changes: force a new call.
    previous_calls = len(embedder.calls)
    await embed_all(store, store.scope(), embedder, executor=executor)
    assert len(embedder.calls) > previous_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_fails", [False, True])
async def test_cancel_during_first_batch_waits_for_commit_but_does_not_start_next_batch(
    commit_fails,
):
    from context_manager.executor import BoundedExecutor
    from context_manager.settings.execution import PoolSettings

    pool = BoundedExecutor("rebuild-test", PoolSettings(workers=1))
    store = MemoryStore(*(node(f"ent-{i:03}", name=f"item {i}") for i in range(230)))
    if commit_fails:
        store.fail_key = "ent-050"
    started, release = Event(), Event()

    def before_atomic():
        started.set()
        assert release.wait(5)

    store.before_atomic = before_atomic
    task = asyncio.create_task(
        embed_all(
            store, store.scope(), RecordingEmbedder(), page_size=100, executor=pool
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 3)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        assert not task.done()
        release.set()
        # The executor finishes the transaction, then propagates cancellation.
        with pytest.raises(asyncio.CancelledError):
            await task
        expected = 0 if commit_fails else 100
        assert (
            sum("embedding" in row.properties for row in store.nodes.values())
            == expected
        )
        assert (
            await pool.run(
                lambda: sum(
                    "embedding" in row.properties for row in store.nodes.values()
                )
            )
            == expected
        )
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await pool.aclose()

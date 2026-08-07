"""Bounded blocking-I/O execution, timeout, and backpressure tests."""

from __future__ import annotations

import asyncio
import contextvars
import threading
import time

import pytest

from datapaw.context.blocking_io import (
    BlockingIOGovernor,
    BlockingIOOverloaded,
    BlockingIOTimeout,
    BlockingPool,
    BlockingPoolConfig,
)


def _configs(
    *,
    graph_workers: int = 1,
    graph_queue: int = 1,
    graph_queue_timeout: float = 0.05,
    graph_timeout: float = 1.0,
) -> dict[BlockingPool, BlockingPoolConfig]:
    graph = BlockingPoolConfig(
        max_workers=graph_workers,
        max_queue=graph_queue,
        queue_timeout_seconds=graph_queue_timeout,
        operation_timeout_seconds=graph_timeout,
    )
    spare = BlockingPoolConfig(
        max_workers=1,
        max_queue=1,
        queue_timeout_seconds=0.05,
        operation_timeout_seconds=1.0,
    )
    return {
        BlockingPool.GRAPH: graph,
        BlockingPool.FILE: spare,
        BlockingPool.NETWORK: spare,
        BlockingPool.SQL: spare,
    }


async def _wait_for_metric(
    governor: BlockingIOGovernor,
    pool: BlockingPool,
    key: str,
    value: int,
) -> None:
    for _ in range(100):
        if governor.snapshot()[pool.value][key] == value:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(
        f"metric {pool.value}.{key} did not become {value}: {governor.snapshot()}"
    )


async def test_queue_is_finite_and_rejects_when_saturated():
    governor = BlockingIOGovernor(_configs())
    release = threading.Event()

    def blocked(value: int) -> int:
        release.wait(timeout=2)
        return value

    first = asyncio.create_task(
        governor.run(BlockingPool.GRAPH, "first", blocked, 1)
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "active", 1)
    second = asyncio.create_task(
        governor.run(BlockingPool.GRAPH, "second", blocked, 2)
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "queued", 1)

    with pytest.raises(BlockingIOOverloaded):
        await governor.run(
            BlockingPool.GRAPH,
            "rejected",
            lambda: 3,
            queue_timeout_seconds=0.01,
        )

    snapshot = governor.snapshot()["graph"]
    assert snapshot["active"] == 1
    assert snapshot["queued"] == 1
    assert snapshot["rejected"] == 1

    release.set()
    assert await asyncio.gather(first, second) == [1, 2]
    await governor.aclose()


async def test_request_timeout_does_not_release_a_running_thread_slot():
    governor = BlockingIOGovernor(
        _configs(graph_queue=0, graph_queue_timeout=0.02, graph_timeout=0.01)
    )
    release = threading.Event()

    def blocked() -> str:
        release.wait(timeout=2)
        return "done"

    with pytest.raises(BlockingIOTimeout):
        await governor.run(BlockingPool.GRAPH, "timeout", blocked)

    # The caller timed out, but the underlying mutation may still be running.
    # Capacity must remain occupied until the thread really exits.
    assert governor.snapshot()["graph"]["active"] == 1
    with pytest.raises(BlockingIOOverloaded):
        await governor.run(BlockingPool.GRAPH, "must-not-overlap", lambda: None)

    release.set()
    await _wait_for_metric(governor, BlockingPool.GRAPH, "active", 0)
    snapshot = governor.snapshot()["graph"]
    assert snapshot["timed_out"] == 1
    assert snapshot["completed"] == 1
    await governor.aclose()


async def test_cancelling_queued_work_releases_admission_capacity():
    governor = BlockingIOGovernor(_configs())
    release = threading.Event()

    first = asyncio.create_task(
        governor.run(
            BlockingPool.GRAPH,
            "first",
            lambda: release.wait(timeout=2),
        )
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "active", 1)
    queued = asyncio.create_task(
        governor.run(BlockingPool.GRAPH, "cancelled", lambda: None)
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "queued", 1)

    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    await _wait_for_metric(governor, BlockingPool.GRAPH, "queued", 0)

    replacement = asyncio.create_task(
        governor.run(BlockingPool.GRAPH, "replacement", lambda: "ok")
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "queued", 1)
    release.set()

    await first
    assert await replacement == "ok"
    snapshot = governor.snapshot()["graph"]
    assert snapshot["cancelled"] == 1
    assert snapshot["rejected"] == 0
    await governor.aclose()


async def test_resource_pools_are_isolated():
    governor = BlockingIOGovernor(_configs(graph_queue=0))
    release = threading.Event()

    graph_task = asyncio.create_task(
        governor.run(
            BlockingPool.GRAPH,
            "graph.blocked",
            lambda: release.wait(timeout=2),
        )
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "active", 1)

    started = time.monotonic()
    result = await governor.run(BlockingPool.FILE, "file.fast", lambda: "ok")
    assert result == "ok"
    assert time.monotonic() - started < 0.2

    release.set()
    await graph_task
    await governor.aclose()


async def test_request_context_is_propagated_to_worker_threads():
    selected_database = contextvars.ContextVar("selected_database", default="default")
    governor = BlockingIOGovernor(_configs())
    token = selected_database.set("tenant-a")
    try:
        result = await governor.run(
            BlockingPool.GRAPH,
            "context.read",
            selected_database.get,
        )
    finally:
        selected_database.reset(token)

    assert result == "tenant-a"
    await governor.aclose()


async def test_background_submission_is_tracked_and_drained():
    governor = BlockingIOGovernor(_configs())
    governor.submit(BlockingPool.FILE, "background", lambda: "ok")
    await _wait_for_metric(governor, BlockingPool.FILE, "completed", 1)
    await governor.aclose()
    assert governor.closed is True


async def test_shutdown_returns_when_background_work_exceeds_drain_deadline():
    governor = BlockingIOGovernor(
        _configs(),
        shutdown_timeout_seconds=0.01,
    )
    release = threading.Event()
    governor.submit(
        BlockingPool.GRAPH,
        "slow-background",
        lambda: release.wait(timeout=2),
    )
    await _wait_for_metric(governor, BlockingPool.GRAPH, "active", 1)

    started = time.monotonic()
    await governor.aclose()
    elapsed = time.monotonic() - started
    assert elapsed < 0.2

    release.set()
    await _wait_for_metric(governor, BlockingPool.GRAPH, "active", 0)

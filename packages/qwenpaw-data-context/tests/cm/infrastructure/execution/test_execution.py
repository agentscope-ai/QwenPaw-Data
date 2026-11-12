"""Observable capacity, cancellation and shutdown of Runtime execution pools."""

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from threading import Event

import pytest
from context_manager.runtime.execution import ExecutionResources
from context_manager.settings.execution import ExecutionSettings


@pytest.fixture(params=[5.0])
def pools(monkeypatch, request):
    monkeypatch.setenv("CM_EXECUTION_BACKGROUND__WORKERS", "1")
    monkeypatch.setenv("CM_EXECUTION_BACKGROUND__MAX_WAITERS", "1")
    monkeypatch.setenv(
        "CM_EXECUTION_BACKGROUND__QUEUE_TIMEOUT_SECONDS", str(request.param)
    )
    settings = ExecutionSettings()
    assert settings.graph_read.workers == 8  # Nested overrides preserve the other pool.
    resources = ExecutionResources(settings)
    yield resources
    resources.close()


@asynccontextmanager
async def worker(pool, after=None):
    started, release = Event(), Event()

    def work():
        started.set()
        assert release.wait(5), "test did not release the worker"
        return 7

    async def execute():
        result = await pool.run(work)
        if after is not None:
            after()
        return result

    task = asyncio.create_task(execute())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        yield task, release
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pools", "cancel"),
    [(0.05, False), (5.0, True)],
    indirect=["pools"],
    ids=["timeout", "cancel"],
)
async def test_bounded_waiting_preserves_read_capacity_and_discards_abandoned_work(
    pools, cancel, caplog
):
    calls = []
    async with worker(pools.background) as (running, release):
        # Foreground availability must not consume the queued task's timeout.
        assert await asyncio.wait_for(pools.graph_read.run(lambda: 42), 1) == 42
        waiting = asyncio.create_task(pools.background.run(calls.append, "abandoned"))
        await asyncio.sleep(0)
        with pytest.raises(BlockingIOError, match="full"):
            await pools.background.run(calls.append, "overflow")
        assert "background execution queue is full" in caplog.messages
        if cancel:
            waiting.cancel()
        with pytest.raises(
            asyncio.CancelledError if cancel else BlockingIOError,
            match=None if cancel else "timed out",
        ):
            await waiting
        if not cancel:
            assert "background execution queue timed out" in caplog.messages
        release.set()
        await running
        await pools.background.run(calls.append, "accepted")
    assert calls == ["accepted"]


@pytest.mark.asyncio
async def test_running_cancellation_retains_capacity_and_transports_context_and_errors(
    pools,
):
    actor = ContextVar("actor", default="missing")
    actor.set("alice")
    async with worker(pools.background) as (running, release):
        running.cancel()
        await asyncio.sleep(0)
        running.cancel()
        waiting = asyncio.create_task(pools.background.run(actor.get))
        await asyncio.sleep(0)
        assert not running.done() and not waiting.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert await waiting == "alice"
    with pytest.raises(ZeroDivisionError):
        await pools.background.run(lambda: 1 / 0)
    assert await pools.background.run(lambda: 42) == 42


@pytest.mark.asyncio
async def test_shutdown_waits_for_async_unit_and_allows_its_final_sync_step(pools):
    entered, release = asyncio.Event(), asyncio.Event()
    results = []

    async def write():
        entered.set()
        await release.wait()
        results.append(await pools.background.run_sync(lambda: 42))

    work = asyncio.create_task(pools.background.run(write))
    closing = None
    try:
        await entered.wait()
        closing = asyncio.create_task(pools.aclose())
        await asyncio.sleep(0)
        assert not closing.done()
        with pytest.raises(BlockingIOError, match="closed"):
            await pools.background.run(lambda: 0)
        release.set()
        await asyncio.wait_for(asyncio.gather(work, closing), 2)
        assert results == [42]
    finally:
        release.set()
        await asyncio.gather(
            work, *([closing] if closing else []), return_exceptions=True
        )

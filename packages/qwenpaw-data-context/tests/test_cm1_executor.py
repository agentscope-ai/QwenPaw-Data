import asyncio
import threading

import pytest

from context_manager.executor import BoundedExecutor
from context_manager.settings.execution import PoolSettings


@pytest.fixture
def executor():
    return BoundedExecutor(
        "test", PoolSettings(workers=1, max_waiters=2, queue_timeout_seconds=1)
    )


@pytest.mark.asyncio
async def test_async_and_sync_operations_share_admission(executor):
    async def async_add(first, *, second):
        return first + second

    assert await executor.run(async_add, 2, second=3) == 5
    assert await executor.run(lambda first, *, second: first + second, 4, second=5) == 9
    await executor.aclose()


@pytest.mark.asyncio
async def test_fifo_admission(executor):
    entered = asyncio.Event()
    release = asyncio.Event()
    order = []

    async def first():
        entered.set()
        await release.wait()
        order.append(0)

    async def record(index):
        order.append(index)

    active = asyncio.create_task(executor.run(first))
    await entered.wait()
    second = asyncio.create_task(executor.run(record, 1))
    await asyncio.sleep(0)
    third = asyncio.create_task(executor.run(record, 2))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(active, second, third)
    assert order == [0, 1, 2]
    await executor.aclose()


@pytest.mark.asyncio
async def test_full_queue_fails_without_starting_operation(executor):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    active = asyncio.create_task(executor.run(hold))
    await entered.wait()
    waiting = [asyncio.create_task(executor.run(hold)) for _ in range(2)]
    await asyncio.sleep(0)
    with pytest.raises(BlockingIOError, match="queue is full"):
        await executor.run(lambda: pytest.fail("rejected operation ran"))
    release.set()
    await asyncio.gather(active, *waiting)
    await executor.aclose()


@pytest.mark.asyncio
async def test_queue_timeout_releases_waiter_capacity():
    executor = BoundedExecutor(
        "timeout", PoolSettings(workers=1, max_waiters=1, queue_timeout_seconds=0.01)
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    active = asyncio.create_task(executor.run(hold))
    await entered.wait()
    with pytest.raises(BlockingIOError, match="queue timed out"):
        await executor.run(lambda: None)
    assert not executor._admission._waiters
    release.set()
    await active
    assert await executor.run(lambda: "available") == "available"
    await executor.aclose()


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_leak_a_permit(executor):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    active = asyncio.create_task(executor.run(hold))
    await entered.wait()
    waiting = asyncio.create_task(executor.run(lambda: None))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await active
    assert await executor.run(lambda: "available") == "available"
    await executor.aclose()


@pytest.mark.asyncio
async def test_shutdown_rejects_waiters_and_drains_active_operations(executor):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold():
        entered.set()
        await release.wait()

    active = asyncio.create_task(executor.run(hold))
    await entered.wait()
    waiting = asyncio.create_task(executor.run(lambda: None))
    await asyncio.sleep(0)
    closing = asyncio.create_task(executor.aclose())
    await asyncio.sleep(0)
    with pytest.raises(BlockingIOError, match="closed"):
        await waiting
    with pytest.raises(BlockingIOError, match="closed"):
        await executor.run(lambda: None)
    assert not closing.done()
    release.set()
    await asyncio.gather(active, closing)


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_sync_operation_cleanup(executor):
    started = threading.Event()
    release = threading.Event()
    completed = threading.Event()

    def operation():
        started.set()
        try:
            assert release.wait(timeout=5)
        finally:
            completed.set()

    active = asyncio.create_task(executor.run(operation))
    assert await asyncio.to_thread(started.wait, 5)
    try:
        active.cancel()
        await asyncio.sleep(0)
        active.cancel()
        await asyncio.sleep(0)
        assert not active.done()
        assert not completed.is_set()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await active
    assert completed.is_set()
    assert await executor.run(lambda: "available") == "available"
    await executor.aclose()


@pytest.mark.asyncio
async def test_operation_failure_releases_admission(executor):
    async def fail():
        raise ValueError("test failure")

    with pytest.raises(ValueError, match="test failure"):
        await executor.run(fail)
    assert await executor.run(lambda: "available") == "available"
    await executor.aclose()

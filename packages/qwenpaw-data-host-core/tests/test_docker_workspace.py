"""Current Docker boundary: cleanup cannot be skipped by stop requests."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import Bash, ToolChunk

from qwenpaw_data.host.core.workspace.docker_workspace import ManagedDockerBash


@pytest.mark.parametrize("transport_error", [False, True])
async def test_failed_cleanup_retires_workspace(transport_error):
    delegate = Bash()

    async def broken(**kwargs):
        if transport_error:
            raise OSError("Docker disconnected")
        yield ToolChunk(content=[TextBlock(text="failed")], state=ToolResultState.ERROR)

    delegate.call = broken
    close = AsyncMock()
    tool = ManagedDockerBash(delegate, cleanup_failure_handler=close)
    await tool._cleanup("/tmp/test-record", interrupted=True)
    close.assert_awaited_once()


async def test_repeated_cancellation_waits_for_cleanup_and_preserves_permissions():
    started, finish = asyncio.Event(), asyncio.Event()
    delegate = Bash()
    delegate.check_permissions = AsyncMock(return_value="original decision")

    async def delayed(**kwargs):
        started.set()
        await finish.wait()
        yield ToolChunk(content=[TextBlock(text="")])

    delegate.call = delayed
    close = AsyncMock()
    tool = ManagedDockerBash(delegate, cleanup_failure_handler=close)
    task = asyncio.create_task(tool._cleanup("/tmp/test-record", interrupted=True))
    await started.wait()
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    close.assert_not_awaited()
    original = {"command": "ls"}
    assert await tool.check_permissions(original, "context") == "original decision"
    delegate.check_permissions.assert_awaited_once_with(original, "context")

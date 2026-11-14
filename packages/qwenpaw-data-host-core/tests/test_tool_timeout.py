import asyncio
import json
from types import SimpleNamespace

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse
from qwenpaw_data.host.core.api.models.preferences import SetRuntimeSettingsRequest
from qwenpaw_data.host.core.settings.runtime import RuntimeSettings

from .test_tool_timeout_middleware import _middleware


@pytest.mark.asyncio
async def test_timeout_cancels_work_and_discards_partial_success():
    cancelled = asyncio.Event()

    async def slow(**kwargs):
        try:
            yield ToolResponse(content=[TextBlock(text="partial")])
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    items = [
        item
        async for item in _middleware(seconds=0.02).on_acting(
            None, {"tool_call": SimpleNamespace(name="mcp__db__execute_sql")}, slow
        )
    ]
    assert cancelled.is_set()
    assert len(items) == 1 and items[0].state == ToolResultState.ERROR
    assert json.loads(items[0].content[0].text)["error"]["code"] == "TOOL_TIMEOUT"


@pytest.mark.asyncio
async def test_outer_cancellation_cleans_up_and_propagates():
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow(**kwargs):
        try:
            started.set()
            await asyncio.sleep(10)
            yield None
        finally:
            cancelled.set()

    async def collect():
        return [
            item
            async for item in _middleware(seconds=30).on_acting(
                None, {"tool_call": SimpleNamespace(name="Bash")}, slow
            )
        ]

    task = asyncio.create_task(collect())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_fast_tool_and_tool_error_unchanged():
    expected = ToolResponse(
        state=ToolResultState.ERROR, content=[TextBlock(text="bad query")]
    )

    async def fast(**kwargs):
        yield expected

    assert [
        item
        async for item in _middleware(seconds=1).on_acting(
            None, {"tool_call": SimpleNamespace(name="tool")}, fast
        )
    ] == [expected]


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf")])
def test_invalid_timeout_rejected(value):
    with pytest.raises(ValueError):
        RuntimeSettings(tool_timeout_seconds=value)
    with pytest.raises(ValueError):
        SetRuntimeSettingsRequest(tool_timeout_seconds=value)

# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse

from qwenpaw_data.host.core.agent.middleware.tool_timeout import ToolTimeoutMiddleware

_PARTIAL = SimpleNamespace(kind="chunk", text="partial")
_DONE = ToolResponse(content=[TextBlock(text="done")], state=ToolResultState.SUCCESS)


def _call(name: str) -> dict[str, Any]:
    return {"tool_call": SimpleNamespace(name=name)}


def _middleware(*, seconds: float, enabled: bool = True) -> ToolTimeoutMiddleware:
    return ToolTimeoutMiddleware(
        run_context=SimpleNamespace(
            user_runtime_config=SimpleNamespace(
                tool_timeout_enabled=enabled,
                tool_timeout_seconds=seconds,
            ),
        ),
    )


async def _collect(name: str, next_handler, *, seconds: float, enabled: bool = True):
    return [
        item
        async for item in _middleware(seconds=seconds, enabled=enabled).on_acting(
            SimpleNamespace(),
            _call(name),
            next_handler,
        )
    ]


def _slow_handler(*, delay: float):
    async def next_handler(**_kwargs: Any):
        yield _PARTIAL
        await asyncio.sleep(delay)
        yield _DONE

    return next_handler


@pytest.mark.asyncio
async def test_timeout_yields_only_error() -> None:
    items = await _collect("Bash", _slow_handler(delay=0.3), seconds=0.05)
    assert len(items) == 1
    assert isinstance(items[0], ToolResponse)
    assert items[0].state == ToolResultState.ERROR
    error = json.loads(items[0].content[0].text)["error"]
    assert error == {
        "code": "TOOL_TIMEOUT",
        "tool": "Bash",
        "timeout_seconds": 0.05,
        "message": (
            "Tool execution timed out. No complete result was accepted; "
            "do not claim success."
        ),
    }
    assert _PARTIAL not in items
    assert _DONE not in items


@pytest.mark.asyncio
async def test_on_time_forwards_chunks() -> None:
    items = await _collect("Bash", _slow_handler(delay=0), seconds=1)
    assert items == [_PARTIAL, _DONE]


@pytest.mark.asyncio
async def test_disabled_timeout_forwards() -> None:
    items = await _collect(
        "Bash", _slow_handler(delay=0.05), seconds=0.01, enabled=False
    )
    assert items == [_PARTIAL, _DONE]


@pytest.mark.asyncio
async def test_timeout_does_not_forward_toolkit_interrupted() -> None:
    interrupted = ToolResponse(
        content=[
            TextBlock(
                text=(
                    "<system-reminder>"
                    "The tool call has been interrupted by the user."
                    "</system-reminder>"
                ),
            )
        ],
        state=ToolResultState.INTERRUPTED,
    )

    async def next_handler(**_kwargs: Any):
        try:
            yield _PARTIAL
            await asyncio.sleep(10)
            yield _DONE
        except asyncio.CancelledError:
            yield interrupted

    items = await _collect("Bash", next_handler, seconds=0.05)
    assert len(items) == 1
    assert items[0].state == ToolResultState.ERROR
    assert interrupted not in items
    assert _PARTIAL not in items

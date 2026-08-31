# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentscope.event import EventType

from qwenpaw_data.host.core.runtime.envelope import Envelope


def _event(evt_type: str, **kwargs: Any) -> MagicMock:
    ev = MagicMock()
    ev.type = evt_type
    for key, value in kwargs.items():
        setattr(ev, key, value)
    return ev


@pytest.mark.asyncio
async def test_text_one_block_one_message_with_source_id() -> None:
    stream = MagicMock()
    stream.message_start = AsyncMock()
    stream.message_complete = AsyncMock()
    stream.text_delta = AsyncMock()
    stream.text_end = AsyncMock()
    stream.response_created = AsyncMock()
    stream.response_in_progress = AsyncMock()
    stream.response_completed = AsyncMock()
    stream.append = AsyncMock()
    stream.data_delta = AsyncMock()
    stream.data_end = AsyncMock()

    env = Envelope(stream)
    await env.begin()
    await env.translate_event(
        _event(EventType.TEXT_BLOCK_START.value, block_id="blk_a")
    )
    await env.translate_event(
        _event(EventType.TEXT_BLOCK_DELTA.value, block_id="blk_a", delta="hi")
    )
    await env.translate_event(
        _event(EventType.TEXT_BLOCK_END.value, block_id="blk_a")
    )
    await env.translate_event(
        _event(EventType.TEXT_BLOCK_START.value, block_id="blk_b")
    )
    await env.translate_event(
        _event(EventType.TEXT_BLOCK_DELTA.value, block_id="blk_b", delta="yo")
    )
    await env.translate_event(
        _event(EventType.TEXT_BLOCK_END.value, block_id="blk_b")
    )

    assert stream.message_start.await_count == 2
    assert stream.message_complete.await_count == 2
    assert stream.message_start.await_args_list[0].kwargs["source_id"] == "blk_a"
    assert stream.message_start.await_args_list[1].kwargs["source_id"] == "blk_b"
    assert stream.message_complete.await_args_list[0].kwargs["source_id"] == "blk_a"
    assert stream.message_complete.await_args_list[1].kwargs["source_id"] == "blk_b"


@pytest.mark.asyncio
async def test_tool_call_and_output_share_source_id() -> None:
    stream = MagicMock()
    stream.message_start = AsyncMock()
    stream.message_complete = AsyncMock()
    stream.text_delta = AsyncMock()
    stream.text_end = AsyncMock()
    stream.response_created = AsyncMock()
    stream.response_in_progress = AsyncMock()
    stream.response_completed = AsyncMock()
    stream.append = AsyncMock()
    stream.data_delta = AsyncMock()
    stream.data_end = AsyncMock()

    env = Envelope(stream)
    await env.translate_event(
        _event(
            EventType.TOOL_CALL_START.value,
            tool_call_id="call_9",
            tool_call_name="Write",
        )
    )
    await env.translate_event(
        _event(
            EventType.TOOL_CALL_DELTA.value,
            tool_call_id="call_9",
            delta='{"x":1}',
        )
    )
    await env.translate_event(
        _event(EventType.TOOL_CALL_END.value, tool_call_id="call_9")
    )
    await env.translate_event(
        _event(
            EventType.TOOL_RESULT_START.value,
            tool_call_id="call_9",
            tool_call_name="Write",
        )
    )
    await env.translate_event(
        _event(
            EventType.TOOL_RESULT_TEXT_DELTA.value,
            tool_call_id="call_9",
            delta="ok",
        )
    )
    await env.translate_event(
        _event(
            EventType.TOOL_RESULT_END.value,
            tool_call_id="call_9",
            state="success",
        )
    )

    call_start = stream.message_start.await_args_list[0].kwargs
    out_start = stream.message_start.await_args_list[1].kwargs
    assert call_start["type"] == "plugin_call"
    assert out_start["type"] == "plugin_call_output"
    assert call_start["source_id"] == "call_9"
    assert out_start["source_id"] == "call_9"
    assert call_start["msg_id"] != out_start["msg_id"]

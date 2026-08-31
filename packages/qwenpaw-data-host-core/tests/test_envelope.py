# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

import pytest
from agentscope.event import (
    HintBlockEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
    UserInterruptEvent,
)
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.runtime.envelope import Envelope


class RecordingStream:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str):
        async def record(*args: Any, **kwargs: Any) -> None:
            if args:
                assert len(args) == 1 and isinstance(args[0], dict)
                self.calls.append((name, args[0]))
            else:
                self.calls.append((name, kwargs))

        return record


async def test_envelope_uses_host_ids_and_agentscope_source_ids() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)

    await envelope.begin()
    await envelope.translate_event(
        TextBlockStartEvent(reply_id="reply-1", block_id="text-1")
    )
    await envelope.translate_event(
        TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="hello")
    )
    await envelope.translate_event(
        TextBlockEndEvent(reply_id="reply-1", block_id="text-1")
    )
    await envelope.translate_event(
        ToolCallStartEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            tool_call_name="read_file",
        )
    )
    await envelope.translate_event(
        ToolCallEndEvent(reply_id="reply-1", tool_call_id="call-1")
    )
    await envelope.translate_event(
        ToolResultStartEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            tool_call_name="read_file",
        )
    )
    await envelope.translate_event(
        ToolResultTextDeltaEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            delta="done",
        )
    )
    await envelope.translate_event(
        ToolResultEndEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            state=ToolResultState.SUCCESS,
        )
    )
    await envelope.complete()

    starts = [payload for name, payload in stream.calls if name == "message_start"]
    assert len(starts) == 3
    assert len({payload["msg_id"] for payload in starts}) == 3
    assert all(payload["msg_id"] not in {"text-1", "call-1"} for payload in starts)
    assert [payload["source_id"] for payload in starts] == [
        "text-1",
        "call-1",
        "call-1",
    ]
    assert stream.calls[-1][0] == "response_completed"


async def test_envelope_renders_hint_block() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)
    source = '{"kind":"plan"}'

    await envelope.translate_event(
        TextBlockStartEvent(reply_id="reply-1", block_id="text-1")
    )
    await envelope.translate_event(
        TextBlockDeltaEvent(reply_id="reply-1", block_id="text-1", delta="partial")
    )
    await envelope.translate_event(
        HintBlockEvent(
            reply_id="reply-1",
            block_id="hint-1",
            hint="continue the current plan",
            source=source,
        )
    )

    starts = [payload for name, payload in stream.calls if name == "message_start"]
    completes = [
        payload for name, payload in stream.calls if name == "message_complete"
    ]
    assert [payload["type"] for payload in starts] == ["message", "hint"]
    assert [payload["type"] for payload in completes] == ["message", "hint"]
    assert [payload["role"] for payload in starts] == ["assistant", "user"]
    assert completes[0]["content"][0]["text"] == "partial"
    assert starts[1]["source_id"] == "hint-1"
    assert starts[1]["metadata"] == {"source": source}
    assert completes[1]["metadata"] == {"source": source}
    assert completes[1]["content"] == [
        {
            "object": "content",
            "type": "text",
            "delta": False,
            "index": 0,
            "text": "continue the current plan",
        }
    ]
    assert not any(name == "segment" for name, _ in stream.calls)


async def test_envelope_hint_block_without_source_has_null_metadata() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)

    await envelope.translate_event(
        HintBlockEvent(
            reply_id="reply-1",
            block_id="hint-2",
            hint="plain hint",
        )
    )

    starts = [payload for name, payload in stream.calls if name == "message_start"]
    completes = [
        payload for name, payload in stream.calls if name == "message_complete"
    ]
    assert len(starts) == 1
    assert starts[0]["type"] == "hint"
    assert starts[0]["role"] == "user"
    assert starts[0]["source_id"] == "hint-2"
    assert starts[0]["metadata"] is None
    assert completes[0]["metadata"] is None
    assert not any(name == "segment" for name, _ in stream.calls)


async def test_envelope_preserves_tool_call_metadata() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)
    metadata = {"expires_at": "2026-08-03T09:00:00Z"}

    await envelope.translate_event(
        ToolCallStartEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            tool_call_name="ask_user_question",
            metadata=metadata,
        )
    )
    await envelope.translate_event(
        ToolCallEndEvent(reply_id="reply-1", tool_call_id="call-1")
    )

    starts = [payload for name, payload in stream.calls if name == "message_start"]
    completes = [
        payload for name, payload in stream.calls if name == "message_complete"
    ]
    assert starts[0]["metadata"] == metadata
    assert completes[0]["metadata"] == metadata


async def test_envelope_ignores_agent_internal_confirm_events() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)

    await envelope.translate_event(
        RequireUserConfirmEvent(reply_id="reply-1", tool_calls=[])
    )
    await envelope.translate_event(UserInterruptEvent(reply_id="reply-1"))

    assert stream.calls == []


async def test_envelope_rejects_unknown_event() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream)

    class MysteryEvent:
        type = "mystery"

    with pytest.raises(ValueError, match="unsupported agent event"):
        await envelope.translate_event(MysteryEvent())

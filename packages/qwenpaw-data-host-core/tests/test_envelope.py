# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from agentscope.event import (
    ExceedMaxItersEvent,
    HintBlockEvent,
    ModelCallEndEvent,
    ModelCallStartEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState

from qwenpaw_data.host.core.api.models.stream_objects import parse_stream_object
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
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]

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
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
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
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]

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


async def test_envelope_steer_hint_keeps_artifact_comments() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
    comments = [
        {
            "path": "russia_dau_mom.sql",
            "line_start": 2,
            "line_end": 10,
            "comment": "加一个参数",
        }
    ]

    await envelope.translate_event(
        HintBlockEvent(
            reply_id="reply-1",
            block_id="hint-3",
            hint="nudge",
            source="steer",
            metadata={"artifact_comments": comments},
        )
    )

    starts = [payload for name, payload in stream.calls if name == "message_start"]
    completes = [
        payload for name, payload in stream.calls if name == "message_complete"
    ]
    assert starts[0]["metadata"] == {
        "source": "steer",
        "artifact_comments": comments,
    }
    assert completes[0]["metadata"] == {
        "source": "steer",
        "artifact_comments": comments,
    }


async def test_envelope_preserves_tool_call_metadata() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
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


@pytest.mark.filterwarnings(
    "ignore:ExceedMaxItersEvent is deprecated:DeprecationWarning"
)
async def test_deprecated_exceed_max_iters_event_does_not_end_the_reply() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
    await envelope.begin()
    await envelope.translate_event(
        TextBlockStartEvent(reply_id="reply-1", block_id="text-1")
    )
    await envelope.translate_event(
        TextBlockDeltaEvent(
            reply_id="reply-1", block_id="text-1", delta="partial answer"
        )
    )
    await envelope.translate_event(
        ExceedMaxItersEvent(reply_id="reply-1", name="qwenpaw-data")
    )
    await envelope.complete()

    assert stream.calls[-1][0] == "response_completed"
    assert not any(name == "response_failed" for name, _ in stream.calls)
    messages = [payload for name, payload in stream.calls if name == "message_complete"]
    assert len(messages) == 1
    assert messages[0]["content"][0]["text"] == "partial answer"
    assert "[Warning]" not in str(stream.calls)


async def test_envelope_emits_model_call_start() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]

    await envelope.translate_event(
        ModelCallStartEvent(reply_id="reply-1", model_name="qwen-plus")
    )

    calls = [payload for name, payload in stream.calls if name == "model_call"]
    assert calls == [{"model_name": "qwen-plus"}]


def test_parse_model_call_event() -> None:
    obj = parse_stream_object(
        {
            "object": "model.call",
            "sequence_number": 1,
            "session_id": "ses_1",
            "chat_id": "chat_1",
            "created_at": "2026-11-04T08:00:00+00:00",
            "model_name": "qwen-plus",
        }
    )
    assert obj.object == "model.call"
    assert obj.model_name == "qwen-plus"


def test_parse_model_call_end_event() -> None:
    obj = parse_stream_object(
        {
            "object": "model.call.end",
            "sequence_number": 2,
            "session_id": "ses_1",
            "chat_id": "chat_1",
            "created_at": "2026-11-04T08:00:01+00:00",
            "input_tokens": 10,
            "output_tokens": 4,
        }
    )
    assert obj.object == "model.call.end"
    assert obj.input_tokens == 10
    assert obj.output_tokens == 4


def test_parse_tool_result_end_event() -> None:
    obj = parse_stream_object(
        {
            "object": "tool.result.end",
            "sequence_number": 3,
            "session_id": "ses_1",
            "chat_id": "chat_1",
            "created_at": "2026-11-04T08:00:02+00:00",
            "tool_name": "read_file",
            "call_id": "call-1",
        }
    )
    assert obj.object == "tool.result.end"
    assert obj.tool_name == "read_file"
    assert obj.call_id == "call-1"


async def test_envelope_emits_model_call_end() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
    await envelope.translate_event(
        ModelCallStartEvent(reply_id="reply-1", model_name="qwen-plus")
    )
    await envelope.translate_event(
        ModelCallEndEvent(reply_id="reply-1", input_tokens=3, output_tokens=5)
    )

    ends = [payload for name, payload in stream.calls if name == "model_call_end"]
    assert ends == [{"input_tokens": 3, "output_tokens": 5}]


async def test_envelope_emits_tool_result_end() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]
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
        ToolResultEndEvent(
            reply_id="reply-1",
            tool_call_id="call-1",
            state=ToolResultState.SUCCESS,
        )
    )

    ends = [payload for name, payload in stream.calls if name == "tool_result_end"]
    starts = [payload for name, payload in stream.calls if name == "tool_result_start"]
    assert starts == [{"tool_name": "read_file", "call_id": "call-1"}]
    assert ends == [
        {
            "tool_name": "read_file",
            "call_id": "call-1",
        }
    ]


async def test_envelope_emits_plan_tool_result_end() -> None:
    stream = RecordingStream()
    envelope = Envelope(stream, session_factory=MagicMock())  # type: ignore[arg-type]

    await envelope.translate_event(
        ToolCallStartEvent(
            reply_id="reply-1",
            tool_call_id="call-plan",
            tool_call_name="TaskStateUpdate",
        )
    )
    await envelope.translate_event(
        ToolCallEndEvent(reply_id="reply-1", tool_call_id="call-plan")
    )
    await envelope.translate_event(
        ToolResultStartEvent(
            reply_id="reply-1",
            tool_call_id="call-plan",
            tool_call_name="TaskStateUpdate",
        )
    )
    await envelope.translate_event(
        ToolResultEndEvent(
            reply_id="reply-1",
            tool_call_id="call-plan",
            state=ToolResultState.SUCCESS,
        )
    )

    ends = [payload for name, payload in stream.calls if name == "tool_result_end"]
    assert ends == [
        {
            "tool_name": "TaskStateUpdate",
            "call_id": "call-plan",
        }
    ]

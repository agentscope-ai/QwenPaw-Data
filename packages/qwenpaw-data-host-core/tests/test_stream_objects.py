from __future__ import annotations

import pytest

from qwenpaw_data.host.core.api.models.stream_objects import (
    dump_stream_object,
    parse_stream_object,
)

_BASE = {
    "sequence_number": 3, "session_id": "s1", "chat_id": "c1",
    "created_at": "2026-01-01T00:00:00Z",
}


def test_parse_response_event() -> None:
    obj = parse_stream_object(
        {**_BASE, "object": "response", "id": "resp_1", "status": "completed"},
    )
    assert obj.object == "response"
    dumped = dump_stream_object(obj)
    assert dumped["status"] == "completed"
    assert "usage" not in dumped  # exclude_none


def test_parse_message_and_content_events() -> None:
    message = parse_stream_object(
        {
            **_BASE,
            "object": "message",
            "id": "msg_1",
            "sequence": 1,
            "type": "message",
            "role": "assistant",
            "status": "in_progress",
        },
    )
    assert message.object == "message"

    content = parse_stream_object(
        {
            **_BASE,
            "object": "content",
            "msg_id": "msg_1",
            "type": "text",
            "delta": True,
            "text": "hello",
        },
    )
    assert content.object == "content"
    assert content.text == "hello"


def test_content_event_rejects_invalid_modality_combo() -> None:
    with pytest.raises(ValueError):
        parse_stream_object(
            {
                **_BASE,
                "object": "content",
                "msg_id": "msg_1",
                "type": "nonsense",
            },
        )


def test_parse_biz_event_segment_artifact_followup() -> None:
    biz = parse_stream_object(
        {
            **_BASE,
            "object": "biz_event",
            "biz_event": {"event_id": "e1", "seq": 1},
        },
    )
    assert biz.biz_event.event_id == "e1"

    segment = parse_stream_object(
        {
            **_BASE,
            "object": "segment",
            "segment": {
                "segment_id": "seg1",
                "title": "t",
                "coverage": {"start_seq": 0, "end_seq": 2},
            },
        },
    )
    assert segment.segment.segment_id == "seg1"

    artifact = parse_stream_object(
        {
            **_BASE,
            "object": "artifact.registered",
            "artifact": {
                "id": "a1",
                "session_id": "s1",
                "name": "report.md",
                "path": "artifacts/report.md",
                "created_at": "2026-01-01T00:00:00Z",
                "updated_at": "2026-01-01T00:00:00Z",
            },
        },
    )
    assert artifact.artifact.name == "report.md"

    followup = parse_stream_object(
        {
            **_BASE,
            "object": "followup.generated",
            "followup": {"questions": ["next?"]},
        },
    )
    assert followup.followup.questions == ["next?"]


def test_retired_dag_task_status_event_rejected() -> None:
    # The current task-plan API no longer emits legacy DAG snapshots over SSE.
    with pytest.raises(ValueError, match="task_status"):
        parse_stream_object(
            {
                **_BASE, "object": "task_status", "event_type": "graph_updated",
                "graph_snapshot": {"nodes": []},
            },
        )


@pytest.mark.parametrize("kind", ["tool.result.start", "tool.result.end"])
def test_tool_result_events_round_trip(kind: str) -> None:
    payload = {**_BASE, "object": kind, "tool_name": "execute_sql", "call_id": "call-1"}
    obj = parse_stream_object(payload)
    dumped = dump_stream_object(obj)
    assert dumped["object"] == kind
    assert dumped["call_id"] == "call-1"
    assert dumped["tool_name"] == "execute_sql"
    assert "created_at" in dumped


def test_unknown_object_rejected() -> None:
    with pytest.raises(ValueError):
        parse_stream_object({**_BASE, "object": "mystery"})

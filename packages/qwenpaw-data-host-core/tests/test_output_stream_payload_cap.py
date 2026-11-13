from __future__ import annotations

from pathlib import Path

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.store.json_store import JSONChatEventStore
from qwenpaw_data.host.core.stream import output_stream as output_stream_module
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream


def _stream(tmp_path: Path) -> OutputStream:
    return OutputStream(
        JSONChatEventStore(tmp_path),
        session_id="sess1",
        chat_id="chat-1",
        identity=Identity.anonymous(),
    )


async def test_oversized_tool_output_is_truncated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(output_stream_module, "_MAX_EVENT_BYTES", 1024)
    reset_hub()
    try:
        stream = _stream(tmp_path)
        payload = "x" * 5000
        event = await stream.data_end(
            msg_id="msg-1",
            index=0,
            data={"call_id": "call-1", "name": "execute_sql", "output": payload},
        )

        assert event.data["truncated"] is True
        body = event.data["output"]
        assert "truncated" in body
        assert body.startswith("x" * 10)
        assert body.endswith("x" * 10)
        assert len(body.encode("utf-8")) < 1200
    finally:
        reset_hub()


async def test_small_tool_output_is_untouched(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(output_stream_module, "_MAX_EVENT_BYTES", 1024)
    reset_hub()
    try:
        stream = _stream(tmp_path)
        event = await stream.data_end(
            msg_id="msg-1",
            index=0,
            data={"call_id": "call-1", "name": "execute_sql", "output": "ok"},
        )

        assert "truncated" not in event.data
        assert event.data["output"] == "ok"
    finally:
        reset_hub()


async def test_oversized_message_content_is_truncated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(output_stream_module, "_MAX_EVENT_BYTES", 1024)
    reset_hub()
    try:
        stream = _stream(tmp_path)
        event = await stream.message_complete(
            msg_id="msg-1",
            sequence=1,
            type="plugin_call_output",
            role="tool",
            content=[
                {
                    "object": "content",
                    "type": "data",
                    "delta": False,
                    "data": {
                        "call_id": "call-1",
                        "name": "execute_sql",
                        "output": "y" * 4000,
                    },
                }
            ],
        )

        block = event.content[0]
        assert block.data["truncated"] is True
        assert len(block.data["output"].encode("utf-8")) < 1200
    finally:
        reset_hub()


async def test_cap_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(output_stream_module, "_MAX_EVENT_BYTES", 0)
    reset_hub()
    try:
        stream = _stream(tmp_path)
        payload = "z" * 4000
        event = await stream.data_end(
            msg_id="msg-1",
            index=0,
            data={"call_id": "call-1", "name": "execute_sql", "output": payload},
        )
        assert event.data["output"] == payload
    finally:
        reset_hub()


async def test_delta_fragments_are_never_truncated(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(output_stream_module, "_MAX_EVENT_BYTES", 16)
    reset_hub()
    try:
        stream = _stream(tmp_path)
        payload = "fragment-" * 100
        event = await stream.data_delta(
            msg_id="msg-1",
            index=0,
            data={"call_id": "call-1", "name": "execute_sql", "output": payload},
        )

        assert event.data["output"] == payload
        assert "truncated" not in event.data
    finally:
        reset_hub()

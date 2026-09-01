# -*- coding: utf-8 -*-
"""End-to-end smoke: POST a turn, consume SSE, resume, and fail paths."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402
from agentscope.event import (  # noqa: E402
    ModelCallEndEvent,
    ReplyEndEvent,
    ReplyStartEvent,
    RequireUserConfirmEvent,
    TextBlockDeltaEvent,
    TextBlockEndEvent,
    TextBlockStartEvent,
    ToolCallEndEvent,
    ToolCallStartEvent,
    ToolResultEndEvent,
    ToolResultStartEvent,
    ToolResultTextDeltaEvent,
)
from agentscope.message import ToolResultState  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.core import QwenPawDataHost  # noqa: E402
from qwenpaw_data.host.core.store.json_store import JSONChatStore  # noqa: E402


def _script() -> list[Any]:
    return [
        ReplyStartEvent(
            session_id="s1", reply_id="r1", name="qwenpaw-data", role="assistant"
        ),
        TextBlockStartEvent(reply_id="r1", block_id="blk-1"),
        TextBlockDeltaEvent(reply_id="r1", block_id="blk-1", delta="hello "),
        TextBlockDeltaEvent(reply_id="r1", block_id="blk-1", delta="world"),
        TextBlockEndEvent(reply_id="r1", block_id="blk-1"),
        ToolCallStartEvent(
            reply_id="r1", tool_call_id="call-1", tool_call_name="execute_sql"
        ),
        ToolCallEndEvent(reply_id="r1", tool_call_id="call-1"),
        ToolResultStartEvent(
            reply_id="r1", tool_call_id="call-1", tool_call_name="execute_sql"
        ),
        ToolResultTextDeltaEvent(reply_id="r1", tool_call_id="call-1", delta="42"),
        ToolResultEndEvent(
            reply_id="r1", tool_call_id="call-1", state=ToolResultState.SUCCESS
        ),
        # Agent-internal confirmation events must be silently ignored.
        RequireUserConfirmEvent(reply_id="r1", tool_calls=[]),
        ModelCallEndEvent(reply_id="r1", input_tokens=7, output_tokens=3),
        ReplyEndEvent(session_id="s1", reply_id="r1"),
    ]


class ScriptedState:
    def __init__(self) -> None:
        self.context: list[Any] = []


class ScriptedAgent:
    def __init__(
        self,
        events: list[Any] | None = None,
        *,
        gate: asyncio.Event | None = None,
        boom: bool = False,
    ) -> None:
        self._events = events or []
        self._gate = gate
        self._boom = boom
        self.name = "qwenpaw-data"
        self.state = ScriptedState()
        self._reasoning_middlewares: list[Any] = []

    async def reply_stream(self, _inputs: Any):
        if self._gate is not None:
            await self._gate.wait()
        if self._boom:
            raise RuntimeError("model exploded")
        for event in self._events:
            yield event


@asynccontextmanager
async def service_client(tmp_path: Path, agent: ScriptedAgent, monkeypatch):
    async def fake_get_agent(self, *, mode: str, request_context=None):
        return agent

    monkeypatch.setattr(QwenPawDataHost, "get_agent", fake_get_agent)
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, app


async def _collect_sse(http: httpx.AsyncClient, url: str, **kwargs: Any):
    """Consume an SSE stream to completion, returning (ids, payloads)."""
    ids: list[int] = []
    payloads: list[dict[str, Any]] = []
    async with http.stream("GET", url, **kwargs) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            elif line.startswith("data: "):
                payloads.append(json.loads(line[6:]))
    return ids, payloads


async def _wait_terminal(tmp_path: Path, chat_id: str) -> dict[str, Any]:
    store = JSONChatStore(tmp_path / "host")
    for _ in range(200):
        chat = await store.get(chat_id)
        if chat.status != "running":
            return {"status": chat.status, "error": chat.error}
        await asyncio.sleep(0.02)
    raise AssertionError("chat never reached a terminal status")


async def _new_session(http: httpx.AsyncClient) -> str:
    response = await http.post("/api/v1/sessions", json={"title": "smoke"})
    assert response.status_code == 200
    return response.json()["session"]["id"]



async def test_full_turn_streams_expected_event_sequence(
    tmp_path, monkeypatch
) -> None:
    async with service_client(tmp_path, ScriptedAgent(_script()), monkeypatch) as (
        http,
        _app,
    ):
        session_id = await _new_session(http)
        created = await http.post(
            f"/api/v1/sessions/{session_id}/chats",
            json={"text": "run the numbers", "datasource_id": "ds1"},
        )
        assert created.status_code == 200
        chat = created.json()["chat"]
        chat_id = chat["id"]
        assert chat["status"] == "running"
        assert chat["sequence"] == 1

        # A second turn while one is active conflicts.
        conflict = await http.post(
            f"/api/v1/sessions/{session_id}/chats",
            json={"text": "another"},
        )
        assert conflict.status_code == 409

        ids, payloads = await _collect_sse(
            http, f"/api/v1/sessions/{session_id}/chats/{chat_id}/events"
        )
        assert ids == list(range(len(ids)))  # dense monotonic SSE ids

        objects = [p["object"] for p in payloads]
        responses = [p for p in payloads if p["object"] == "response"]
        assert [r["status"] for r in responses] == [
            "created",
            "in_progress",
            "completed",
        ]
        assert responses[-1]["usage"] == {"input_tokens": 7, "output_tokens": 3}

        messages = [p for p in payloads if p["object"] == "message"]
        assert [m["type"] for m in messages] == [
            "message",
            "message",
            "plugin_call",
            "plugin_call",
            "plugin_call_output",
            "plugin_call_output",
        ]
        # Both the tool call and its output complete with the shared source id.
        completed = [m for m in messages if m["status"] == "completed"]
        assert {m["source_id"] for m in completed if m["type"] != "message"} == {
            "call-1"
        }

        text_deltas = [
            p["text"]
            for p in payloads
            if p["object"] == "content" and p.get("delta") and p["type"] == "text"
        ]
        assert text_deltas == ["hello ", "world"]
        final_text = [
            p["text"]
            for p in payloads
            if p["object"] == "content"
            and not p.get("delta")
            and p["type"] == "text"
        ]
        assert final_text == ["hello world"]
        assert "error" not in objects

        final_chat = await _wait_terminal(tmp_path, chat_id)
        assert final_chat["status"] == "completed"


async def test_resume_with_last_event_id_replays_tail_only(
    tmp_path, monkeypatch
) -> None:
    async with service_client(tmp_path, ScriptedAgent(_script()), monkeypatch) as (
        http,
        _app,
    ):
        session_id = await _new_session(http)
        created = await http.post(
            f"/api/v1/sessions/{session_id}/chats", json={"text": "hi"}
        )
        chat_id = created.json()["chat"]["id"]
        await _wait_terminal(tmp_path, chat_id)

        full_ids, _ = await _collect_sse(
            http, f"/api/v1/sessions/{session_id}/chats/{chat_id}/events"
        )
        assert full_ids, "expected a replayed event history"

        resume_from = full_ids[len(full_ids) // 2]
        tail_ids, _ = await _collect_sse(
            http,
            f"/api/v1/sessions/{session_id}/chats/{chat_id}/events",
            headers={"Last-Event-ID": str(resume_from)},
        )
        assert tail_ids == full_ids[full_ids.index(resume_from) + 1 :]

        missing = await http.get("/api/v1/sessions/s1/chats/nope/events")
        assert missing.status_code == 404
        wrong_session = await http.get(
            f"/api/v1/sessions/other/chats/{chat_id}/events"
        )
        assert wrong_session.status_code == 404


async def test_live_subscription_receives_events_as_they_happen(
    tmp_path, monkeypatch
) -> None:
    gate = asyncio.Event()
    agent = ScriptedAgent(_script(), gate=gate)
    async with service_client(tmp_path, agent, monkeypatch) as (http, _app):
        session_id = await _new_session(http)
        created = await http.post(
            f"/api/v1/sessions/{session_id}/chats", json={"text": "hi"}
        )
        chat_id = created.json()["chat"]["id"]

        async def _open_gate() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        opener = asyncio.create_task(_open_gate())
        ids, payloads = await _collect_sse(
            http, f"/api/v1/sessions/{session_id}/chats/{chat_id}/events"
        )
        await opener

        assert ids == list(range(len(ids)))
        assert payloads[-1]["object"] == "response"
        assert payloads[-1]["status"] == "completed"


async def test_failing_agent_yields_response_failed(tmp_path, monkeypatch) -> None:
    async with service_client(
        tmp_path, ScriptedAgent(boom=True), monkeypatch
    ) as (http, _app):
        session_id = await _new_session(http)
        created = await http.post(
            f"/api/v1/sessions/{session_id}/chats", json={"text": "hi"}
        )
        chat_id = created.json()["chat"]["id"]

        final_chat = await _wait_terminal(tmp_path, chat_id)
        assert final_chat["status"] == "failed"
        assert final_chat["error"]["message"] == "model exploded"

        _ids, payloads = await _collect_sse(
            http, f"/api/v1/sessions/{session_id}/chats/{chat_id}/events"
        )
        assert payloads[-1]["object"] == "response"
        assert payloads[-1]["status"] == "failed"
        assert payloads[-1]["error"]["message"] == "model exploded"


async def test_orphaned_running_chats_are_cancelled_on_startup(
    tmp_path, monkeypatch
) -> None:
    gate = asyncio.Event()  # never opened: chat stays running at shutdown
    agent = ScriptedAgent(_script(), gate=gate)
    async with service_client(tmp_path, agent, monkeypatch) as (http, _app):
        session_id = await _new_session(http)
        created = await http.post(
            f"/api/v1/sessions/{session_id}/chats", json={"text": "hi"}
        )
        chat_id = created.json()["chat"]["id"]
        assert created.json()["chat"]["status"] == "running"

    # Restart the service on the same home: the orphan must be cancelled.
    async with service_client(
        tmp_path, ScriptedAgent(_script()), monkeypatch
    ) as (http, _app):
        stopped = await http.post(f"/api/v1/sessions/{session_id}/chats/{chat_id}/stop")
        assert stopped.json()["chat"]["status"] == "canceled"
        _ids, payloads = await _collect_sse(
            http, f"/api/v1/sessions/{session_id}/chats/{chat_id}/events"
        )
        assert payloads[-1]["object"] == "response"
        assert payloads[-1]["status"] == "cancelled"

"""Host HTTP/SQL/SSE smoke; only the agent and external Context are simulated."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from agentscope.event import (
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
    UserConfirmResultEvent,
)
from agentscope.message import ToolResultState
from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.runtime.builder.data_agent import DataAgentBuilder
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from qwenpaw_data.host.core.utils.time import utcnow
from .conftest import TEST_IDENTITY, _seed_test_preferences


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
        # SDK prompts must be denied without leaking to the wire.
        RequireUserConfirmEvent(reply_id="r1", tool_calls=[]),
        ModelCallEndEvent(reply_id="r1", input_tokens=7, output_tokens=3),
        ReplyEndEvent(session_id="s1", reply_id="r1"),
    ]


class ScriptedAgent:
    def __init__(self, *, gate=None, boom=False):
        self.gate, self.boom = gate, boom
        self.started = asyncio.Event()
        self.confirmations = []
        self.name = "qwenpaw-data"
        self._reply_middlewares = []

    async def reply_stream(self, inputs):
        if isinstance(inputs, UserConfirmResultEvent):
            self.confirmations.append(inputs)
            return
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.boom:
            raise RuntimeError("model exploded")
        for event in _script():
            yield event


@asynccontextmanager
async def service_client(agent, monkeypatch):
    async def build(self, *, run_context, envelope, state):
        agent.state = state
        return agent

    monkeypatch.setattr(DataAgentBuilder, "build", build)
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.McpRepository.sync",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "qwenpaw_data.host.core.runtime.chat_runtime.fetch_datasource_type",
        AsyncMock(return_value=None),
    )
    app = create_app()
    async with app.router.lifespan_context(app):
        await _seed_test_preferences(get_session_factory())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
            trust_env=False,
        ) as http:
            yield http


async def _collect_sse(http, url, **kwargs):
    ids, payloads = [], []
    async with http.stream("GET", url, **kwargs) as response:
        assert response.status_code == 200, await response.aread()
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                ids.append(int(line[4:]))
            elif line.startswith("data: "):
                payloads.append(json.loads(line[6:]))
    return ids, payloads


async def _new_session(http):
    response = await http.post("/api/sessions", json={"title": "smoke"})
    assert response.status_code == 200, response.text
    return response.json()["session"]["id"]


async def _post_turn(http, session_id, *, legacy=False):
    url = f"/api/v1/sessions/{session_id}/chats" if legacy else "/api/console/chat"
    body = {"text": "hi"} if legacy else {"session_id": session_id, "text": "hi"}
    response = await http.post(url, json=body)
    assert response.status_code == 200, response.text
    return response.json()["chat"]


async def _chat(chat_id):
    async with get_session_factory()() as db:
        return await ChatRepository(db, TEST_IDENTITY).get(chat_id)


async def _session_chat(session_id):
    async with get_session_factory()() as db:
        chats = await ChatRepository(db, TEST_IDENTITY).list_for_session(session_id)
        assert len(chats) == 1
        return chats[0]


def _url(session_id, chat_id):
    return f"/api/sessions/{session_id}/chats/{chat_id}/events"


async def _cleanup(*tasks):
    for task in tasks:
        if task is not None and not task.done():
            task.cancel()
    await asyncio.gather(*[t for t in tasks if t is not None], return_exceptions=True)


@pytest.mark.parametrize("legacy", [False, True])
async def test_completed_turn_replays_messages_usage_and_cursor(monkeypatch, legacy):
    agent = ScriptedAgent()
    async with service_client(agent, monkeypatch) as http:
        session_id = await _new_session(http)
        created = await _post_turn(http, session_id, legacy=legacy)
        chat_id = created["id"]
        assert created["status"] == "running"
        assert created["sequence"] == 1
        assert (await _chat(chat_id)).status == "completed"
        ids, payloads = await _collect_sse(http, _url(session_id, chat_id))
        assert ids == sorted(set(ids))
        assert len(ids) > 5
        responses = [p for p in payloads if p["object"] == "response"]
        assert [p["status"] for p in responses] == [
            "created",
            "in_progress",
            "completed",
        ]
        assert responses[-1]["usage"] == {"input_tokens": 7, "output_tokens": 3}
        # Terminal compaction retains completed messages, not content deltas.
        assert not any(p["object"] == "content" for p in payloads)
        messages = [p for p in payloads if p["object"] == "message"]
        text = [
            p for p in messages if p["type"] == "message" and p["status"] == "completed"
        ]
        assert text[0]["content"][0]["text"] == "hello world"
        tools = [
            p for p in messages if p["type"] != "message" and p["status"] == "completed"
        ]
        assert {p["type"] for p in tools} == {"plugin_call", "plugin_call_output"}
        assert {p["source_id"] for p in tools} == {"call-1"}
        assert len(agent.confirmations) == 1
        assert not any("confirm" in p["object"] for p in payloads)
        cursor = ids[len(ids) // 2]
        tail, _ = await _collect_sse(
            http, _url(session_id, chat_id), headers={"Last-Event-ID": str(cursor)}
        )
        assert tail == ids[ids.index(cursor) + 1 :]
        assert await _collect_sse(
            http, _url(session_id, chat_id), headers={"Last-Event-ID": str(ids[-1])}
        ) == ([], [])
        other = await _new_session(http)
        assert (await http.get(_url(other, chat_id))).status_code == 404
        assert (await http.get(_url(session_id, "missing"))).status_code == 404


@pytest.mark.parametrize("cancel", [False, True])
async def test_running_turn_conflicts_and_stream_terminates(monkeypatch, cancel):
    agent = ScriptedAgent(gate=asyncio.Event())
    async with service_client(agent, monkeypatch) as http:
        session_id = await _new_session(http)
        # ASGITransport awaits background work, so POST must be concurrent.
        turn = asyncio.create_task(_post_turn(http, session_id))
        collector = None
        try:
            await asyncio.wait_for(agent.started.wait(), 5)
            chat = await _session_chat(session_id)
            conflict = await http.post(
                "/api/console/chat", json={"session_id": session_id, "text": "second"}
            )
            assert conflict.status_code == 409
            collector = asyncio.create_task(
                _collect_sse(http, _url(session_id, chat.id))
            )
            if cancel:
                stopped = await http.post(
                    "/api/console/chat/stop",
                    params={"session_id": session_id, "chat_id": chat.id},
                )
                assert stopped.status_code == 200, stopped.text
            else:
                agent.gate.set()
            await asyncio.wait_for(turn, 5)
            ids, payloads = await asyncio.wait_for(collector, 5)
            assert ids == sorted(set(ids))
            assert payloads[-1]["object"] == "response"
            assert payloads[-1]["status"] == ("cancelled" if cancel else "completed")
            assert (await _chat(chat.id)).status == (
                "canceled" if cancel else "completed"
            )
        finally:
            agent.gate.set()
            await _cleanup(turn, collector)


async def test_agent_failure_is_persisted_and_streamed(monkeypatch):
    async with service_client(ScriptedAgent(boom=True), monkeypatch) as http:
        session_id = await _new_session(http)
        created = await _post_turn(http, session_id)
        chat = await _chat(created["id"])
        assert chat.status == "failed"
        assert chat.error["code"] == "INTERNAL"
        assert chat.error["details"]["cause"] == "model exploded"
        _, payloads = await _collect_sse(http, _url(session_id, chat.id))
        assert payloads[-1]["status"] == "failed"
        assert payloads[-1]["error"] == chat.error


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_stream_waits_for_terminal_event_after_status_commit(
    monkeypatch, outcome
):
    pending, release, polled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_append, original_read = OutputStream.append, ChatEventRepository.read_after

    async def delayed_append(self, payload):
        if payload.get("object") == "response" and payload.get("status") == outcome:
            pending.set()
            await release.wait()
        return await original_append(self, payload)

    async def observed_read(self, *args, **kwargs):
        result = await original_read(self, *args, **kwargs)
        polled.set()
        return result

    monkeypatch.setattr(OutputStream, "append", delayed_append)
    monkeypatch.setattr(ChatEventRepository, "read_after", observed_read)
    async with service_client(
        ScriptedAgent(boom=outcome == "failed"), monkeypatch
    ) as http:
        session_id = await _new_session(http)
        turn = asyncio.create_task(_post_turn(http, session_id))
        collector = None
        try:
            await asyncio.wait_for(pending.wait(), 5)
            chat = await _session_chat(session_id)
            assert chat.status == outcome
            collector = asyncio.create_task(
                _collect_sse(http, _url(session_id, chat.id))
            )
            await asyncio.wait_for(polled.wait(), 5)
            # The route must remain open when status is saved but event is absent.
            await asyncio.sleep(0.1)
            assert not collector.done(), "SSE closed before terminal event commit"
            release.set()
            await asyncio.wait_for(turn, 5)
            ids, payloads = await asyncio.wait_for(collector, 5)
            assert payloads[-1]["status"] == outcome
            assert await _collect_sse(
                http, _url(session_id, chat.id), headers={"Last-Event-ID": str(ids[-1])}
            ) == ([], [])
        finally:
            release.set()
            await _cleanup(turn, collector)


async def test_restart_recovers_stale_chat_but_preserves_fresh_chat(monkeypatch):
    async with service_client(ScriptedAgent(), monkeypatch) as http:
        session_ids = [await _new_session(http) for _ in range(2)]
        chat_ids = []
        async with get_session_factory()() as db:
            for index, session_id in enumerate(session_ids):
                session = await SessionRepository(db, TEST_IDENTITY).get(session_id)
                chat = session.open_chat(
                    text="orphan", datasource_id=None, has_active_chat=False
                )
                chat.heartbeat_at = (
                    utcnow() - timedelta(hours=1) if index == 0 else utcnow()
                )
                await SessionRepository(db, TEST_IDENTITY).save(session)
                await ChatRepository(db, TEST_IDENTITY).add(chat)
                chat_ids.append(chat.id)
            await db.commit()
    async with service_client(ScriptedAgent(), monkeypatch) as http:
        assert (await _chat(chat_ids[0])).status == "canceled"
        assert (await _chat(chat_ids[1])).status == "running"
        _, payloads = await _collect_sse(http, _url(session_ids[0], chat_ids[0]))
        assert payloads[-1]["status"] == "cancelled"


async def test_json_mode_rejected_before_database_initialization(monkeypatch):
    monkeypatch.setenv("QWENPAW_DATA_STORE", "json")
    init = AsyncMock()
    monkeypatch.setattr("qwenpaw_data.host.core.api.app.init_db", init)
    app = create_app()
    with pytest.raises(RuntimeError, match="existing JSON data has not been migrated"):
        async with app.router.lifespan_context(app):
            pytest.fail("legacy JSON mode must not silently start a SQL Host")
    init.assert_not_called()

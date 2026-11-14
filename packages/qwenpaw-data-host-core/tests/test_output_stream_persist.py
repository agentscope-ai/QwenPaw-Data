# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.db.tables import ChatRow, SessionRow
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.stream.hub import get_hub, reset_hub
from qwenpaw_data.host.core.stream.output_stream import OutputStream

_IDENTITY = Identity(
    user_id="u1", tenant_id=TEST_TENANT_ID, workspace_id=TEST_WORKSPACE_ID
)


def test_timing_replay_survives_terminal_cleanup() -> None:
    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        stream = OutputStream(
            factory,
            session_id=session_id,
            chat_id=chat_id,
            identity=_IDENTITY,
            last_sequence_number=-1,
        )
        await stream.model_call(model_name="qwen")
        await stream.message_start(
            msg_id="m",
            sequence=1,
            type="message",
            role="assistant",
            source_id="block",
        )
        await stream.message_complete(
            msg_id="m",
            sequence=1,
            type="message",
            role="assistant",
            source_id="block",
            content=[{"object": "content", "type": "text", "text": "private body"}],
        )
        await stream.model_call_end()
        await stream.tool_result_start(tool_name="read_file", call_id="call")
        await stream.tool_result_end(tool_name="read_file", call_id="call")
        await stream.response_completed()
        async with factory() as db:
            records = await ChatEventRepository(db, _IDENTITY).read_timing_events(
                chat_id
            )
            from qwenpaw_data.host.core.api.routers.trace import trace

            replay = await trace(
                session_id, chat_id, identity=_IDENTITY, db=db, view="raw"
            )
            assert replay["timing_events"] == records
            business = await trace(
                session_id, chat_id, identity=_IDENTITY, db=db, view="business"
            )
            assert business["timing_events"] == []
        assert [row["object"] for row in records] == [
            "model.call",
            "message",
            "model.call.end",
            "tool.result.start",
            "tool.result.end",
        ]
        assert all(row["created_at"] for row in records)
        assert all("content" not in row for row in records)
        assert records[1]["source_id"] == "block"
        assert records[3]["call_id"] == records[4]["call_id"] == "call"
        await reset_engine()

    asyncio.run(scenario())


async def _prepare_db() -> tuple[str, str]:
    await reset_engine()
    reset_hub()
    await init_db()
    factory = get_session_factory()
    async with factory() as db:
        await db.flush()
        db.add(
            SessionRow(
                id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                title="t",
            )
        )
        await db.flush()
        db.add(
            ChatRow(
                id="chat_1",
                session_id="ses_1",
                user_id="u1",
                tenant_id=TEST_TENANT_ID,
                workspace_id=TEST_WORKSPACE_ID,
                sequence=1,
            )
        )
        await db.commit()
    return "ses_1", "chat_1"


async def test_sse_waits_for_terminal_publication_after_chat_status_commit():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from qwenpaw_data.host.core.api.routers.events import chat_events

    session_id, chat_id = await _prepare_db()
    factory = get_session_factory()
    stream = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=_IDENTITY,
        last_sequence_number=-1,
    )
    await stream.response_in_progress()
    async with factory() as db:
        chat = await db.get(ChatRow, chat_id)
        chat.status = "completed"
        await db.commit()
    request = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
    response = await chat_events(
        request,
        session_id,
        chat_id,
        identity=_IDENTITY,
        after_sequence_number=0,
        last_event_id=None,
    )
    waiting = asyncio.create_task(anext(response.body_iterator))
    try:
        await asyncio.sleep(0.05)
        assert not waiting.done(), "SSE closed before the terminal event was committed"
        terminal = await stream.response_completed()
        frame = await asyncio.wait_for(waiting, timeout=3)
        assert '"status":"completed"' in frame
        await response.body_iterator.aclose()
        replay = await chat_events(
            request,
            session_id,
            chat_id,
            identity=_IDENTITY,
            after_sequence_number=terminal.sequence_number,
            last_event_id=None,
        )
        assert [frame async for frame in replay.body_iterator] == []
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        await response.body_iterator.aclose()
        await reset_engine()


async def test_sse_bounds_terminal_rows_missing_their_final_event(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from qwenpaw_data.host.core.api.routers.events import chat_events
    from qwenpaw_data.host.core.settings import clear_settings

    monkeypatch.setenv("QWENPAW_DATA_STREAM_HEARTBEAT_TTL_SECONDS", "0.05")
    clear_settings()
    session_id, chat_id = await _prepare_db()
    try:
        async with get_session_factory()() as db:
            chat = await db.get(ChatRow, chat_id)
            chat.status = "completed"
            await db.commit()
        response = await chat_events(
            SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
            session_id,
            chat_id,
            identity=_IDENTITY,
            after_sequence_number=-1,
            last_event_id=None,
        )

        async def consume():
            return [frame async for frame in response.body_iterator]

        assert await asyncio.wait_for(consume(), timeout=2) == []
    finally:
        clear_settings()
        await reset_engine()


def test_output_stream_persists_events_for_replay(tmp_path: Path) -> None:
    async def scenario() -> None:
        session_id, chat_id = await _prepare_db()
        factory = get_session_factory()
        live: list[str] = []
        hub = get_hub()

        async def collect() -> None:
            async for obj in hub.subscribe_live(chat_id):
                if obj is None:
                    continue
                live.append(obj.object)
                if obj.object == "error":
                    return

        collector = asyncio.create_task(collect())
        await asyncio.sleep(0)
        stream = OutputStream(
            factory,
            session_id=session_id,
            chat_id=chat_id,
            identity=_IDENTITY,
            last_sequence_number=-1,
        )
        await stream.response_created()
        await stream.message_start(
            msg_id="msg_1",
            sequence=1,
            type="message",
            role="assistant",
        )
        await stream.text_delta(msg_id="msg_1", index=0, text="hello")
        await stream.message_complete(
            msg_id="msg_1",
            sequence=1,
            type="message",
            role="assistant",
            content=[{"object": "content", "type": "text", "text": "hello"}],
        )
        await stream.append(
            {
                "object": "message",
                "id": "msg_fail",
                "sequence": 2,
                "type": "message",
                "role": "assistant",
                "status": "failed",
                "content": [],
            }
        )
        await stream.append(
            {
                "object": "message",
                "id": "msg_cancel",
                "sequence": 3,
                "type": "message",
                "role": "assistant",
                "status": "cancelled",
                "content": [],
            }
        )
        await stream.biz_event(
            event_id="call_1",
            seq=1,
            presentation={"card_type": "tool", "caption": "x", "body": "y"},
            started_at=1.0,
        )
        await stream.append({"object": "error", "code": "VALIDATION", "message": "bad"})
        async with factory() as db:
            events = await ChatEventRepository(db, _IDENTITY).read_after(chat_id, -1)
            chat = await ChatRepository(db, _IDENTITY).get(chat_id)

        await asyncio.wait_for(collector, timeout=2)
        # Multi-POD refactor: every event (deltas included) is persisted so any
        # Host can replay the stream by sequence_number; the live hub carries the
        # same objects for in-process subscribers.
        assert [obj.object for obj in events] == [
            "response",
            "message",
            "content",
            "message",
            "message",
            "message",
            "biz_event",
            "error",
        ]
        assert [obj.status for obj in events if obj.object == "message"] == [
            "in_progress",
            "completed",
            "failed",
            "cancelled",
        ]
        assert chat.last_sequence_number == events[-1].sequence_number
        assert live == [
            "response",
            "message",
            "content",
            "message",
            "message",
            "message",
            "biz_event",
            "error",
        ]

    asyncio.run(scenario())

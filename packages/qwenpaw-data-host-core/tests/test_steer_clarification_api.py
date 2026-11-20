# -*- coding: utf-8 -*-
"""HTTP admission commits commands for the native SQL-backed runtime."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import httpx
from sqlalchemy import select

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import get_session_factory, reset_engine
from qwenpaw_data.host.core.db.tables import ChatCommandRow
from qwenpaw_data.host.core.domain.clarification import ClarificationWithExecutor
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ChatCommandRepository,
    ChatRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.settings import clear_settings

from .conftest import TEST_IDENTITY


@asynccontextmanager
async def service_client():
    clear_settings()
    await reset_engine()
    app = create_app()
    try:
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://localhost",
                trust_env=False,
            ) as http:
                yield http
    finally:
        await reset_engine()


async def _seed_running_chat() -> tuple[str, str]:
    session = Session.create(identity=TEST_IDENTITY)
    chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
    async with get_session_factory()() as db:
        await SessionRepository(db, TEST_IDENTITY).add(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
    return session.id, chat.id


async def _complete_chat(chat_id: str) -> None:
    async with get_session_factory()() as db:
        repo = ChatRepository(db, TEST_IDENTITY)
        chat = await repo.get(chat_id)
        chat.mark_status("completed")
        await repo.save(chat)
        await db.commit()


ANSWER_BODY = {
    "clarification_id": "clar_1",
    "result": {
        "status": "answered",
        "answers": [
            {
                "question": "哪个区域？",
                "selected_options": ["华东"],
                "custom_text": None,
            }
        ],
    },
}


async def test_steer_paths() -> None:
    async with service_client() as http:
        missing = await http.post(
            "/api/v1/sessions/s/chats/c/steer", json={"text": "x"}
        )
        assert missing.status_code == 404

        sid, cid = await _seed_running_chat()
        endpoint = f"/api/v1/sessions/{sid}/chats/{cid}/steer"
        # No in-process runtime is required: success means a durable command.
        ok = await http.post(endpoint, json={"text": "聚焦华东"})
        assert ok.status_code == 200
        assert ok.json() == {"ok": True}
        comments = [
            {
                "path": "out/chart.png",
                "line_start": 1,
                "line_end": 1,
                "comment": "换成柱状图",
            }
        ]
        commented = await http.post(
            endpoint, json={"text": "改一下图", "artifact_comments": comments}
        )
        assert commented.status_code == 200
        async with get_session_factory()() as db:
            commands = ChatCommandRepository(db)
            assert await commands.take_steers(cid) == [
                {"text": "聚焦华东", "artifact_comments": []},
                {"text": "改一下图", "artifact_comments": comments},
            ]
            await db.commit()
        async with get_session_factory()() as db:
            assert await ChatCommandRepository(db).take_steers(cid) == []

        await _complete_chat(cid)
        done = await http.post(endpoint, json={"text": "late"})
        assert done.status_code == 409
        assert done.json()["details"]["reason"] == "CHAT_ENDED"


async def test_clarification_paths() -> None:
    async with service_client() as http:
        sid, cid = await _seed_running_chat()
        endpoint = f"/api/v1/sessions/{sid}/chats/{cid}/clarification/answer"
        missing = await http.post(
            f"/api/v1/sessions/{sid}/chats/missing/clarification/answer",
            json=ANSWER_BODY,
        )
        assert missing.status_code == 404

        # Verify admission is committed before a separate runtime session reads it.
        ok = await http.post(endpoint, json=ANSWER_BODY)
        assert ok.status_code == 200, ok.text
        assert ok.json()["chat"]["id"] == cid
        async with get_session_factory()() as db:
            row = (await db.scalars(select(ChatCommandRow))).one()
            assert (row.chat_id, row.kind, row.status) == (
                cid,
                "clarification",
                "waiting",
            )
            assert (row.tenant_id, row.workspace_id, row.user_id) == (
                TEST_IDENTITY.tenant_id,
                TEST_IDENTITY.workspace_id,
                TEST_IDENTITY.user_id,
            )
            assert (
                await ChatCommandRepository(db).take_clarification(cid, "other") is None
            )

        executor = ClarificationWithExecutor(get_session_factory(), cid)
        result = await asyncio.wait_for(executor.wait_for_answer("clar_1"), timeout=2)
        assert result.id == "clar_1"
        assert json.loads(result.output[0].text) == ANSWER_BODY["result"]
        async with get_session_factory()() as db:
            row = (await db.scalars(select(ChatCommandRow))).one()
            assert row.status == "done"
            assert (
                await ChatCommandRepository(db).take_clarification(cid, "clar_1")
                is None
            )

        # Internal admission queues repeated answers; it does not synchronously
        # reject them based on a process-local pending-tool registry.
        repeated = await http.post(endpoint, json=ANSWER_BODY)
        assert repeated.status_code == 200
        async with get_session_factory()() as db:
            rows = (await db.scalars(select(ChatCommandRow))).all()
            assert sorted(row.status for row in rows) == ["done", "waiting"]

        bad = await http.post(
            endpoint,
            json={"clarification_id": "x", "result": {"status": "nonsense"}},
        )
        assert bad.status_code == 422
        await _complete_chat(cid)
        ended = await http.post(endpoint, json=ANSWER_BODY)
        assert ended.status_code == 404
        assert ended.json()["details"]["reason"] == "CLARIFICATION_NOT_FOUND"
        async with get_session_factory()() as db:
            assert len((await db.scalars(select(ChatCommandRow))).all()) == 2

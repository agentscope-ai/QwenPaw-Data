# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def test_chat_plan_edit_snapshot_and_clear(tmp_path: Path) -> None:
    asyncio.run(reset_engine())
    reset_hub()

    async def seed() -> tuple[str, str]:
        await init_db()
        session_factory = get_session_factory()
        async with session_factory() as db:
            session = Session.create(
                identity=TEST_IDENTITY,
                agent_id="default",
                title="plan",
            )
            await SessionRepository(db, TEST_IDENTITY).add(session)
            chat = session.open_chat(
                text="seed",
                datasource_id="ds-1",
                has_active_chat=False,
            )
            chat.mark_status("completed")
            await SessionRepository(db, TEST_IDENTITY).save(session)
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
            return session.id, chat.id

    session_id, chat_id = asyncio.run(seed())
    app = create_app()
    headers: dict[str, str] = {}
    path = f"/api/sessions/{session_id}/chats/{chat_id}/plan"

    with TestClient(app) as client:
        draft = {
            "tasks": [
                {
                    "id": "2",
                    "subject": "Analyze",
                    "description": "Analyze data",
                    "blocked_by": ["1"],
                },
                {
                    "id": "1",
                    "subject": "Fetch",
                    "description": "Fetch data",
                },
            ],
        }
        response = client.post(
            f"{path}/edit",
            json={"plan": draft, "reason": "user edit"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        plan = response.json()["plan"]
        assert set(plan) == {"tasks"}
        assert plan["tasks"][0]["blocks"] == []
        assert plan["tasks"][1]["blocks"] == ["2"]

        snapshot = client.get(
            f"/api/sessions/{session_id}/snapshot",
            headers=headers,
        )
        assert snapshot.status_code == 200
        assert "plan" not in snapshot.json()
        assert snapshot.json()["chats"][0]["plan"] == plan

        other_chat_id = asyncio.run(_add_completed_chat(session_id))
        snapshot = client.get(
            f"/api/sessions/{session_id}/snapshot",
            headers=headers,
        )
        other = next(
            chat for chat in snapshot.json()["chats"] if chat["id"] == other_chat_id
        )
        assert other["plan"] is None

        legacy = {
            **draft,
            "name": "not accepted",
            "description": "not accepted",
        }
        response = client.post(
            f"{path}/edit",
            json={"plan": legacy, "reason": None},
            headers=headers,
        )
        assert response.status_code == 422

        response = client.post(
            f"{path}/edit",
            json={"plan": None, "reason": None},
            headers=headers,
        )
        assert response.status_code == 200
        assert response.json() == {"plan": None}


async def _add_completed_chat(session_id: str) -> str:
    session_factory = get_session_factory()
    async with session_factory() as db:
        session = await SessionRepository(db, TEST_IDENTITY).get(session_id)
        chat = session.open_chat(
            text="other",
            datasource_id="ds-1",
            has_active_chat=False,
        )
        chat.mark_status("completed")
        await SessionRepository(db, TEST_IDENTITY).save(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
        return chat.id

# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.db.tables import FeedbackRow
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.repositories import ChatRepository, SessionRepository
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


def test_feedback_reaction_is_single_persisted_state(tmp_path: Path) -> None:
    asyncio.run(reset_engine())
    reset_hub()

    async def seed() -> tuple[str, str]:
        await init_db()
        async with get_session_factory()() as db:
            session = Session.create(
                identity=TEST_IDENTITY,
                agent_id="default",
                title="feedback",
            )
            session_repo = SessionRepository(db, TEST_IDENTITY)
            await session_repo.add(session)
            chat = session.open_chat(
                text="seed",
                datasource_id="ds-1",
                has_active_chat=False,
            )
            chat.mark_status("completed")
            await session_repo.save(session)
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
            return session.id, chat.id

    session_id, chat_id = asyncio.run(seed())
    app = create_app()
    headers: dict[str, str] = {}
    feedback_url = f"/api/sessions/{session_id}/chats/{chat_id}/feedback"
    reaction_url = f"{feedback_url}/reaction"

    with TestClient(app) as client:
        reaction = client.get(reaction_url, headers=headers)
        assert reaction.status_code == 200, reaction.text
        assert reaction.json()["feedback"] is None

        like = client.post(feedback_url, json={"kind": "like"}, headers=headers)
        assert like.status_code == 200, like.text
        like_feedback = like.json()["feedback"]
        assert like_feedback["kind"] == "like"

        repeated_like = client.post(
            feedback_url,
            json={"kind": "like"},
            headers=headers,
        )
        assert repeated_like.status_code == 200, repeated_like.text
        assert repeated_like.json()["feedback"]["id"] == like_feedback["id"]

        dislike = client.post(
            feedback_url,
            json={
                "kind": "dislike",
                "reason": "内容不完整",
                "detail": "缺少趋势",
            },
            headers=headers,
        )
        assert dislike.status_code == 200, dislike.text
        dislike_feedback = dislike.json()["feedback"]
        assert dislike_feedback["id"] == like_feedback["id"]
        assert dislike_feedback["kind"] == "dislike"
        assert dislike_feedback["reason"] == "内容不完整"

        reaction = client.get(reaction_url, headers=headers)
        assert reaction.status_code == 200, reaction.text
        assert reaction.json()["feedback"]["kind"] == "dislike"

        cleared = client.delete(reaction_url, headers=headers)
        assert cleared.status_code == 204, cleared.text
        reaction = client.get(reaction_url, headers=headers)
        assert reaction.status_code == 200, reaction.text
        assert reaction.json()["feedback"] is None

        first_copy = client.post(
            feedback_url,
            json={"kind": "copy"},
            headers=headers,
        )
        second_copy = client.post(
            feedback_url,
            json={"kind": "copy"},
            headers=headers,
        )
        assert first_copy.status_code == 200, first_copy.text
        assert second_copy.status_code == 200, second_copy.text
        assert (
            first_copy.json()["feedback"]["id"] != second_copy.json()["feedback"]["id"]
        )

        async def count_feedback() -> tuple[int, int]:
            async with get_session_factory()() as db:
                reactions = await db.scalar(
                    select(func.count())
                    .select_from(FeedbackRow)
                    .where(
                        FeedbackRow.chat_id == chat_id,
                        FeedbackRow.kind.in_(("like", "dislike")),
                    )
                )
                copies = await db.scalar(
                    select(func.count())
                    .select_from(FeedbackRow)
                    .where(
                        FeedbackRow.chat_id == chat_id,
                        FeedbackRow.kind == "copy",
                    )
                )
                return int(reactions or 0), int(copies or 0)

        assert asyncio.run(count_feedback()) == (0, 2)

# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.api.deps import get_identity
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ArtifactRepository,
    ChatRepository,
    SegmentRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.stream.hub import reset_hub

OWNER = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)
STRANGER = Identity(
    user_id="stranger",
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


class _IdentitySwitch:
    def __init__(self, identity: Identity) -> None:
        self.current = identity

    def __call__(self) -> Identity:
        return self.current


async def _seed_two_chats() -> tuple[str, str, str]:
    await reset_engine()
    reset_hub()
    await init_db()
    async with get_session_factory()() as db:
        session = Session.create(identity=OWNER, title="artifacts")
        session_repo = SessionRepository(db, OWNER)
        await session_repo.add(session)
        chat_repo = ChatRepository(db, OWNER)
        chats = []
        for text in ("第一轮", "第二轮"):
            chat = session.open_chat(
                text=text, datasource_id="ds_demo", has_active_chat=False
            )
            chat.mark_status("completed")
            await session_repo.save(session)
            await chat_repo.add(chat)
            chats.append(chat)
        repo = ArtifactRepository(db, OWNER)
        await repo.register(
            session_id=session.id,
            chat_id=chats[0].id,
            name="dau.csv",
            path="analysis/dau.csv",
        )
        await repo.register(
            session_id=session.id,
            chat_id=chats[0].id,
            name="draft.md",
            path="draft.md",
        )
        await repo.register(
            session_id=session.id,
            chat_id=chats[1].id,
            name="dau-v2.csv",
            path="analysis/dau.csv",
        )
        segments = SegmentRepository(db, OWNER)
        await segments.upsert(
            chat_id=chats[0].id,
            segment_id="seg_1",
            title="定位变化点",
            coverage={"start_seq": 1, "end_seq": 1},
            artifact=[
                {
                    "name": "dau.csv",
                    "description": "变化点结果",
                    "relative_path": "analysis/dau.csv",
                }
            ],
        )
        await segments.upsert(
            chat_id=chats[1].id,
            segment_id="seg_2",
            title="复核环比",
            coverage={"start_seq": 1, "end_seq": 2},
            artifact=[
                {
                    "name": "dau.csv",
                    "description": "复核后的环比",
                    "relative_path": "analysis/dau.csv",
                },
                {
                    "name": "draft.md",
                    "description": "分析草稿",
                    "relative_path": "draft.md",
                },
            ],
        )
        await db.commit()
        return session.id, chats[0].id, chats[1].id


async def test_list_for_session_keeps_latest_path() -> None:
    session_id, first_chat_id, second_chat_id = await _seed_two_chats()
    async with get_session_factory()() as db:
        rows = await ArtifactRepository(db, OWNER).list_for_session(session_id)
    assert [(row["path"], row["name"], row["chat_id"]) for row in rows] == [
        ("analysis/dau.csv", "dau-v2.csv", second_chat_id),
        ("draft.md", "draft.md", first_chat_id),
    ]


async def test_list_segment_artifacts_for_session_merges_paths() -> None:
    session_id, first_chat_id, second_chat_id = await _seed_two_chats()
    async with get_session_factory()() as db:
        rows = await SegmentRepository(db, OWNER).list_artifacts_for_session(session_id)
    assert [row["relative_path"] for row in rows] == [
        "analysis/dau.csv",
        "draft.md",
    ]
    assert rows[0]["name"] == "dau.csv"
    assert rows[0]["description"] == "变化点结果"
    assert rows[0]["chat_id"] == first_chat_id
    assert rows[0]["segments"] == [
        {"segment_id": "seg_1", "chat_id": first_chat_id, "title": "定位变化点"},
        {"segment_id": "seg_2", "chat_id": second_chat_id, "title": "复核环比"},
    ]
    assert rows[1]["description"] == "分析草稿"
    assert rows[1]["chat_id"] == second_chat_id
    assert rows[1]["segments"] == [
        {"segment_id": "seg_2", "chat_id": second_chat_id, "title": "复核环比"},
    ]


async def test_list_for_session_requires_session_id() -> None:
    await reset_engine()
    await init_db()
    async with get_session_factory()() as db:
        with pytest.raises(ValueError, match="session_id is required"):
            await ArtifactRepository(db, OWNER).list_for_session("")
        with pytest.raises(ValueError, match="session_id is required"):
            await SegmentRepository(db, OWNER).list_artifacts_for_session("")


def test_session_artifacts_http_empty_session() -> None:
    import asyncio

    async def seed() -> str:
        await reset_engine()
        reset_hub()
        await init_db()
        async with get_session_factory()() as db:
            session = Session.create(identity=OWNER, title="empty")
            await SessionRepository(db, OWNER).add(session)
            await db.commit()
            return session.id

    session_id = asyncio.run(seed())
    app = create_app()
    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session_id}/artifacts")
        assert response.status_code == 200, response.text
        assert response.json() == {"artifacts": [], "segment_artifacts": []}


def test_session_artifacts_http_collapses_rewrites() -> None:
    import asyncio

    session_id, first_chat_id, second_chat_id = asyncio.run(_seed_two_chats())
    app = create_app()
    with TestClient(app) as client:
        empty = client.get("/api/sessions/ses_missing/artifacts")
        assert empty.status_code == 404

        response = client.get(f"/api/sessions/{session_id}/artifacts")
        assert response.status_code == 200, response.text
        body = response.json()
        artifacts = body["artifacts"]
        assert [item["path"] for item in artifacts] == [
            "analysis/dau.csv",
            "draft.md",
        ]
        assert artifacts[0]["chat_id"] == second_chat_id
        assert artifacts[0]["name"] == "dau-v2.csv"
        assert artifacts[1]["chat_id"] == first_chat_id
        segment_artifacts = body["segment_artifacts"]
        assert [item["relative_path"] for item in segment_artifacts] == [
            "analysis/dau.csv",
            "draft.md",
        ]
        assert segment_artifacts[0]["description"] == "变化点结果"
        assert [ref["segment_id"] for ref in segment_artifacts[0]["segments"]] == [
            "seg_1",
            "seg_2",
        ]


def test_session_artifacts_http_hides_unreadable_session() -> None:
    import asyncio

    session_id, _, _ = asyncio.run(_seed_two_chats())
    app = create_app()
    switch = _IdentitySwitch(STRANGER)
    app.dependency_overrides[get_identity] = switch
    with TestClient(app) as client:
        response = client.get(f"/api/sessions/{session_id}/artifacts")
        assert response.status_code == 404

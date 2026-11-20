"""Trace views and feedback on the current authenticated service and SQL repositories."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
import httpx
import pytest
from sqlalchemy import select
from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.db.tables import FeedbackRow
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ChatRepository,
    SessionRepository,
    FeedbackRepository,
)
from qwenpaw_data.host.core.stream.output_stream import OutputStream
from .conftest import TEST_IDENTITY
from . import test_runtime_settings

database = test_runtime_settings.database


async def _seed_chat(app=None):
    async with get_session_factory()() as db:
        session = Session.create(identity=TEST_IDENTITY)
        await SessionRepository(db, TEST_IDENTITY).add(session)
        chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
        await SessionRepository(db, TEST_IDENTITY).save(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
    return session.id, chat.id


async def test_reaction_set_replace_get_clear(database):
    sid, cid = await _seed_chat()
    async with database() as db:
        chat = await ChatRepository(db, TEST_IDENTITY).get(cid)
        repo = FeedbackRepository(db, TEST_IDENTITY)
        assert await repo.get_reaction(chat) is None
        liked = await repo.set_reaction(chat, kind="like", reason=None, detail=None)
        assert liked["kind"] == "like" and liked["chat_id"] == cid
        switched = await repo.set_reaction(
            chat, kind="dislike", reason="错误结论", detail="口径没对齐"
        )
        assert switched["id"] == liked["id"]
        await db.commit()
    async with database() as db:
        chat = await ChatRepository(db, TEST_IDENTITY).get(cid)
        repo = FeedbackRepository(db, TEST_IDENTITY)
        current = await repo.get_reaction(chat)
        assert current["kind"] == "dislike" and current["reason"] == "错误结论"
        for field in ("user_id", "tenant_id", "workspace_id"):
            other = FeedbackRepository(db, replace(TEST_IDENTITY, **{field: "other"}))
            assert await other.get_reaction(chat) is None
            await other.clear_reaction(chat)
        assert (await repo.get_reaction(chat))["id"] == liked["id"]
        await repo.clear_reaction(chat)
        await db.commit()
    async with database() as db:
        assert await FeedbackRepository(db, TEST_IDENTITY).get_reaction(chat) is None


async def test_reaction_rejects_other_kinds(database):
    _, cid = await _seed_chat()
    async with database() as db:
        chat = await ChatRepository(db, TEST_IDENTITY).get(cid)
        repo = FeedbackRepository(db, TEST_IDENTITY)
        with pytest.raises(ValueError, match="like or dislike"):
            await repo.set_reaction(chat, kind="copy", reason=None, detail=None)
        assert await repo.get_reaction(chat) is None


async def test_history_survives_reaction_replacement_and_clear(database):
    _, cid = await _seed_chat()
    async with database() as db:
        chat = await ChatRepository(db, TEST_IDENTITY).get(cid)
        repo = FeedbackRepository(db, TEST_IDENTITY)
        copied = await repo.add(
            chat, kind="copy", reason=None, detail=None, artifact_ref=None
        )
        commented = await repo.add(
            chat,
            kind="artifact_comment",
            reason=None,
            detail="换个配色",
            artifact_ref={
                "artifact_id": "art_1",
                "content_hash": "h",
                "line_start": 1,
                "line_end": 2,
                "quote": "q",
            },
        )
        assert commented["artifact_ref"]["artifact_id"] == "art_1"
        assert await repo.get_reaction(chat) is None
        await repo.set_reaction(chat, kind="like", reason=None, detail=None)
        await repo.set_reaction(chat, kind="dislike", reason="wrong", detail=None)
        await repo.clear_reaction(chat)
        await db.commit()
    async with database() as db:
        rows = (
            await db.scalars(select(FeedbackRow).where(FeedbackRow.chat_id == cid))
        ).all()
        assert {r.id for r in rows} == {copied["id"], commented["id"]}
        assert {r.kind for r in rows} == {"copy", "artifact_comment"}


@asynccontextmanager
async def service_client(tmp_path, monkeypatch):
    app = create_app()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1234)),
            base_url="http://localhost",
            trust_env=False,
        ) as http:
            yield http, app


def _message_payload(msg_id: str, text: str) -> dict:
    return {
        "object": "message",
        "id": msg_id,
        "sequence": 0,
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"object": "content", "type": "text", "index": 0, "text": text}],
    }


def _biz_event_payload(event_id: str, channel: str) -> dict:
    return {
        "object": "biz_event",
        "biz_event": {
            "event_id": event_id,
            "seq": 1,
            "channel": channel,
            "status": "done",
            "started_at": 1.0,
            "presentation": {
                "card_type": "text",
                "caption": "取数",
                "body": "拉取订单明细",
            },
        },
    }


async def test_trace_views(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat(app)
        events = OutputStream(
            get_session_factory(),
            session_id=sid,
            chat_id=cid,
            identity=TEST_IDENTITY,
            last_sequence_number=-1,
        )
        await events.append(_message_payload("msg_1", "你好"))
        await events.biz_event(**_biz_event_payload("be_1", "main")["biz_event"])
        await events.biz_event(**_biz_event_payload("be_2", "subagent")["biz_event"])

        raw = await http.get(f"/api/sessions/{sid}/chats/{cid}/trace")
        assert raw.status_code == 200, raw.text
        body = raw.json()
        assert [m["id"] for m in body["messages"]] == ["msg_1"]
        assert "sequence_number" not in body["messages"][0]
        assert body["biz_events"] == []

        business = await http.get(
            f"/api/sessions/{sid}/chats/{cid}/trace", params={"view": "business"}
        )
        events_body = business.json()
        assert events_body["messages"] == []
        # Subagent-channel events are filtered out.
        assert [e["event_id"] for e in events_body["biz_events"]] == ["be_1"]

        both = await http.get(
            f"/api/sessions/{sid}/chats/{cid}/trace", params={"view": "both"}
        )
        assert both.json()["messages"] and both.json()["biz_events"]

        invalid = await http.get(
            f"/api/sessions/{sid}/chats/{cid}/trace", params={"view": "nope"}
        )
        assert invalid.status_code == 400

        missing = await http.get("/api/sessions/s/chats/c/trace")
        assert missing.status_code == 404


async def test_feedback_routes(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat(app)
        base = f"/api/sessions/{sid}/chats/{cid}/feedback"

        # No reaction yet.
        empty = await http.get(f"{base}/reaction")
        assert empty.status_code == 200
        assert empty.json()["feedback"] is None

        liked = await http.post(base, json={"kind": "like"})
        assert liked.status_code == 200, liked.text
        assert liked.json()["feedback"]["kind"] == "like"

        # Switching replaces instead of stacking.
        switched = await http.post(base, json={"kind": "dislike", "reason": "结论不对"})
        assert switched.json()["feedback"]["kind"] == "dislike"
        reaction = await http.get(f"{base}/reaction")
        assert reaction.json()["feedback"]["kind"] == "dislike"

        # Copy feedback is appended and does not disturb the reaction.
        copied = await http.post(base, json={"kind": "copy"})
        assert copied.status_code == 200
        assert (await http.get(f"{base}/reaction")).json()["feedback"][
            "kind"
        ] == "dislike"

        commented = await http.post(
            base,
            json={
                "kind": "artifact_comment",
                "detail": "调整图例",
                "artifact_ref": {
                    "artifact_id": "art_1",
                    "content_hash": "h",
                    "line_start": 1,
                    "line_end": 1,
                    "quote": "q",
                },
            },
        )
        assert commented.status_code == 200
        assert commented.json()["feedback"]["artifact_ref"]["artifact_id"] == "art_1"

        deleted = await http.delete(f"{base}/reaction")
        assert deleted.status_code == 204
        assert (await http.get(f"{base}/reaction")).json()["feedback"] is None

        missing = await http.post(
            "/api/sessions/s/chats/c/feedback", json={"kind": "like"}
        )
        assert missing.status_code == 404


async def test_trace_and_feedback_cannot_cross_session_boundary(tmp_path, monkeypatch):
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat()
        other_sid, _ = await _seed_chat()
        base = f"/api/sessions/{other_sid}/chats/{cid}"
        assert (await http.get(base + "/trace")).status_code == 404
        assert (await http.get(base + "/feedback/reaction")).status_code == 404
        assert (
            await http.post(base + "/feedback", json={"kind": "like"})
        ).status_code == 404
        assert (await http.delete(base + "/feedback/reaction")).status_code == 404
        own = await http.get(f"/api/sessions/{sid}/chats/{cid}/feedback/reaction")
        assert own.status_code == 200 and own.json()["feedback"] is None

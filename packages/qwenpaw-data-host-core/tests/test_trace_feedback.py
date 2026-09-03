# -*- coding: utf-8 -*-
"""Trace view assembly and feedback store/routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from qwenpaw_data.host.core.store.json_store import JSONFeedbackStore

sqlalchemy = pytest.importorskip("sqlalchemy")

from qwenpaw_data.host.core.db.engine import (  # noqa: E402
    create_engine_and_factory,
    init_db,
)
from qwenpaw_data.host.core.store.sql_store import SQLFeedbackStore  # noqa: E402


# ---------------------------------------------------------------------------
# FeedbackStore conformance


@pytest.fixture(params=["json", "sql"])
async def feedback_store(request, tmp_path: Path):
    if request.param == "json":
        yield JSONFeedbackStore(tmp_path)
        return
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{tmp_path / 'host.db'}",
    )
    await init_db(engine)
    yield SQLFeedbackStore(factory)
    await engine.dispose()


IDS = {"user_id": "local", "session_id": "ses1", "chat_id": "chat1"}


async def test_reaction_set_replace_get_clear(feedback_store) -> None:
    assert await feedback_store.get_reaction("local", "ses1", "chat1") is None

    liked = await feedback_store.set_reaction(**IDS, kind="like")
    assert liked["kind"] == "like"
    assert liked["chat_id"] == "chat1"

    switched = await feedback_store.set_reaction(
        **IDS, kind="dislike", reason="错误结论", detail="口径没对齐"
    )
    assert switched["kind"] == "dislike"

    current = await feedback_store.get_reaction("local", "ses1", "chat1")
    assert current is not None
    assert current["kind"] == "dislike"
    assert current["reason"] == "错误结论"

    # Reactions are per user.
    assert await feedback_store.get_reaction("other", "ses1", "chat1") is None

    await feedback_store.clear_reaction("local", "ses1", "chat1")
    assert await feedback_store.get_reaction("local", "ses1", "chat1") is None


async def test_reaction_rejects_other_kinds(feedback_store) -> None:
    with pytest.raises(ValueError, match="like or dislike"):
        await feedback_store.set_reaction(**IDS, kind="copy")


async def test_add_keeps_history_and_reaction_apart(feedback_store) -> None:
    copied = await feedback_store.add(**IDS, kind="copy")
    assert copied["kind"] == "copy"
    commented = await feedback_store.add(
        **IDS,
        kind="artifact_comment",
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
    # Appended feedback never surfaces as the reaction.
    assert await feedback_store.get_reaction("local", "ses1", "chat1") is None
    await feedback_store.clear_reaction("local", "ses1", "chat1")


# ---------------------------------------------------------------------------
# Routes

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from qwenpaw_data.host.core.domain.identity import Identity  # noqa: E402
from qwenpaw_data.host.core.domain.session import Session  # noqa: E402


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, app


async def _seed_chat(app) -> tuple[str, str]:
    state = app.state.service
    session = Session.create(identity=Identity.anonymous())
    await state.sessions.add(session)
    chat = session.open_chat(text="hi", datasource_id=None, has_active_chat=False)
    await state.sessions.save(session)
    await state.chats.add(chat)
    return session.id, chat.id


def _message_payload(msg_id: str, text: str) -> dict:
    return {
        "object": "message",
        "id": msg_id,
        "sequence": 0,
        "type": "text",
        "role": "assistant",
        "status": "completed",
        "content": [
            {"object": "content", "type": "text", "index": 0, "text": text}
        ],
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
        events = app.state.service.events
        await events.append(
            session_id=sid, chat_id=cid, payload=_message_payload("msg_1", "你好")
        )
        await events.append(
            session_id=sid, chat_id=cid, payload=_biz_event_payload("be_1", "main")
        )
        await events.append(
            session_id=sid,
            chat_id=cid,
            payload=_biz_event_payload("be_2", "subagent"),
        )

        raw = await http.get(f"/api/v1/sessions/{sid}/chats/{cid}/trace")
        assert raw.status_code == 200, raw.text
        body = raw.json()
        assert [m["id"] for m in body["messages"]] == ["msg_1"]
        assert "sequence_number" not in body["messages"][0]
        assert body["biz_events"] == []

        business = await http.get(
            f"/api/v1/sessions/{sid}/chats/{cid}/trace", params={"view": "business"}
        )
        events_body = business.json()
        assert events_body["messages"] == []
        # Subagent-channel events are filtered out.
        assert [e["event_id"] for e in events_body["biz_events"]] == ["be_1"]

        both = await http.get(
            f"/api/v1/sessions/{sid}/chats/{cid}/trace", params={"view": "both"}
        )
        assert both.json()["messages"] and both.json()["biz_events"]

        invalid = await http.get(
            f"/api/v1/sessions/{sid}/chats/{cid}/trace", params={"view": "nope"}
        )
        assert invalid.status_code == 400

        missing = await http.get("/api/v1/sessions/s/chats/c/trace")
        assert missing.status_code == 404


async def test_feedback_routes(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid = await _seed_chat(app)
        base = f"/api/v1/sessions/{sid}/chats/{cid}/feedback"

        # No reaction yet.
        empty = await http.get(f"{base}/reaction")
        assert empty.status_code == 200
        assert empty.json()["feedback"] is None

        liked = await http.post(base, json={"kind": "like"})
        assert liked.status_code == 200, liked.text
        assert liked.json()["feedback"]["kind"] == "like"

        # Switching replaces instead of stacking.
        switched = await http.post(
            base, json={"kind": "dislike", "reason": "结论不对"}
        )
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
        assert (
            commented.json()["feedback"]["artifact_ref"]["artifact_id"] == "art_1"
        )

        deleted = await http.delete(f"{base}/reaction")
        assert deleted.status_code == 204
        assert (await http.get(f"{base}/reaction")).json()["feedback"] is None

        missing = await http.post(
            "/api/v1/sessions/s/chats/c/feedback", json={"kind": "like"}
        )
        assert missing.status_code == 404

# -*- coding: utf-8 -*-
"""Session fork, snapshot bundles, and file access/share links."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402
from agentscope.state import AgentState
from qwenpaw_data.host.core.db.engine import get_session_factory, init_db, reset_engine
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.agent_state import AgentStateStore
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.stream.output_stream import OutputStream

from .conftest import TEST_IDENTITY, _seed_test_preferences


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch, **env: str):
    # These tests seed chat state explicitly. A real background run could
    # overwrite it after seeding (and would require Docker/model credentials).
    monkeypatch.setattr(
        "qwenpaw_data.host.core.api.routers.console.ChatRuntime.run", AsyncMock()
    )
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_SHARE_SECRET", raising=False)
    for key in list(os.environ):
        if key.startswith("QWENPAW_DATA_OSS_"):
            monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    clear_settings()
    await reset_engine()
    app = create_app()
    async with app.router.lifespan_context(app):
        await _seed_test_preferences(get_session_factory())
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://localhost",
            trust_env=False,
        ) as http:
            yield http, app
    await reset_engine()


def _paths(session: dict) -> UserAgentPaths:
    return UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=TEST_IDENTITY,
        agent_id=session["agent_id"],
        session_id=session["id"],
    )


async def _seed_forkable_session(http, app) -> tuple[str, str, str]:
    """Session with an uploaded attachment and one completed chat."""
    created = await http.post("/api/v1/sessions", json={"title": "月报"})
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]
    uploaded = await http.post(
        "/api/v1/console/upload",
        data={"session_id": session_id},
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment_id = uploaded.json()["attachment"]["attachment_id"]
    chat = await http.post(
        "/api/v1/console/chat",
        json={
            "session_id": session_id,
            "text": "分析这个文件",
            "attachment_ids": [attachment_id],
        },
    )
    assert chat.status_code == 200, chat.text
    chat_id = chat.json()["chat"]["id"]

    factory = get_session_factory()
    async with factory() as db:
        record = await ChatRepository(db, TEST_IDENTITY).get(chat_id)
    events = OutputStream(
        factory,
        session_id=session_id,
        chat_id=chat_id,
        identity=TEST_IDENTITY,
        last_sequence_number=record.last_sequence_number,
    )
    await events.biz_event(
        event_id="be_1", seq=1, channel="main", status="done", started_at=1.0
    )
    await events.followup_generated(questions=["按区域拆一下？"])
    await events.artifact_registered(
        id="art_report", name="report.md", path="report.md"
    )
    await events.flush()
    # Agent state belongs in SQL; files belong in the identity-scoped workspace.
    async with factory() as db:
        chats = ChatRepository(db, TEST_IDENTITY)
        record = await chats.get(chat_id)
        record.mark_status("completed")
        record.agent_state = AgentState(
            session_id=session_id, summary=f"attachment {attachment_id} from {chat_id}"
        ).model_dump(mode="json")
        await chats.save(record)
        await db.commit()
    artifact_dir = _paths(created.json()["session"]).artifact_dir
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "report.md").write_text("# 结论", encoding="utf-8")
    return session_id, chat_id, attachment_id


async def test_fork_copies_session_state(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid, att_id = await _seed_forkable_session(http, app)

        forked = await http.post(f"/api/v1/sessions/{sid}/fork", json={"chat_id": cid})
        assert forked.status_code == 200, forked.text
        target = forked.json()["session"]
        new_sid = target["id"]
        assert new_sid != sid
        assert target["title"] == "月报 (fork)"
        assert target["parent_session_id"] == sid
        assert target["forked_from_chat_id"] == cid
        assert target["chat_count"] == 1

        # Chats copied with remapped ids and rewritten attachment refs.
        chats = (await http.get(f"/api/v1/sessions/{new_sid}/snapshot")).json()
        assert len(chats["chats"]) == 1
        copied = chats["chats"][0]
        assert copied["id"] != cid
        assert copied["session_id"] == new_sid
        new_att_id = copied["attachments"][0]["attachment_id"]
        assert new_att_id != att_id

        # Events copied and rewritten (runtime response frames ride along).
        async with get_session_factory()() as db:
            events = await ChatEventRepository(db, TEST_IDENTITY).read_after(
                copied["id"], -1
            )
            agent_state = await AgentStateStore(db, TEST_IDENTITY).load(new_sid)
            copied_chat = await ChatRepository(db, TEST_IDENTITY).get(copied["id"])
        objects = [e.object for e in events]
        assert "biz_event" in objects and "followup.generated" in objects
        followup = next(e for e in events if e.object == "followup.generated")
        assert followup.followup.chat_id == copied["id"]
        assert all(e.chat_id == copied["id"] for e in events)

        assert agent_state.session_id == new_sid
        assert agent_state.summary == f"attachment {new_att_id} from {copied['id']}"
        assert copied_chat.agent_state == agent_state.model_dump(mode="json")
        assert [e["event_id"] for e in copied["biz_events"]] == ["be_1"]
        assert copied["followup"]["questions"] == ["按区域拆一下？"]

        # Files copied alongside SQL references; the source remains unchanged.
        paths = _paths(target)
        assert (paths.workspace / "uploads" / new_sid / "data.csv").is_file()
        assert (paths.artifact_dir / "report.md").read_text(
            encoding="utf-8"
        ) == "# 结论"
        original = (await http.get(f"/api/v1/sessions/{sid}/snapshot")).json()["chats"][
            0
        ]
        assert original["id"] == cid
        assert original["attachments"][0]["attachment_id"] == att_id

        # The copied attachment is usable in the forked session.
        chat2 = await http.post(
            "/api/v1/console/chat",
            json={
                "session_id": new_sid,
                "text": "继续",
                "attachment_ids": [new_att_id],
            },
        )
        assert chat2.status_code == 200, chat2.text


async def test_fork_guards(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid, _ = await _seed_forkable_session(http, app)

        missing = await http.post(
            f"/api/v1/sessions/{sid}/fork", json={"chat_id": "chat_missing"}
        )
        assert missing.status_code == 404

        # A non-completed chat cannot be the fork point.
        async with get_session_factory()() as db:
            chats = ChatRepository(db, TEST_IDENTITY)
            record = await chats.get(cid)
            record.mark_status("failed")
            await chats.save(record)
            await db.commit()
        conflict = await http.post(
            f"/api/v1/sessions/{sid}/fork", json={"chat_id": cid}
        )
        assert conflict.status_code == 409


async def test_snapshot_bundles(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid, _ = await _seed_forkable_session(http, app)

        snapshot = await http.get(f"/api/v1/sessions/{sid}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        body = snapshot.json()
        assert body["session"]["id"] == sid
        assert len(body["chats"]) == 1
        bundle = body["chats"][0]
        assert bundle["id"] == cid
        assert [e["event_id"] for e in bundle["biz_events"]] == ["be_1"]
        assert bundle["followup"]["questions"] == ["按区域拆一下？"]
        assert bundle["attachments"][0]["filename"] == "data.csv"

        missing = await http.get("/api/v1/sessions/ses_missing/snapshot")
        assert missing.status_code == 404


async def test_files_access(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, _, _ = await _seed_forkable_session(http, app)

        preview = await http.get(
            f"/api/v1/sessions/{sid}/files/access",
            params={"path": "report.md"},
        )
        assert preview.status_code == 200
        assert "inline" in preview.headers["content-disposition"]

        download = await http.get(
            f"/api/v1/sessions/{sid}/files/access",
            params={"path": "report.md", "purpose": "download"},
        )
        assert "attachment" in download.headers["content-disposition"]

        for bad, status in (
            ("../secret", 400),
            ("/etc/hosts", 400),
            ("a/../../b", 400),
            ("./x", 404),
            ("", 422),
        ):
            response = await http.get(
                f"/api/v1/sessions/{sid}/files/access", params={"path": bad}
            )
            assert response.status_code == status, bad


async def test_share_link_roundtrip(tmp_path, monkeypatch) -> None:
    async with service_client(
        tmp_path, monkeypatch, QWENPAW_DATA_SHARE_SECRET="test-secret"
    ) as (http, app):
        sid, _, _ = await _seed_forkable_session(http, app)

        shared = await http.post(
            f"/api/v1/sessions/{sid}/files/share", json={"path": "report.md"}
        )
        assert shared.status_code == 200, shared.text
        body = shared.json()
        assert body["name"] == "report.md"
        url = body["url"]
        assert url.startswith("/api/v1/files/shared/")

        resolved = await http.get(url)
        assert resolved.status_code == 200
        assert resolved.text == "# 结论"

        # Tampered signature and truncated token both fail closed.
        tampered = await http.get(url[:-1] + ("0" if url[-1] != "0" else "1"))
        assert tampered.status_code == 404
        broken = await http.get("/api/v1/files/shared/not-a-token", params={"sig": "x"})
        assert broken.status_code == 404


async def test_share_requires_secret(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, _, _ = await _seed_forkable_session(http, app)
        response = await http.post(
            f"/api/v1/sessions/{sid}/files/share", json={"path": "report.md"}
        )
        assert response.status_code in (400, 422)


async def test_shared_route_bypasses_bearer_auth(tmp_path, monkeypatch) -> None:
    async with service_client(
        tmp_path,
        monkeypatch,
        QWENPAW_DATA_API_TOKEN="api-token",
    ) as (http, app):
        headers = {"Authorization": "Bearer api-token"}
        created = await http.post(
            "/api/v1/sessions", json={"title": "s"}, headers=headers
        )
        sid = created.json()["session"]["id"]
        artifact_dir = _paths(created.json()["session"]).artifact_dir
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "out.txt").write_text("shared", encoding="utf-8")

        shared = await http.post(
            f"/api/v1/sessions/{sid}/files/share",
            json={"path": "out.txt"},
            headers=headers,
        )
        assert shared.status_code == 200, shared.text
        # No Authorization header on the resolver: the signature is enough.
        resolved = await http.get(shared.json()["url"])
        assert resolved.status_code == 200
        assert resolved.text == "shared"


async def test_init_db_adds_missing_columns() -> None:
    """Old databases gain newly introduced columns without data loss."""
    from sqlalchemy import text

    await reset_engine()
    engine = await init_db()
    factory = get_session_factory()
    session = Session.create(identity=TEST_IDENTITY, title="Existing data")
    chat = session.open_chat(
        text="preserve me", datasource_id=None, has_active_chat=False
    )
    async with factory() as db:
        await SessionRepository(db, TEST_IDENTITY).add(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        await db.commit()
    # Simulate an old install: drop a column that a later release added.
    async with engine.begin() as conn:
        await conn.execute(
            text('ALTER TABLE "chats" DROP COLUMN "artifact_comments_json"')
        )
        await conn.execute(text('ALTER TABLE "chats" DROP COLUMN "attachments_json"'))
    await init_db()
    await init_db()  # Idempotent, including when existing rows need defaults.
    async with factory() as db:
        loaded = await ChatRepository(db, TEST_IDENTITY).get(chat.id)
        assert loaded.attachments == []
        assert loaded.artifact_comments == []
        assert loaded.user_input == chat.user_input
        assert (
            await SessionRepository(db, TEST_IDENTITY).get(session.id)
        ).title == "Existing data"
    await reset_engine()

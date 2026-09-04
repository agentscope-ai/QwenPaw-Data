# -*- coding: utf-8 -*-
"""Session fork, snapshot bundles, and file access/share links."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
import httpx  # noqa: E402

from qwenpaw_data.host.core.api.app import create_app  # noqa: E402


@asynccontextmanager
async def service_client(tmp_path: Path, monkeypatch, **env: str):
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_DB_URL", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_STORE", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_SHARE_SECRET", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = create_app(home=tmp_path, model=object())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as http:
            yield http, app


def _biz_event_payload(event_id: str) -> dict:
    return {
        "object": "biz_event",
        "biz_event": {
            "event_id": event_id,
            "seq": 1,
            "channel": "main",
            "status": "done",
            "started_at": 1.0,
        },
    }


async def _seed_forkable_session(http, app) -> tuple[str, str, str]:
    """Session with an uploaded attachment and one completed chat."""
    created = await http.post("/api/v1/sessions", json={"title": "月报"})
    session_id = created.json()["session"]["id"]
    uploaded = await http.post(
        "/api/v1/console/upload",
        data={"session_id": session_id},
        files={"file": ("data.csv", b"a,b\n1,2\n", "text/csv")},
    )
    attachment_id = uploaded.json()["attachment"]["attachment_id"]
    chat = await http.post(
        "/api/v1/console/chat",
        json={
            "session_id": session_id,
            "text": "分析这个文件",
            "attachment_ids": [attachment_id],
        },
    )
    chat_id = chat.json()["chat"]["id"]

    state = app.state.service
    events = state.events
    await events.append(
        session_id=session_id,
        chat_id=chat_id,
        payload=_biz_event_payload("be_1"),
    )
    await events.append(
        session_id=session_id,
        chat_id=chat_id,
        payload={
            "object": "followup.generated",
            "followup": {"chat_id": chat_id, "questions": ["按区域拆一下？"]},
        },
    )
    # Force the background run (which fails without a real model) terminal.
    record = await state.chats.get(chat_id)
    if record.status not in ("completed",):
        record.mark_status("completed")
        await state.chats.save(record)
    # Seed an artifact and agent state on disk.
    artifact_dir = Path(state.hosts.get(session_id=session_id).paths.artifact_dir)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "report.md").write_text("# 结论", encoding="utf-8")
    console_root = state.hosts.get(session_id=session_id).paths.console_root
    console_root.mkdir(parents=True, exist_ok=True)
    (console_root / f"local_{session_id}.json").write_text(
        f'{{"session_id": "{session_id}"}}', encoding="utf-8"
    )
    return session_id, chat_id, attachment_id


async def test_fork_copies_session_state(tmp_path, monkeypatch) -> None:
    async with service_client(tmp_path, monkeypatch) as (http, app):
        sid, cid, att_id = await _seed_forkable_session(http, app)

        forked = await http.post(
            f"/api/v1/sessions/{sid}/fork", json={"chat_id": cid}
        )
        assert forked.status_code == 200, forked.text
        target = forked.json()["session"]
        new_sid = target["id"]
        assert new_sid != sid
        assert target["title"] == "月报 (fork)"
        assert target["parent_session_id"] == sid
        assert target["forked_from_chat_id"] == cid
        assert target["chat_count"] == 1

        # Chats copied with remapped ids and rewritten attachment refs.
        chats = (await http.get(f"/api/v1/sessions/{new_sid}/chats")).json()
        assert len(chats["items"]) == 1
        copied = chats["items"][0]
        assert copied["id"] != cid
        assert copied["session_id"] == new_sid
        new_att_id = copied["attachments"][0]["attachment_id"]
        assert new_att_id != att_id

        # Events copied and rewritten (runtime response frames ride along).
        state = app.state.service
        events = await state.events.read_after(copied["id"], -1)
        objects = [e.object for e in events]
        assert "biz_event" in objects and "followup.generated" in objects
        followup = next(e for e in events if e.object == "followup.generated")
        assert followup.followup.chat_id == copied["id"]
        assert all(e.chat_id == copied["id"] for e in events)

        # Files copied: uploads, artifacts, console agent state.
        paths = state.hosts.get(session_id=new_sid).paths
        assert (paths.workspace / "uploads" / new_sid / "data.csv").is_file()
        assert (paths.artifact_dir / "report.md").is_file()
        state_file = paths.console_root / f"local_{new_sid}.json"
        assert state_file.is_file()
        assert new_sid in state_file.read_text(encoding="utf-8")

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
        record = await app.state.service.chats.get(cid)
        record.mark_status("failed")
        await app.state.service.chats.save(record)
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

        for bad in ("../secret", "/etc/hosts", "a/../../b", "./x", ""):
            response = await http.get(
                f"/api/v1/sessions/{sid}/files/access", params={"path": bad}
            )
            assert response.status_code in (404, 422), bad


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
        broken = await http.get(
            "/api/v1/files/shared/not-a-token", params={"sig": "x"}
        )
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
        state = app.state.service
        artifact_dir = Path(state.hosts.get(session_id=sid).paths.artifact_dir)
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


async def test_init_db_adds_missing_columns(tmp_path) -> None:
    """Old databases gain newly introduced columns without data loss."""
    import aiosqlite  # noqa: F401  (ensures the driver is present)
    from sqlalchemy import text

    from qwenpaw_data.host.core.db.engine import (
        create_engine_and_factory,
        init_db,
    )
    from qwenpaw_data.host.core.store.sql_store import SQLChatStore

    db_path = tmp_path / "host.db"
    engine, factory = create_engine_and_factory(
        f"sqlite+aiosqlite:///{db_path}",
    )
    await init_db(engine)
    # Simulate an old install: drop a column that a later release added.
    async with engine.begin() as conn:
        await conn.execute(
            text('ALTER TABLE "chats" DROP COLUMN "artifact_comments_json"')
        )
        await conn.execute(
            text('ALTER TABLE "chats" DROP COLUMN "attachments_json"')
        )
    await init_db(engine)

    from qwenpaw_data.host.core.domain.chat import Chat
    from qwenpaw_data.host.core.domain.identity import Identity

    store = SQLChatStore(factory)
    chat = Chat.start(
        session_id="s",
        identity=Identity.anonymous(),
        sequence=1,
        datasource_id=None,
        text="hi",
    )
    await store.add(chat)
    loaded = await store.get(chat.id)
    assert loaded.attachments == []
    await engine.dispose()

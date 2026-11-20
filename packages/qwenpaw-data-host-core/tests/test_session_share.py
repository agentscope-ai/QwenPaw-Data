# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import unittest

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
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import (
    ArtifactRepository,
    BizEventRepository,
    ChatRepository,
    FollowUpRepository,
    SegmentRepository,
    SessionRepository,
)
from qwenpaw_data.host.core.repositories.chat_event import (
    ChatEventRecord,
    ChatEventRepository,
)
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.stream.hub import reset_hub
from qwenpaw_data.host.core.utils.time import utcnow

OWNER = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)
WS_VIEWER = Identity(
    user_id="viewer-ws",
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)
TENANT_VIEWER = Identity(
    user_id="viewer-tn",
    tenant_id=TEST_TENANT_ID,
    workspace_id="other-ws",
)
ANY_VIEWER = Identity(
    user_id="viewer-any",
    tenant_id="other-tenant",
    workspace_id="other-ws",
)


class SessionShareDomainTest(unittest.TestCase):
    def test_owner_always_reads_and_writes(self) -> None:
        session = Session.create(identity=OWNER)
        self.assertTrue(session.can_read(OWNER))
        self.assertTrue(session.can_write(OWNER))
        self.assertFalse(session.can_read(WS_VIEWER))
        self.assertFalse(session.can_write(WS_VIEWER))

    def test_workspace_scope(self) -> None:
        session = Session.create(identity=OWNER)
        session.set_share_scope("workspace")
        self.assertTrue(session.can_read(WS_VIEWER))
        self.assertFalse(session.can_read(TENANT_VIEWER))
        self.assertFalse(session.can_write(WS_VIEWER))

    def test_tenant_scope(self) -> None:
        session = Session.create(identity=OWNER)
        session.set_share_scope("tenant")
        self.assertTrue(session.can_read(TENANT_VIEWER))
        self.assertFalse(session.can_read(ANY_VIEWER))

    def test_anyone_scope(self) -> None:
        session = Session.create(identity=OWNER)
        session.set_share_scope("anyone")
        self.assertTrue(session.can_read(ANY_VIEWER))
        self.assertFalse(session.can_write(ANY_VIEWER))


class _IdentitySwitch:
    def __init__(self, identity: Identity) -> None:
        self.current = identity

    def __call__(self) -> Identity:
        return self.current


def _seed() -> tuple[str, str]:
    async def run() -> tuple[str, str]:
        await reset_engine()
        reset_hub()
        await init_db()
        async with get_session_factory()() as db:
            session = Session.create(
                identity=OWNER,
                agent_id="default",
                title="shared",
            )
            sessions = SessionRepository(db, OWNER)
            await sessions.add(session)
            chat = session.open_chat(
                text="seed",
                datasource_id="ds-1",
                has_active_chat=False,
            )
            chat.mark_status("completed")
            await sessions.save(session)
            await ChatRepository(db, OWNER).add(chat)
            await SegmentRepository(db, OWNER).upsert(
                chat_id=chat.id,
                segment_id="seg_1",
                title="分析",
                coverage={"start_seq": 0, "end_seq": 0},
                conclusion="done",
            )
            await BizEventRepository(db, OWNER).upsert(
                chat_id=chat.id,
                event_id="bev_1",
                seq=0,
                presentation={"card_type": "text", "caption": "hi", "body": "body"},
            )
            await FollowUpRepository(db, OWNER).upsert(
                chat_id=chat.id,
                questions=["next?"],
            )
            await ArtifactRepository(db, OWNER).register(
                session_id=session.id,
                chat_id=chat.id,
                name="note.txt",
                path="note.txt",
            )
            await ChatEventRepository(db, OWNER).persist_batch(
                [
                    ChatEventRecord(
                        chat_id=chat.id,
                        sequence_number=0,
                        object="message",
                        session_id=session.id,
                        payload={
                            "object": "message",
                            "id": "msg_share",
                            "sequence": 0,
                            "type": "message",
                            "role": "user",
                            "content": [
                                {
                                    "object": "content",
                                    "type": "text",
                                    "text": "shared hello",
                                }
                            ],
                            "status": "completed",
                            "sequence_number": 0,
                            "session_id": session.id,
                            "chat_id": chat.id,
                        },
                        created_at=utcnow(),
                    )
                ]
            )
            paths = UserAgentPaths.from_identity(
                home=get_settings().paths.home,
                identity=session.identity,
                agent_id=session.agent_id,
                session_id=session.id,
            )
            paths.artifact_dir.mkdir(parents=True, exist_ok=True)
            (paths.artifact_dir / "note.txt").write_text(
                "shared file", encoding="utf-8"
            )
            await db.commit()
            return session.id, chat.id

    return asyncio.run(run())


def test_shared_viewer_reads_console_content() -> None:
    session_id, chat_id = _seed()
    app = create_app()
    switch = _IdentitySwitch(OWNER)
    app.dependency_overrides[get_identity] = switch
    session_url = f"/api/sessions/{session_id}"
    snapshot_url = f"{session_url}/snapshot"
    artifacts_url = f"{session_url}/artifacts"
    events_url = f"{session_url}/chats/{chat_id}/events"
    trace_url = f"{session_url}/chats/{chat_id}/trace"
    file_url = f"{session_url}/files/access"

    with TestClient(app) as client:
        switch.current = WS_VIEWER
        assert client.get(session_url).status_code == 404
        assert client.get(snapshot_url).status_code == 404
        assert client.get(artifacts_url).status_code == 404

        switch.current = OWNER
        patched = client.patch(session_url, json={"share_scope": "workspace"})
        assert patched.status_code == 200, patched.text
        assert patched.json()["session"]["share_scope"] == "workspace"
        assert patched.json()["session"]["owner_user_id"] == OWNER.user_id

        switch.current = WS_VIEWER
        session = client.get(session_url)
        assert session.status_code == 200, session.text
        assert session.json()["session"]["share_scope"] == "workspace"

        listed = client.get("/api/sessions")
        assert listed.status_code == 200, listed.text
        assert all(item["id"] != session_id for item in listed.json()["items"])

        snapshot = client.get(snapshot_url)
        assert snapshot.status_code == 200, snapshot.text
        chats = snapshot.json()["chats"]
        assert len(chats) == 1
        assert chats[0]["id"] == chat_id
        assert chats[0]["segments"][0]["segment_id"] == "seg_1"
        assert chats[0]["biz_events"][0]["event_id"] == "bev_1"
        assert chats[0]["artifacts"][0]["path"] == "note.txt"
        assert chats[0]["followup"]["questions"] == ["next?"]

        artifacts = client.get(artifacts_url)
        assert artifacts.status_code == 200, artifacts.text
        assert artifacts.json()["artifacts"][0]["path"] == "note.txt"
        assert artifacts.json()["artifacts"][0]["chat_id"] == chat_id
        assert artifacts.json()["segment_artifacts"] == []

        events = client.get(events_url)
        assert events.status_code == 200, events.text
        assert "shared hello" in events.text

        trace = client.get(f"{trace_url}?view=both")
        assert trace.status_code == 200, trace.text
        assert trace.json()["messages"][0]["id"] == "msg_share"
        assert trace.json()["biz_events"][0]["event_id"] == "bev_1"

        file_resp = client.get(file_url, params={"path": "note.txt"})
        assert file_resp.status_code == 200, file_resp.text
        assert file_resp.content == b"shared file"

        assert client.patch(session_url, json={"title": "stolen"}).status_code == 404
        assert (
            client.post(
                "/api/console/chat",
                json={"session_id": session_id, "text": "nope"},
            ).status_code
            == 404
        )
        assert client.delete(session_url).status_code == 404

        switch.current = TENANT_VIEWER
        assert client.get(session_url).status_code == 404

        switch.current = OWNER
        assert (
            client.patch(session_url, json={"share_scope": "tenant"}).status_code == 200
        )
        switch.current = TENANT_VIEWER
        assert client.get(snapshot_url).status_code == 200
        switch.current = ANY_VIEWER
        assert client.get(session_url).status_code == 404

        switch.current = OWNER
        assert (
            client.patch(session_url, json={"share_scope": "anyone"}).status_code == 200
        )
        switch.current = ANY_VIEWER
        assert client.get(snapshot_url).status_code == 200

        switch.current = OWNER
        assert (
            client.patch(session_url, json={"share_scope": "none"}).status_code == 200
        )
        switch.current = WS_VIEWER
        assert client.get(session_url).status_code == 404

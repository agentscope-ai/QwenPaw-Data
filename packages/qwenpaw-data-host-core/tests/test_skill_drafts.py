# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.algo.skill_create import SkillCreateManager
from qwenpaw_data.host.core.api.app import create_app
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    reset_engine,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import ChatRepository, SkillDraftRepository
from qwenpaw_data.host.core.repositories.mcp import McpRepository
from qwenpaw_data.host.core.settings import clear_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

SKILL_MD = """---
name: draft-funnel
description: Draft funnel skill
---

# draft-funnel
"""

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)


@pytest.fixture
def manager_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []

    async def fake_generate(*_args, **kwargs) -> None:
        calls.append(("generate", kwargs))

    async def fake_revise(*_args, **kwargs) -> None:
        calls.append(("revise", kwargs))

    monkeypatch.setattr(SkillCreateManager, "generate", fake_generate)
    monkeypatch.setattr(SkillCreateManager, "revise", fake_revise)
    return calls


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed_test_preferences,
    manager_calls,
):
    asyncio.run(reset_engine())
    reset_hub()
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    # The builtin context-manager MCP is an external service; these tests only
    # care about the Host-side skill-draft flow.
    monkeypatch.setattr(McpRepository, "sync", AsyncMock())
    app = create_app()
    with TestClient(app) as test_client:
        asyncio.run(seed_test_preferences(get_session_factory()))
        yield test_client, tmp_path / "home", manager_calls


def _write_draft_files(
    session_id: str,
    draft_id: str,
    files: dict[str, str],
    *,
    status: str = "ready",
) -> None:
    async def _write() -> None:
        async with get_session_factory()() as db:
            await SkillDraftRepository(db, TEST_IDENTITY).save_files(
                draft_id,
                session_id=session_id,
                files=files,
                status=status,
            )
            await db.commit()

    asyncio.run(_write())


def _session_paths(home: Path, session: dict) -> UserAgentPaths:
    return UserAgentPaths.from_identity(
        home=home,
        identity=TEST_IDENTITY,
        agent_id=session["agent_id"],
        session_id=session["id"],
    )


def _finished_chat(test_client: TestClient, session_id: str) -> dict:
    created = test_client.post(
        "/api/console/chat",
        json={
            "session_id": session_id,
            "text": "analyze gaap",
            "datasource_id": "ds_demo",
        },
    )
    assert created.status_code == 200, created.text
    chat = created.json()["chat"]
    chat_id = chat["id"]
    for _ in range(50):
        snapshot = test_client.get(f"/api/sessions/{session_id}/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        item = next(c for c in snapshot.json()["chats"] if c["id"] == chat_id)
        if item["status"] != "running":
            return item
    raise AssertionError("chat did not leave running")


def test_skill_draft_create_confirm_registers_directory(
    client: tuple[TestClient, Path, list],
) -> None:
    test_client, home, _calls = client
    session = test_client.post("/api/sessions", json={"title": "draft"}).json()[
        "session"
    ]
    chat = _finished_chat(test_client, session["id"])

    created = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts",
        json={"source_chat_id": chat["id"]},
    )
    assert created.status_code == 200, created.text
    draft = created.json()["skill_draft"]
    assert draft["status"] == "generating"
    assert draft["source_chat_id"] == chat["id"]
    _write_draft_files(
        session["id"],
        draft["id"],
        {
            "SKILL.md": SKILL_MD,
            "scripts/ok.py": "print(1)\n",
            "evidence/digest.md": "# digest\nshould not install",
            "report.md": "sidecar",
        },
    )

    listed = test_client.get(f"/api/sessions/{session['id']}/skill-drafts")
    assert listed.status_code == 200, listed.text
    assert listed.json()["count"] == 1
    assert "files" not in listed.json()["skill_drafts"][0]

    detail = test_client.get(
        f"/api/sessions/{session['id']}/skill-drafts/{draft['id']}"
    )
    assert detail.status_code == 200, detail.text
    ready = detail.json()["skill_draft"]
    assert ready["status"] == "ready"
    assert ready["name"] == "draft-funnel"
    assert "SKILL.md" in ready["files"]
    assert "scripts/ok.py" in ready["files"]

    confirmed = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts/{draft['id']}/confirm"
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["created"] == "draft-funnel"
    assert confirmed.json()["skill_draft"]["status"] == "accepted"

    paths = _session_paths(home, session)
    skill_md = paths.skills / "draft-funnel" / "SKILL.md"
    script = paths.skills / "draft-funnel" / "scripts" / "ok.py"
    assert skill_md.is_file()
    assert script.read_text(encoding="utf-8") == "print(1)\n"
    assert not (paths.skills / "draft-funnel" / "evidence").exists()
    assert not (paths.skills / "draft-funnel" / "report.md").exists()
    # `skill.json` manifests were retired: "source" is derived from the store
    # (a directory without the builtin marker is custom) and "enabled" from the
    # DB state, both surfaced through the skills API.
    listed_skills = test_client.get(f"/api/skills/agents/{session['agent_id']}")
    assert listed_skills.status_code == 200, listed_skills.text
    installed = next(
        item
        for item in listed_skills.json()["skills"]
        if item["name"] == "draft-funnel"
    )
    assert installed["source"] == "custom"
    assert installed["enabled"] is True


def test_skill_draft_create_and_revise_kick_background_job(
    client: tuple[TestClient, Path, list],
) -> None:
    test_client, _home, calls = client
    session = test_client.post("/api/sessions", json={"title": "bg"}).json()["session"]
    chat = _finished_chat(test_client, session["id"])
    created = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts",
        json={"source_chat_id": chat["id"]},
    )
    assert created.status_code == 200, created.text
    draft = created.json()["skill_draft"]
    assert draft["status"] == "generating"
    assert calls[0][0] == "generate"
    assert calls[0][1]["draft_id"] == draft["id"]
    assert calls[0][1]["chat_id"] == chat["id"]
    _write_draft_files(session["id"], draft["id"], {"SKILL.md": SKILL_MD})
    revised = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts/{draft['id']}/revise",
        json={"text": "收窄触发"},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["skill_draft"]["status"] == "revising"
    assert calls[1][0] == "revise"
    assert calls[1][1]["text"] == "收窄触发"


def test_skill_draft_revise_and_dismiss(client: tuple[TestClient, Path, list]) -> None:
    test_client, _home, _calls = client
    session = test_client.post("/api/sessions", json={"title": "revise"}).json()[
        "session"
    ]
    chat = _finished_chat(test_client, session["id"])
    draft_id = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts",
        json={"source_chat_id": chat["id"]},
    ).json()["skill_draft"]["id"]
    _write_draft_files(
        session["id"],
        draft_id,
        {"SKILL.md": SKILL_MD, "scripts/ok.py": "print(1)\n"},
    )

    revised = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts/{draft_id}/revise",
        json={"text": "收窄触发"},
    )
    assert revised.status_code == 200, revised.text
    assert revised.json()["skill_draft"]["status"] == "revising"
    _write_draft_files(
        session["id"],
        draft_id,
        {"SKILL.md": SKILL_MD + "\n# 收窄触发\n"},
    )

    detail = test_client.get(f"/api/sessions/{session['id']}/skill-drafts/{draft_id}")
    assert "收窄触发" in detail.json()["skill_draft"]["files"]["SKILL.md"]

    dismissed = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts/{draft_id}/dismiss"
    )
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["skill_draft"]["status"] == "dismissed"


def test_skill_draft_rejects_running_chat(
    client: tuple[TestClient, Path, list],
) -> None:
    test_client, _home, _calls = client
    session = test_client.post("/api/sessions", json={"title": "running"}).json()[
        "session"
    ]

    async def insert_running_chat() -> str:
        async with get_session_factory()() as db:
            chat = Chat.start(
                session_id=session["id"],
                identity=TEST_IDENTITY,
                sequence=1,
                datasource_id="ds_demo",
                text="still running",
            )
            await ChatRepository(db, TEST_IDENTITY).add(chat)
            await db.commit()
            return chat.id

    chat_id = asyncio.run(insert_running_chat())
    response = test_client.post(
        f"/api/sessions/{session['id']}/skill-drafts",
        json={"source_chat_id": chat_id},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION"

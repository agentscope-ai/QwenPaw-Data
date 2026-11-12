# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qwenpaw_data.host.core.algo.skill_evolution.models import (
    EvolutionProposal,
    ProposedPatch,
    SkillFileSnapshot,
)
from qwenpaw_data.host.core.algo.skill_evolution.validate import validate_and_materialize
from qwenpaw_data.host.core.api.app import create_app
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
from qwenpaw_data.host.core.repositories import SessionRepository, SkillProposalRepository
from qwenpaw_data.host.core.repositories.skill_revision import SkillRevisionRepository
from qwenpaw_data.host.core.settings import clear_settings, get_settings
from qwenpaw_data.host.core.stream.hub import reset_hub

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)
SAMPLE_SKILL_MD = "# skill\nstep 1\n"


def _agent_paths(home: Path, *, agent_id: str = "default") -> UserAgentPaths:
    return UserAgentPaths.from_identity(
        home=home,
        identity=TEST_IDENTITY,
        agent_id=agent_id,
        session_id="default",
    )


def _seed_sql_analyst(home: Path, *, content: str = SAMPLE_SKILL_MD) -> Path:
    skills_dir = _agent_paths(home).skills
    skill_md = skills_dir / "sql-analyst" / "SKILL.md"
    skill_md.parent.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(content, encoding="utf-8")
    return skill_md


async def _revision_rows() -> list[dict]:
    async with get_session_factory()() as db:
        return await SkillRevisionRepository(
            db, TEST_IDENTITY, agent_id="default"
        ).list_history("sql-analyst")


def _sample_patches() -> list[dict]:
    return validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step 1",
                new_string="step 1\nstep 2",
                reason="补步骤",
            )
        ],
        snapshots={
            ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                content="# skill\nstep 1\n",
            )
        },
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )


def _sample_proposal() -> EvolutionProposal:
    patches = _sample_patches()
    return EvolutionProposal(
        summary="补步骤",
        skills=["sql-analyst"],
        patch_count=len(patches),
        patches=patches,
    )


def _add_kwargs(proposal: EvolutionProposal, *, session_id: str, agent_id: str) -> dict:
    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "summary": proposal.summary,
        "mentioned_skill_names": list(proposal.skills),
        "modified_file_count": proposal.patch_count,
        "file_changes": [dict(item) for item in proposal.patches],
    }


@pytest.fixture
def skill_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(home))
    clear_settings()
    asyncio.run(reset_engine())
    reset_hub()
    home = get_settings().paths.home
    _seed_sql_analyst(home)

    async def seed() -> tuple[str, str]:
        await init_db()
        async with get_session_factory()() as db:
            session = Session.create(
                identity=TEST_IDENTITY,
                agent_id="default",
                title="skill-proposal",
            )
            await SessionRepository(db, TEST_IDENTITY).add(session)
            row = await SkillProposalRepository(db, TEST_IDENTITY).upsert_pending(
                **_add_kwargs(
                    _sample_proposal(), session_id=session.id, agent_id=session.agent_id
                ),
            )
            await db.commit()
            return session.id, row["id"]

    session_id, proposal_id = asyncio.run(seed())
    app = create_app()
    yield app, session_id, proposal_id, home


def test_upsert_roundtrip_and_keeps_same_pending_id() -> None:
    async def run() -> None:
        await reset_engine()
        await init_db()
        proposal = _sample_proposal()
        async with get_session_factory()() as db:
            session = Session.create(identity=TEST_IDENTITY, title="roundtrip")
            await SessionRepository(db, TEST_IDENTITY).add(session)
            repo = SkillProposalRepository(db, TEST_IDENTITY)
            first = await repo.upsert_pending(
                **_add_kwargs(
                    proposal, session_id=session.id, agent_id=session.agent_id
                ),
            )
            second = await repo.upsert_pending(
                **_add_kwargs(
                    proposal, session_id=session.id, agent_id=session.agent_id
                ),
            )
            detail = await repo.get(first["id"], session_id=session.id)
            pending = await repo.list_by_session(session.id, status="pending")
            await db.commit()
        assert first["file_changes"] == proposal.patches
        assert second["id"] == first["id"]
        assert detail["file_changes"] == proposal.patches
        assert [row["id"] for row in pending] == [first["id"]]

    asyncio.run(run())


def test_list_omits_file_changes_detail_includes_them(skill_app) -> None:
    app, session_id, proposal_id, _home = skill_app
    with TestClient(app) as client:
        listed = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals",
            params={"status": "pending"},
        )
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 1
        item = body["skill_proposals"][0]
        assert item["id"] == proposal_id
        assert "file_changes" not in item
        assert item["mentioned_skill_names"] == ["sql-analyst"]

        detail = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}"
        )
        assert detail.status_code == 200, detail.text
        proposal = detail.json()["skill_proposal"]
        assert proposal["file_changes"]
        assert proposal["file_changes"][0]["hunks"][0]["hunk_id"]


def test_workspace_list_returns_proposal_without_file_changes(skill_app) -> None:
    app, session_id, proposal_id, _home = skill_app
    with TestClient(app) as client:
        listed = client.get("/api/settlement/skill-proposals")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["count"] == 1
        item = body["skill_proposals"][0]
        assert item["id"] == proposal_id
        assert item["session_id"] == session_id
        assert "file_changes" not in item


def test_accept_writes_skill_and_revision(skill_app) -> None:
    app, session_id, proposal_id, home = skill_app
    skill_md = _agent_paths(home).skills / "sql-analyst" / "SKILL.md"
    with TestClient(app) as client:
        accepted = client.post(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}/accept",
            json={},
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["skill_proposal"]["status"] == "accepted"
    assert "step 2" in skill_md.read_text(encoding="utf-8")
    rows = asyncio.run(_revision_rows())
    assert any(
        item["origin"] == "evolution_accept" and item["source_ref"] == proposal_id
        for item in rows
    )


def test_accept_empty_hunks_is_400(skill_app) -> None:
    app, session_id, proposal_id, home = skill_app
    skill_md = _agent_paths(home).skills / "sql-analyst" / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    with TestClient(app) as client:
        empty = client.post(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}/accept",
            json={"hunk_ids": []},
        )
        assert empty.status_code == 400, empty.text
        assert empty.json()["code"] == "VALIDATION"
        pending = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}"
        )
        assert pending.json()["skill_proposal"]["status"] == "pending"
    assert skill_md.read_text(encoding="utf-8") == before
    assert asyncio.run(_revision_rows()) == []


def test_accept_conflict_when_skill_changed(skill_app) -> None:
    app, session_id, proposal_id, home = skill_app
    skill_md = _agent_paths(home).skills / "sql-analyst" / "SKILL.md"
    skill_md.write_text("changed by someone else\n", encoding="utf-8")
    with TestClient(app) as client:
        conflict = client.post(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}/accept",
            json={},
        )
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "CONFLICT"
        assert conflict.json()["details"]["reason"] == "SKILL_CHANGED"
        detail = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}"
        )
        assert detail.json()["skill_proposal"]["status"] == "pending"
    assert skill_md.read_text(encoding="utf-8") == "changed by someone else\n"
    assert asyncio.run(_revision_rows()) == []


def test_reject_does_not_write_files(skill_app) -> None:
    app, session_id, proposal_id, home = skill_app
    skill_md = _agent_paths(home).skills / "sql-analyst" / "SKILL.md"
    before = skill_md.read_text(encoding="utf-8")
    with TestClient(app) as client:
        rejected = client.post(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}/reject"
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["skill_proposal"]["status"] == "rejected"
        gone = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}"
        )
        assert gone.json()["skill_proposal"]["file_changes"][0]["status"] == "pending"
    assert skill_md.read_text(encoding="utf-8") == before
    assert asyncio.run(_revision_rows()) == []


def test_upsert_after_reject_creates_new_pending(skill_app) -> None:
    app, session_id, proposal_id, _home = skill_app
    with TestClient(app) as client:
        rejected = client.post(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}/reject"
        )
        assert rejected.status_code == 200, rejected.text

    async def write_next() -> str:
        async with get_session_factory()() as db:
            row = await SkillProposalRepository(db, TEST_IDENTITY).upsert_pending(
                **_add_kwargs(
                    _sample_proposal(), session_id=session_id, agent_id="default"
                ),
            )
            await db.commit()
            return row["id"]

    new_id = asyncio.run(write_next())
    assert new_id != proposal_id
    with TestClient(app) as client:
        old = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals/{proposal_id}"
        )
        assert old.status_code == 200, old.text
        assert old.json()["skill_proposal"]["status"] == "rejected"
        listed = client.get(
            f"/api/sessions/{session_id}/settlement/skill-proposals",
            params={"status": "pending"},
        )
        assert {item["id"] for item in listed.json()["skill_proposals"]} == {new_id}

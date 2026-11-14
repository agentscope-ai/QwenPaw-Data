# -*- coding: utf-8 -*-
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.algo.skill_create import SkillCreateManager
from qwenpaw_data.host.core.algo.skill_create.files import skill_payload_files
from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.requests import (
    CreateSkillDraftRequest,
    SkillDraftReviseRequest,
)
from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.domain.skill_manager import SkillManager
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories import ChatRepository, SkillDraftRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.host.core.utils.skill import write_skill_files

router = APIRouter(
    prefix="/sessions/{session_id}/skill-drafts",
    tags=["skill-drafts"],
)


def _raise(exc: Exception) -> None:
    mapped = map_domain_error(exc)
    if mapped is not None:
        raise mapped
    raise


async def _get_session_and_drafts(
    session_id: str,
    identity: Identity,
    db: AsyncSession,
) -> tuple[Session, SkillDraftRepository]:
    session = await access_session(db, identity, session_id, write=True)
    return session, SkillDraftRepository(db, identity)


async def _import_skill_files_to_agent(
    db: AsyncSession,
    session: Session,
    files: dict[str, str],
    *,
    source_ref: str = "",
) -> str:
    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=session.identity,
        agent_id=require_safe_name(session.agent_id),
        session_id=session.id,
    )
    manager = SkillManager(db, identity=session.identity, paths=paths)
    manager.ensure_ready()
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "skill"
        write_skill_files(staged, files)
        return await manager.import_dir(staged, source_ref=source_ref)


@router.post("")
async def create_skill_draft(
    session_id: str,
    body: CreateSkillDraftRequest,
    background: BackgroundTasks,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        session, drafts = await _get_session_and_drafts(session_id, identity, db)
        chat = await ChatRepository(db, identity).get(
            body.source_chat_id, session_id=session.id
        )
        if not chat.is_terminal():
            raise ValueError(f"source chat is not finished: {chat.id}")
        draft = await drafts.add(session_id=session.id, source_chat_id=chat.id)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    background.add_task(
        SkillCreateManager.generate,
        get_session_factory(),
        identity=identity,
        session_id=session.id,
        chat_id=chat.id,
        draft_id=draft["id"],
    )
    return {"skill_draft": draft}


@router.get("")
async def list_skill_drafts(
    session_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        _session, drafts = await _get_session_and_drafts(session_id, identity, db)
        items = await drafts.list_by_session(session_id)
    except Exception as exc:
        _raise(exc)
    return {"skill_drafts": items, "count": len(items)}


@router.get("/{draft_id}")
async def get_skill_draft(
    session_id: str,
    draft_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        _session, drafts = await _get_session_and_drafts(session_id, identity, db)
        draft = await drafts.get(draft_id, session_id=session_id)
    except Exception as exc:
        _raise(exc)
    return {"skill_draft": draft}


@router.post("/{draft_id}/revise")
async def revise_skill_draft(
    session_id: str,
    draft_id: str,
    body: SkillDraftReviseRequest,
    background: BackgroundTasks,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        text = body.text.strip()
        if not text:
            raise ValueError("text is required")
        _session, drafts = await _get_session_and_drafts(session_id, identity, db)
        draft = await drafts.mark_status(
            draft_id,
            session_id=session_id,
            status="revising",
            expected_status="ready",
        )
        await db.commit()
    except Exception as exc:
        _raise(exc)
    background.add_task(
        SkillCreateManager.revise,
        get_session_factory(),
        identity=identity,
        session_id=session_id,
        draft_id=draft_id,
        text=text,
    )
    return {"skill_draft": draft}


@router.post("/{draft_id}/confirm")
async def confirm_skill_draft(
    session_id: str,
    draft_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        session, drafts = await _get_session_and_drafts(session_id, identity, db)
        draft = await drafts.get(draft_id, session_id=session.id)
        if draft["status"] != "ready":
            raise ValueError(f"skill draft is not ready: {draft_id}")
        files = skill_payload_files(dict(draft["files"] or {}))
        if not str(files.get("SKILL.md") or "").strip():
            raise ValueError("SKILL.md is required")
        created = await _import_skill_files_to_agent(
            db, session, files, source_ref=draft_id
        )
        draft = await drafts.mark_status(
            draft_id,
            session_id=session.id,
            status="accepted",
            expected_status="ready",
        )
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"created": created, "skill_draft": draft}


@router.post("/{draft_id}/dismiss")
async def dismiss_skill_draft(
    session_id: str,
    draft_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        _session, drafts = await _get_session_and_drafts(session_id, identity, db)
        draft = await drafts.dismiss(draft_id, session_id=session_id)
        await db.commit()
    except Exception as exc:
        _raise(exc)
    return {"skill_draft": draft}

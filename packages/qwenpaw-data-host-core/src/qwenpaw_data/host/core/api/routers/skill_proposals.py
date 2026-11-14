# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution import SkillEvolutionManager
from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.requests import SkillProposalAcceptRequest
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import tracer_send_score
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.skill_proposal import SkillProposalRepository
from qwenpaw_data.host.core.settings import get_settings
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.tracing.core.names import Score
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/sessions/{session_id}/settlement/skill-proposals",
    tags=["skill-proposals"],
)
workspace_router = APIRouter(
    prefix="/settlement/skill-proposals",
    tags=["skill-proposals"],
)


def _manager(
    db: AsyncSession,
    identity: Identity,
    *,
    agent_id: str,
    session_id: str,
) -> SkillEvolutionManager:
    paths = UserAgentPaths.from_identity(
        home=get_settings().paths.home,
        identity=identity,
        agent_id=agent_id,
        session_id=session_id,
    )
    return SkillEvolutionManager(db, identity=identity, paths=paths)


@workspace_router.get("")
async def list_workspace_skill_proposals(
    status: str | None = Query(default=None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        items = await SkillProposalRepository(db, identity).list(
            status=status  # type: ignore[arg-type]
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"skill_proposals": items, "count": len(items)}


@router.get("")
async def list_skill_proposals(
    session_id: str,
    status: str | None = Query(default=None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        items = await SkillProposalRepository(db, identity).list_by_session(
            session_id,
            status=status,  # type: ignore[arg-type]
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"skill_proposals": items, "count": len(items)}


@router.get("/{proposal_id}")
async def get_skill_proposal(
    session_id: str,
    proposal_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        proposal = await SkillProposalRepository(db, identity).get(
            proposal_id, session_id=session_id
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"skill_proposal": proposal}


@router.post("/{proposal_id}/accept")
async def accept_skill_proposal(
    session_id: str,
    proposal_id: str,
    body: SkillProposalAcceptRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        session = await access_session(db, identity, session_id, write=True)
        proposal = await _manager(
            db, identity, agent_id=session.agent_id, session_id=session_id
        ).accept(
            proposal_id,
            session_id=session_id,
            patch_ids=body.patch_ids,
            hunk_ids=body.hunk_ids,
        )
        await _tracer_send_proposal_score(db, identity, proposal)
        await db.commit()
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"ok": True, "skill_proposal": proposal}


@router.post("/{proposal_id}/reject")
async def reject_skill_proposal(
    session_id: str,
    proposal_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        proposal = await SkillProposalRepository(db, identity).reject(
            proposal_id, session_id=session_id
        )
        await _tracer_send_proposal_score(db, identity, proposal)
        await db.commit()
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"ok": True, "skill_proposal": proposal}


async def _tracer_send_proposal_score(
    db: AsyncSession,
    identity: Identity,
    proposal: dict[str, Any],
) -> None:
    chat_id = str(proposal.get("source_chat_id") or "").strip()
    proposal_id = str(proposal.get("id") or "").strip()
    status = str(proposal.get("status") or "")
    if not chat_id or not proposal_id:
        return
    if status not in {"accepted", "partial", "rejected"}:
        return
    await tracer_send_score(
        db,
        identity,
        id=proposal_id,
        name=Score.SKILL_PROPOSAL,
        value=status,
        chat_id=chat_id,
    )

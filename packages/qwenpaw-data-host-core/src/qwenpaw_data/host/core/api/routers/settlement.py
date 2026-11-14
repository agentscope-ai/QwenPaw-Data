# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.requests import SettlementConfirmRequest
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.tracing.records import tracer_send_score
from qwenpaw_data.host.core.repositories import SettlementRepository
from qwenpaw_data.host.core.utils.session import access_session
from qwenpaw_data.tracing.core.names import Score
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/sessions/{session_id}/settlement", tags=["settlement"])
workspace_router = APIRouter(prefix="/settlement", tags=["settlement"])


@workspace_router.get("/cards")
async def list_workspace_settlement_cards(
    status: str | None = Query(default=None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        cards = await SettlementRepository(db, identity).list(
            status=status  # type: ignore[arg-type]
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"cards": cards, "count": len(cards)}


@router.get("/cards")
async def list_settlement_cards(
    session_id: str,
    status: str | None = Query(default=None),
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        repo = SettlementRepository(db, identity)
        if status == "pending":
            # Push path: keep returning queried cards; still mark new pending.
            pending = await repo.list_by_session(session_id, status="pending")
            queried = await repo.list_by_session(session_id, status="queried")
            if pending:
                await repo.mark_queried(session_id, [c["id"] for c in pending])
                for card in pending:
                    card["status"] = "queried"
            cards = pending + queried
        else:
            cards = await repo.list_by_session(session_id, status=status)
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"cards": cards, "count": len(cards)}


@router.post("/cards/{card_id}/confirm")
async def confirm_settlement_card(
    session_id: str,
    card_id: str,
    body: SettlementConfirmRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        from qwenpaw_data.host.core.algo.settlement import SettlementManager

        session = await access_session(db, identity, session_id, write=True)
        card = await SettlementRepository(db, identity).confirm(
            card_id,
            session_id=session_id,
            fields=body.fields,
        )
        await SettlementManager(db, identity=identity).schedule_cm_ingest(
            card,
            datasource_id=session.datasource_id,
        )
        await _tracer_send_card_score(db, identity, card, "confirmed")
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"ok": True, "card": card}


@router.post("/cards/{card_id}/dismiss")
async def dismiss_settlement_card(
    session_id: str,
    card_id: str,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        await access_session(db, identity, session_id, write=True)
        card = await SettlementRepository(db, identity).dismiss(
            card_id,
            session_id=session_id,
        )
        await _tracer_send_card_score(db, identity, card, "dismissed")
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"ok": True, "card": card}


async def _tracer_send_card_score(
    db: AsyncSession,
    identity: Identity,
    card: dict[str, Any],
    value: str,
) -> None:
    chat_id = str(card.get("source_chat_id") or "").strip()
    card_id = str(card.get("id") or "").strip()
    if not chat_id or not card_id:
        return
    await tracer_send_score(
        db,
        identity,
        id=card_id,
        name=Score.SETTLEMENT_CARD,
        value=value,
        chat_id=chat_id,
    )

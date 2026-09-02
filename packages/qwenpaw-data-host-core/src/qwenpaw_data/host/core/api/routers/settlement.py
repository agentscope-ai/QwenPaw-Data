# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from qwenpaw_data.host.core.api.deps import ServiceState, get_identity, get_state
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.requests import SettlementConfirmRequest
from qwenpaw_data.host.core.domain.identity import Identity

router = APIRouter(prefix="/sessions/{session_id}/settlement", tags=["settlement"])


@router.get("/cards")
async def list_settlement_cards(
    session_id: str,
    status: str | None = Query(default=None),
    identity: Identity = Depends(get_identity),
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await state.sessions.get(session_id)
        repo = state.settlement
        if status == "pending":
            # Poll path: keep returning queried cards; still mark new pending.
            pending = await repo.list_by_session(
                identity.user_id, session_id, status="pending"
            )
            queried = await repo.list_by_session(
                identity.user_id, session_id, status="queried"
            )
            if pending:
                await repo.mark_queried(
                    identity.user_id, session_id, [c["id"] for c in pending]
                )
                for card in pending:
                    card["status"] = "queried"
            cards = pending + queried
        else:
            cards = await repo.list_by_session(
                identity.user_id, session_id, status=status
            )
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
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        from qwenpaw_data.host.core.algo.settlement import SettlementManager

        session = await state.sessions.get(session_id)
        card = await state.settlement.confirm(
            identity.user_id,
            card_id,
            session_id=session_id,
            fields=body.fields,
        )
        SettlementManager(
            sessions=state.sessions,
            chats=state.chats,
            events=state.events,
            cards=state.settlement,
            identity=identity,
        ).schedule_cm_ingest(
            card,
            datasource_id=session.datasource_id,
        )
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
    state: ServiceState = Depends(get_state),
) -> dict[str, Any]:
    try:
        await state.sessions.get(session_id)
        card = await state.settlement.dismiss(
            identity.user_id,
            card_id,
            session_id=session_id,
        )
    except Exception as exc:
        http = map_domain_error(exc)
        if http:
            raise http from exc
        raise
    return {"ok": True, "card": card}

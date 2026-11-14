"""Authenticated, session-scoped generation of portable OSS plans."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.algo.sop import generate_sop
from qwenpaw_data.host.core.api.deps import get_db, get_identity
from qwenpaw_data.host.core.api.errors import map_domain_error
from qwenpaw_data.host.core.api.models.common import ApiModel
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.model_usage import (
    ModelUsageContext,
    ModelUsageSource,
)
from qwenpaw_data.host.core.providers.factory import build_model
from qwenpaw_data.host.core.repositories import PreferencesRepository
from qwenpaw_data.host.core.utils.session import access_session

router = APIRouter(prefix="/sessions/{session_id}/sop", tags=["sop"])


class GenerateSopRequest(ApiModel):
    text: str = Field(min_length=1, max_length=32768)


@router.post("/plan")
async def plan_sop(
    session_id: str,
    body: GenerateSopRequest,
    identity: Identity = Depends(get_identity),
    db: AsyncSession = Depends(get_db),
):
    try:
        if not body.text.strip():
            raise ValueError("prompt is required")
        session = await access_session(db, identity, session_id, write=True)
        config = (await PreferencesRepository(db, identity).load()).resolve()
        model = build_model(
            config.default,
            usage=ModelUsageContext(
                ModelUsageSource.DATA_AGENT,
                identity,
                session_id,
                "",
                source_step="sop_plan",
            ),
        )
        # The structured call owns no tools. Keep the user's configured model,
        # but consume one complete structured response instead of text deltas.
        model.stream = False
        plan = await generate_sop(model, body.text, datasource_id=session.datasource_id)
        return {"session_id": session_id, "plan": plan.to_dict()}
    except Exception as exc:
        mapped = map_domain_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise

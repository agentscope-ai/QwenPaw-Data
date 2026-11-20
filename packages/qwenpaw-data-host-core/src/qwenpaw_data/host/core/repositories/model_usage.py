# -*- coding: utf-8 -*-
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import ModelUsageRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.time import utcnow


class ModelUsageRepository:
    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    async def add(
        self,
        ctx: ModelUsageContext,
        *,
        provider_id: str,
        model_id: str,
        input_tokens: int,
        output_tokens: int,
        duration_ms: int,
        cache_read_tokens: int,
        cache_creation_tokens: int,
    ) -> None:
        if ctx.identity != self.identity:
            raise ValueError("usage identity mismatch")
        if not provider_id.strip() or not model_id.strip():
            raise ValueError("provider_id and model_id are required")
        if (
            min(
                input_tokens,
                output_tokens,
                duration_ms,
                cache_read_tokens,
                cache_creation_tokens,
            )
            < 0
        ):
            raise ValueError("tokens and duration must be >= 0")
        self.db.add(
            ModelUsageRow(
                id=create_id("usage"),
                user_id=ctx.identity.user_id,
                tenant_id=ctx.identity.tenant_id,
                workspace_id=ctx.identity.workspace_id,
                session_id=ctx.session_id,
                chat_id=ctx.chat_id,
                source=ctx.source.value,
                source_step=ctx.source_step,
                model_type=ctx.model_type.value,
                provider_id=provider_id,
                model_id=model_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_creation_tokens=cache_creation_tokens,
                duration_ms=duration_ms,
                created_at=utcnow(),
            )
        )
        await self.db.flush()

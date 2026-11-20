# -*- coding: utf-8 -*-
from __future__ import annotations

from agentscope.permission import PermissionContext, PermissionMode
from agentscope.state import AgentState
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import SessionAgentStateRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.time import utcnow


class AgentStateStore:
    """Persist AgentState per Session."""

    def __init__(self, db: AsyncSession, identity: Identity) -> None:
        self.db = db
        self.identity = identity

    async def load(self, session_id: str) -> AgentState:
        row = await self.db.get(SessionAgentStateRow, session_id)
        if row is None:
            return AgentState(
                permission_context=PermissionContext(mode=PermissionMode.BYPASS),
            )
        return AgentState.model_validate(dict(row.state_json or {}))

    async def save(self, session_id: str, state: AgentState) -> None:
        payload = state.model_dump(mode="json")
        now = utcnow()
        row = await self.db.get(SessionAgentStateRow, session_id)
        if row is None:
            self.db.add(
                SessionAgentStateRow(
                    session_id=session_id,
                    user_id=self.identity.user_id,
                    tenant_id=self.identity.tenant_id,
                    workspace_id=self.identity.workspace_id,
                    state_json=payload,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.state_json = payload
            row.updated_at = now
        await self.db.flush()

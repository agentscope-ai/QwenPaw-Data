# -*- coding: utf-8 -*-
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import AgentSkillStateRow
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.time import utcnow


class SkillStateRepository:
    """一个 agent 的技能 Host 侧状态。没有行即默认状态(启用)。"""

    def __init__(self, db: AsyncSession, identity: Identity, *, agent_id: str) -> None:
        self.db = db
        self.identity = identity
        self.agent_id = require_safe_name(agent_id)

    def _scope(self) -> list:
        return [
            AgentSkillStateRow.user_id == self.identity.user_id,
            AgentSkillStateRow.tenant_id == self.identity.tenant_id,
            AgentSkillStateRow.workspace_id == self.identity.workspace_id,
            AgentSkillStateRow.agent_id == self.agent_id,
        ]

    async def disabled_names(self) -> frozenset[str]:
        rows = await self.db.scalars(
            select(AgentSkillStateRow.skill_name).where(
                *self._scope(), AgentSkillStateRow.disabled.is_(True)
            )
        )
        return frozenset(rows.all())

    async def set_disabled(self, skill_name: str, disabled: bool) -> None:
        safe = require_safe_name(skill_name)
        row = await self._row(safe)
        if row is None:
            row = AgentSkillStateRow(
                user_id=self.identity.user_id,
                tenant_id=self.identity.tenant_id,
                workspace_id=self.identity.workspace_id,
                agent_id=self.agent_id,
                skill_name=safe,
                created_at=utcnow(),
            )
            self.db.add(row)
        row.disabled = disabled
        row.updated_at = utcnow()
        await self.db.flush()

    async def forget(self, skill_name: str) -> None:
        """卸载技能时丢掉它的状态行。"""
        row = await self._row(require_safe_name(skill_name))
        if row is not None:
            await self.db.delete(row)
            await self.db.flush()

    async def _row(self, skill_name: str) -> AgentSkillStateRow | None:
        return (
            await self.db.scalars(
                select(AgentSkillStateRow).where(
                    *self._scope(), AgentSkillStateRow.skill_name == skill_name
                )
            )
        ).first()

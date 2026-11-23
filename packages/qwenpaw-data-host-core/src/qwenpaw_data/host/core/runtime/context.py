# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.model_usage import ModelUsageContext, ModelUsageSource
from qwenpaw_data.host.core.paths import UserAgentPaths

if TYPE_CHECKING:
    from agentscope.mcp import MCPClient

    from qwenpaw_data.host.core.domain.preference import UserRuntimeConfig


@dataclass
class RunContext:
    """Ephemeral dependencies and identifiers for one QwenPawData run."""

    session_id: str
    chat_id: str
    workspace: Any
    paths: UserAgentPaths
    user_runtime_config: UserRuntimeConfig
    identity: Identity
    session_factory: async_sessionmaker[AsyncSession]
    disabled_skills: frozenset[str] = frozenset()
    request_context: dict[str, Any] = field(default_factory=dict)
    application_state: Any = field(default=None, repr=False)
    clarification: Any = field(default=None, repr=False)
    host_cm: MCPClient | None = field(default=None, repr=False)

    async def list_mcps(self) -> list[MCPClient]:
        clients = await self.workspace.list_mcps(
            agent_id=self.paths.agent_id, session_id=self.session_id
        )
        if self.host_cm is None:
            return clients
        return [c for c in clients if c.name != self.host_cm.name] + [self.host_cm]

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        if not self.chat_id.strip():
            raise ValueError("chat_id is required")
        self.request_context = dict(self.request_context)

    def model_usage(
        self, source: ModelUsageSource, source_step: str | StrEnum = ""
    ) -> ModelUsageContext:
        return ModelUsageContext(
            source, self.identity, self.session_id, self.chat_id, source_step
        )

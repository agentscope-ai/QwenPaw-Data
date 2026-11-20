# -*- coding: utf-8 -*-
"""Shared plumbing for review-agent read-only tools."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from agentscope.tool import ToolBase, ToolChunk
from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.session import SessionRepository
from qwenpaw_data.host.core.settings import get_settings

if TYPE_CHECKING:
    from qwenpaw_data.host.core.runtime.context import RunContext


class ReviewToolBase(ToolBase):
    """Base for review-session tools with identity-scoped access."""

    is_concurrency_safe = True
    is_read_only = True

    def __init__(self, run_context: RunContext) -> None:
        super().__init__()
        self._run_context = run_context

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        _ = (tool_input, context)
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message=f"{self.name} is always allowed.",
        )

    async def call(self, **kwargs: Any) -> ToolChunk:  # pragma: no cover - abstract
        raise NotImplementedError

    # -- shared helpers -------------------------------------------------

    def _ok(self, payload: Any) -> ToolChunk:
        return ToolChunk(
            content=[
                TextBlock(text=json.dumps(payload, ensure_ascii=False, default=str))
            ],
        )

    def _err(self, exc: Exception) -> ToolChunk:
        return ToolChunk(
            content=[TextBlock(text=f"{self.name}Error: {exc}")],
            state=ToolResultState.ERROR,
        )

    def _session_factory(self) -> Any:
        return get_session_factory()

    async def _reviewed_session(self, db: AsyncSession) -> Session:
        """Load the reviewed session via the current session's parent link."""
        repo = SessionRepository(db, self._run_context.identity)
        current = await repo.get(self._run_context.session_id)
        parent_id = current.parent_session_id
        if not parent_id:
            raise ValueError(
                "current session has no parent_session_id; "
                "review tools only work inside a review session"
            )
        reviewed = await repo.get(parent_id)
        identity = self._run_context.identity
        if (
            reviewed.identity.user_id != identity.user_id
            or reviewed.identity.tenant_id != identity.tenant_id
            or reviewed.identity.workspace_id != identity.workspace_id
        ):
            raise LookupError("reviewed session not found")
        return reviewed

    @staticmethod
    def _source_paths(reviewed: Session) -> UserAgentPaths:
        """Anchor the reviewed session's agent directory tree."""
        return UserAgentPaths.from_identity(
            home=get_settings().paths.home,
            identity=reviewed.identity,
            agent_id=reviewed.agent_id,
            session_id=reviewed.id,
        )

    @staticmethod
    def _resolve_artifact(artifact_dir: Path, relative_path: str) -> Path:
        """Resolve one artifact file; reject escapes and missing files."""
        rel = PurePosixPath(relative_path)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError("artifact path escapes session artifact directory")
        candidate = artifact_dir.joinpath(*rel.parts)
        if not candidate.is_file():
            raise LookupError(f"artifact file not found: {relative_path}")
        return candidate


def truncate_text(text: str, limit: int) -> str:
    """Clip one string, marking the cut so the model knows to page."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated, {len(text) - limit} chars omitted]"


def page_meta(total: int, offset: int, returned: int) -> dict[str, Any]:
    """State whether a paged window is cut short, and where to resume."""
    has_more = offset + returned < total
    return {
        "has_more": has_more,
        "next_offset": offset + returned if has_more else None,
    }


def plan_tasks(chat: Any) -> list[dict[str, Any]]:
    """Project one chat's plan snapshot (id/subject/description/state/blocked_by)."""
    plan = getattr(chat, "plan", None)
    tasks = getattr(plan, "tasks", None) if plan is not None else None
    return [
        {
            "id": getattr(task, "id", ""),
            "subject": getattr(task, "subject", ""),
            "description": getattr(task, "description", ""),
            "state": getattr(task, "state", ""),
            "blocked_by": list(getattr(task, "blocked_by", None) or []),
        }
        for task in (tasks or [])
    ]

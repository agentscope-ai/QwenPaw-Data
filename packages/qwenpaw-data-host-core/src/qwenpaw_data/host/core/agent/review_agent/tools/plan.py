# -*- coding: utf-8 -*-
"""Review tool: plan snapshots of the reviewed session, indexed by chat."""

from __future__ import annotations

from typing import Any

from agentscope.tool import ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.repositories.chat import ChatRepository

from .base import ReviewToolBase, page_meta, plan_tasks


class _GetPlanParams(BaseModel):
    # Reject non-expected tool input parameters
    model_config = ConfigDict(extra="forbid")

    chat_id: str | None = Field(
        default=None,
        description="只看某轮的计划快照；不填则返回带计划轮次的索引。",
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="索引起始位置（0 起）；默认自首个带计划轮次。",
    )
    limit: int = Field(
        default=50,
        ge=1,
        le=200,
        description="本窗口条数，默认前 50 条，最大 200。",
    )


class GetPlanTool(ReviewToolBase):
    name = "get_plan"
    description = (
        "读取被审核会话的计划快照，按轮次索引。不给 chat_id 时分页"
        "返回全部带计划的轮次，每项含轮次号、chat_id 与该轮完整"
        "任务清单（id、主题、描述、状态与依赖），has_more 为 true 时"
        "以 next_offset 继续翻页；给 chat_id 时返回"
        "该轮的任务清单。"
    )

    def __init__(self, run_context: Any) -> None:
        super().__init__(run_context)
        self.input_schema = _GetPlanParams.model_json_schema()

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            params = _GetPlanParams.model_validate(kwargs)
            async with self._session_factory()() as db:
                result = await self._run(params, db)
            return self._ok(result)
        except Exception as exc:
            return self._err(exc)

    async def _run(self, params: _GetPlanParams, db: Any) -> dict[str, Any]:
        reviewed = await self._reviewed_session(db)
        chats = await ChatRepository(db, self._run_context.identity).list_for_session(
            reviewed.id
        )
        # Plans are chat-scoped: every chat persists the snapshot of the moment
        # that turn ended, so the snapshots read as a per-turn index. Chats that
        # never touched the plan carry an empty snapshot, not None.
        if params.chat_id:
            target = next((c for c in chats if c.id == params.chat_id), None)
            if target is None:
                raise LookupError(f"chat not in reviewed session: {params.chat_id}")
            return {
                "reviewed_session_id": reviewed.id,
                "chat_id": target.id,
                "sequence": target.sequence,
                "tasks": plan_tasks(target),
            }
        snapshots = []
        for chat in chats:
            tasks = plan_tasks(chat)
            if not tasks:
                continue
            snapshots.append(
                {
                    "chat_id": chat.id,
                    "sequence": chat.sequence,
                    "tasks": tasks,
                }
            )
        window = snapshots[params.offset : params.offset + params.limit]
        return {
            "reviewed_session_id": reviewed.id,
            "total_snapshots": len(snapshots),
            "offset": params.offset,
            "limit": params.limit,
            **page_meta(len(snapshots), params.offset, len(window)),
            "snapshots": window,
        }

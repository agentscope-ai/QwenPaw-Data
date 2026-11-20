# -*- coding: utf-8 -*-
"""Review tool: chat-by-chat skeleton of the reviewed session."""

from __future__ import annotations

from typing import Any

from agentscope.tool import ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.repositories.artifact import ArtifactRepository
from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.settlement import SettlementRepository
from qwenpaw_data.host.core.repositories.skill_draft import SkillDraftRepository

from .base import ReviewToolBase, page_meta, plan_tasks

_USER_INPUT_LIMIT = 200
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class _GetOverviewParams(BaseModel):
    # Reject non-expected tool input parameters
    model_config = ConfigDict(extra="forbid")

    offset: int = Field(
        default=0,
        ge=0,
        description="轮次起始位置（0 起）；默认自首轮。",
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"本窗口轮数，默认前 {DEFAULT_LIMIT} 轮，最大 {MAX_LIMIT}。",
    )


class GetOverviewTool(ReviewToolBase):
    name = "get_overview"
    description = (
        "读取被审核会话的逐轮骨架：每轮一行，含轮次号、chat_id、状态、"
        "用户输入（截断）、计划任务主题列表与产物数。当会话轮数过多时，"
        "按 offset/limit 分页；has_more 为 true 表示轮次未列全，"
        "以 next_offset 继续翻页。第一页（offset=0）附带用户已确认的口径"
        "和 skill 修改等信息。"
    )

    def __init__(self, run_context: Any) -> None:
        super().__init__(run_context)
        self.input_schema = _GetOverviewParams.model_json_schema()

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            params = _GetOverviewParams.model_validate(kwargs)
            async with self._session_factory()() as db:
                result = await self._run(params, db)
            return self._ok(result)
        except Exception as exc:
            return self._err(exc)

    async def _run(self, params: _GetOverviewParams, db: Any) -> dict[str, Any]:
        reviewed = await self._reviewed_session(db)
        identity = self._run_context.identity
        chats = await ChatRepository(db, identity).list_for_session(reviewed.id)
        artifacts = await ArtifactRepository(db, identity).list_for_chats(
            [c.id for c in chats]
        )
        window = chats[params.offset : params.offset + params.limit]
        rows = [
            {
                "chat_id": c.id,
                "sequence": c.sequence,
                "status": c.status,
                "user_input": (c.user_input or "")[:_USER_INPUT_LIMIT],
                "plan_tasks": [task["subject"] for task in plan_tasks(c)],
                "artifact_count": len(artifacts.get(c.id, [])),
            }
            for c in window
        ]
        result: dict[str, Any] = {
            "reviewed_session_id": reviewed.id,
            "total_chats": len(chats),
            "offset": params.offset,
            "limit": params.limit,
            **page_meta(len(chats), params.offset, len(rows)),
            "chats": rows,
        }
        # Session-wide material rides on the first page only
        if params.offset == 0:
            result["confirmed_settlements"] = [
                {
                    "id": card["id"],
                    "type": card["type"],
                    "fields": card["fields"],
                    "source_chat_id": card["source_chat_id"],
                    "confirmed_at": card["confirmed_at"],
                }
                for card in await SettlementRepository(db, identity).list_by_session(
                    reviewed.id, status="confirmed"
                )
            ]
            # ``accepted`` drafts are skill edits the user confirmed;
            # file bodies are not included.
            result["confirmed_skill_drafts"] = [
                {
                    "id": draft["id"],
                    "name": draft["name"],
                    "source_chat_id": draft["source_chat_id"],
                    "updated_at": draft["updated_at"],
                }
                for draft in await SkillDraftRepository(db, identity).list_by_session(
                    reviewed.id
                )
                if draft["status"] == "accepted"
            ]
        return result

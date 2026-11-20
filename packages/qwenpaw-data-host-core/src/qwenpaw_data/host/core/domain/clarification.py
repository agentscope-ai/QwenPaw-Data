# -*- coding: utf-8 -*-
"""Clarification interaction lifecycle."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from agentscope.message import TextBlock, ToolResultBlock, ToolResultState
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.db.tables import ChatRow
from qwenpaw_data.host.core.repositories.chat_command import ChatCommandRepository
from qwenpaw_data.host.core.settings import get_settings

ASK_USER_QUESTION = "ask_user_question"
ABSENT_USER_CLARIFICATION_TEXT = (
    "用户现在不在，无法作答。这不是选项里的任何一项。"
    "能在不违背既有规则、也不编造缺失事实的前提下继续，就自己选定并在回复里写明假设，继续执行。"
    "缺了这条信息就做不下去，或继续会违反其它指令，就停止本轮：说明卡在哪、缺什么，"
    "不要再问用户，不要再调用本工具。"
)


def automatic_clarification_result(tool_input: Any) -> dict[str, Any]:
    payload = json.loads(tool_input) if isinstance(tool_input, str) else tool_input
    return {
        "status": "answered",
        "answers": [
            {
                "question": item["question"],
                "selected_options": [],
                "custom_text": ABSENT_USER_CLARIFICATION_TEXT,
            }
            for item in payload["questions"]
        ],
    }


class ClarificationNotFound(LookupError):
    def __init__(self, message: str = "clarification not found") -> None:
        self.reason = "CLARIFICATION_NOT_FOUND"
        super().__init__(message)


class ClarificationWithFrontend:
    """Host ↔ Frontend: Clarification interaction metadata."""

    @staticmethod
    def tool_call_metadata(*, now: datetime | None = None) -> dict[str, str]:
        expires_at = (now or datetime.now(UTC)) + timedelta(
            seconds=get_settings().clarification.hint_ttl_seconds
        )
        return {
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }


class ClarificationWithExecutor:
    """Park a clarification request until its command arrives."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._chat_id = chat_id

    @staticmethod
    def is_timeout(tool_result: ToolResultBlock) -> bool:
        output = tool_result.output[0]
        text = output.text if isinstance(output, TextBlock) else str(output)
        return json.loads(text).get("status") == "timeout"

    @staticmethod
    def _to_tool_result_block(
        *,
        call_id: str,
        result: dict[str, Any],
    ) -> ToolResultBlock:
        return ToolResultBlock(
            id=call_id,
            name=ASK_USER_QUESTION,
            output=[TextBlock(text=json.dumps(result, ensure_ascii=False))],
            state=ToolResultState.SUCCESS,
        )

    def add_metadata(
        self,
        *,
        tool_name: str,
        metadata: dict[str, Any],
    ) -> None:
        if tool_name != ASK_USER_QUESTION:
            return
        metadata.update(ClarificationWithFrontend.tool_call_metadata())

    async def wait_for_answer(
        self, call_id: str, *, tool_input: Any = None
    ) -> ToolResultBlock:
        if not call_id.strip():
            raise ValueError("call_id is required")
        interval = get_settings().stream.sse_poll_seconds
        while True:
            async with self._session_factory() as db:
                payload = await ChatCommandRepository(db).take_clarification(
                    self._chat_id, call_id
                )
                if payload is not None:
                    await db.commit()
                    return self._to_tool_result_block(call_id=call_id, result=payload)
                row = await db.get(ChatRow, self._chat_id)
                if row is not None and not row.enable_clarification:
                    return self._to_tool_result_block(
                        call_id=call_id,
                        result=automatic_clarification_result(tool_input),
                    )
            await asyncio.sleep(interval)

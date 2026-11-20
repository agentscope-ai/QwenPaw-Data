# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.db.tables import ChatRow
from qwenpaw_data.host.core.settings import get_settings

ToolConfirmationLevel = Literal["off", "auto", "all"]
ToolConfirmationDecision = Literal["allow", "ask"]
ToolConfirmationAnswer = Literal["approved", "denied", "timeout"]


class ToolConfirmation:
    """一次工具调用的确认。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_id: str,
        *,
        level: str,
    ) -> None:
        """绑定这一轮的级别和 Chat。"""
        self._session_factory = session_factory
        self._chat_id = chat_id
        self.level = level

    def decide(self, tool_name: str) -> ToolConfirmationDecision:
        """off 放行；all 除 ask_user_question 外都问；auto 走 decide_auto。"""
        name = tool_name.strip()
        if not name:
            raise ValueError("tool name is required")
        if name == "ask_user_question":
            return "allow"
        if self.level == "off":
            return "allow"
        if self.level == "all":
            return "ask"
        return self.decide_auto(name)

    def decide_auto(self, name: str) -> ToolConfirmationDecision:
        """auto 检查。现在只看名单；后续参数 / 模型检查加在这里。"""
        names = get_settings().tool_confirmation.auto_ask_names
        return "ask" if name in names else "allow"

    def add_metadata(self, *, tool_name: str, metadata: dict[str, Any]) -> None:
        """待确认时写入 awaiting_confirmation / expires_at。"""
        if self.decide(tool_name) != "ask":
            return
        if metadata is None:
            raise ValueError("metadata is required")
        expires = datetime.now(UTC) + timedelta(
            seconds=get_settings().tool_confirmation.ttl_seconds
        )
        metadata["awaiting_confirmation"] = True
        metadata["expires_at"] = expires.isoformat().replace("+00:00", "Z")
        metadata["reason"] = f"tool_confirmation:{self.level}"

    async def wait(self, call_id: str) -> ToolConfirmationAnswer:
        """等人确认。Chat 结束返回 denied，超时返回 timeout。"""
        if not call_id.strip():
            raise ValueError("call_id is required")
        interval = get_settings().stream.sse_poll_seconds
        deadline = datetime.now(UTC) + timedelta(
            seconds=get_settings().tool_confirmation.ttl_seconds
        )
        from qwenpaw_data.host.core.repositories.chat_command import ChatCommandRepository

        while datetime.now(UTC) < deadline:
            async with self._session_factory() as db:
                payload = await ChatCommandRepository(db).take_tool_confirmation(
                    self._chat_id, call_id
                )
                if payload is not None:
                    await db.commit()
                    return payload
                row = await db.get(ChatRow, self._chat_id)
                if row is None or row.status != "running":
                    return "denied"
            await asyncio.sleep(interval)
        return "timeout"

    @staticmethod
    def denied_response(tool_name: str, *, reason: str) -> ToolResponse:
        """这次工具失败回执。不取消 Chat。"""
        return ToolResponse(
            content=[TextBlock(text=f"Tool '{tool_name}' was not executed: {reason}.")],
            state=ToolResultState.DENIED,
        )

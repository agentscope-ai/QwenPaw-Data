# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from agentscope.message import TextBlock, ToolResultState
from agentscope.tool import ToolResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from qwenpaw_data.host.core.db.tables import ChatRow
from qwenpaw_data.host.core.settings import get_settings

UI_TOOL_NAMES = frozenset({"ui_inspect", "ui_open_page", "ui_open_object", "ui_act"})
TTL_SECONDS = 60


def ui_tools_enabled() -> bool:
    return get_settings().tools.ui_enabled


def require_ui_tools() -> None:
    if not ui_tools_enabled():
        raise RuntimeError("CONFLICT: UI tools are disabled")


def failure(status: str, code: str) -> dict[str, Any]:
    return {"status": status, "error": {"code": code, "message": code}}


class UiExecution:
    """一次前端 UI 工具调用。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        chat_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._chat_id = chat_id

    def add_metadata(self, *, tool_name: str, metadata: dict[str, Any]) -> None:
        """UI 工具写入 awaiting_ui / expires_at。"""
        if not ui_tools_enabled() or tool_name not in UI_TOOL_NAMES:
            return
        if metadata is None:
            raise ValueError("metadata is required")
        expires = datetime.now(UTC) + timedelta(seconds=TTL_SECONDS)
        metadata["awaiting_ui"] = True
        metadata["expires_at"] = expires.isoformat().replace("+00:00", "Z")

    async def wait(self, call_id: str) -> dict[str, Any]:
        """等前端回包。Chat 结束返回 cancelled，超时返回 failed。"""
        if not call_id.strip():
            raise ValueError("call_id is required")
        interval = get_settings().stream.sse_poll_seconds
        deadline = datetime.now(UTC) + timedelta(seconds=TTL_SECONDS)
        from qwenpaw_data.host.core.repositories.chat_command import ChatCommandRepository

        while datetime.now(UTC) < deadline:
            async with self._session_factory() as db:
                payload = await ChatCommandRepository(db).take_ui(
                    self._chat_id, call_id
                )
                if payload is not None:
                    await db.commit()
                    return payload
                row = await db.get(ChatRow, self._chat_id)
                if row is None or row.status != "running":
                    return failure("cancelled", "CANCELLED")
            await asyncio.sleep(interval)
        return failure("failed", "TIMEOUT")

    @staticmethod
    def response(outcome: dict[str, Any]) -> ToolResponse:
        """把前端回包变成工具结果。"""
        return ToolResponse(
            content=[TextBlock(text=json.dumps(outcome, ensure_ascii=False))],
            state=ToolResultState.SUCCESS
            if outcome.get("status") == "executed"
            else ToolResultState.ERROR,
        )

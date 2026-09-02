# -*- coding: utf-8 -*-
"""Clarification domain: executor-facing pause/resume for ask_user_question."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from agentscope.message import TextBlock, ToolResultBlock, ToolResultState

ASK_USER_QUESTION = "ask_user_question"

CLARIFICATION_HINT_TTL_SECONDS = 300


class ClarificationConflict(RuntimeError):
    """HTTP-mappable conflict for clarification REST."""

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(f"CONFLICT: {message}")


class ClarificationNotFound(LookupError):
    def __init__(self, message: str = "clarification not found") -> None:
        self.reason = "CLARIFICATION_NOT_FOUND"
        super().__init__(message)


class ClarificationWithFrontend:
    """Host ↔ Frontend: Clarification interaction metadata."""

    @staticmethod
    def tool_call_metadata(*, now: datetime | None = None) -> dict[str, str]:
        expires_at = (now or datetime.now(UTC)) + timedelta(
            seconds=CLARIFICATION_HINT_TTL_SECONDS
        )
        return {
            "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
        }


class ClarificationWithExecutor:
    """Executor-facing pause/resume for one ask_user_question."""

    def __init__(self) -> None:
        self._call_id: str | None = None
        self._result: asyncio.Future[dict[str, Any]] | None = None

    @property
    def call_id(self) -> str | None:
        return self._call_id

    @property
    def is_pending(self) -> bool:
        return self._result is not None and not self._result.done()

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

    def answer(
        self,
        *,
        clarification_id: str,
        result: dict[str, Any],
    ) -> None:
        future = self._result
        if future is None or self._call_id is None:
            raise ClarificationNotFound()
        if self._call_id != clarification_id or future.done():
            raise ClarificationConflict(
                "clarification is not awaiting this answer",
                reason="CLARIFICATION_ALREADY_RESOLVED",
            )
        future.set_result(result)

    async def wait_for_answer(self, call_id: str) -> ToolResultBlock:
        """Park until ``answer`` delivers a result for ``call_id``."""
        if not call_id.strip():
            raise ValueError("call_id is required")
        self._call_id = call_id
        self._result = asyncio.get_running_loop().create_future()
        try:
            payload = await self._result
            return self._to_tool_result_block(call_id=call_id, result=payload)
        finally:
            self._call_id = None
            self._result = None

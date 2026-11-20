# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
import pytest
from agentscope.event import (
    ExternalExecutionResultEvent,
    RequireExternalExecutionEvent,
)
from agentscope.message import (
    TextBlock,
    ToolCallBlock,
    ToolResultBlock,
    ToolResultState,
    UserMsg,
)

from qwenpaw_data.host.core.agent.middleware import query_log as query_log_module
from qwenpaw_data.host.core.agent.middleware.query_log import QueryLogMiddleware
from qwenpaw_data.host.core.query_log import Step

_CALL_ID = "call-1"
_TOOL_INPUT = json.dumps(
    {
        "title": "确认分析时间范围",
        "questions": [
            {
                "question": "你说的「最近」是指哪个时间范围？",
                "multiSelect": False,
                "options": [{"label": "近 7 天"}, {"label": "近 30 天"}],
            }
        ],
    },
    ensure_ascii=False,
)
_ANSWER = {
    "status": "answered",
    "answers": [
        {
            "question": "你说的「最近」是指哪个时间范围？",
            "selected_options": ["近 30 天"],
            "custom_text": None,
        }
    ],
}


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(cur_iter=1, reply_id="reply-1"),
        react_config=SimpleNamespace(max_iters=20),
    )


def _middleware() -> QueryLogMiddleware:
    return QueryLogMiddleware(
        run_context=SimpleNamespace(chat_id="chat_1", session_id="ses_1"),
    )


def _handler(events: list[Any]):
    async def next_handler(**_kwargs: Any):
        for event in events:
            yield event

    return next_handler


def _required() -> RequireExternalExecutionEvent:
    return RequireExternalExecutionEvent(
        reply_id="reply-1",
        tool_calls=[
            ToolCallBlock(
                id=_CALL_ID,
                name="ask_user_question",
                input=_TOOL_INPUT,
            )
        ],
    )


def _answered() -> ExternalExecutionResultEvent:
    return ExternalExecutionResultEvent(
        reply_id="reply-1",
        execution_results=[
            ToolResultBlock(
                id=_CALL_ID,
                name="ask_user_question",
                output=[TextBlock(text=json.dumps(_ANSWER, ensure_ascii=False))],
                state=ToolResultState.SUCCESS,
            )
        ],
    )


def _records_by_step(records: list[dict[str, Any]], step: Step) -> list[dict[str, Any]]:
    return [row for row in records if row["step"] == step]


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    def _capture(
        *,
        request_id: str,
        step: Step,
        message: str,
        context: dict[str, Any] | None = None,
    ) -> None:
        rows.append(
            {
                "request_id": request_id,
                "step": step,
                "message": message,
                "context": context or {},
            }
        )

    monkeypatch.setattr(query_log_module, "log_info", _capture)
    return rows


@pytest.mark.asyncio
async def test_pause_emits_tool_start_before_aclose(
    recorded: list[dict[str, Any]],
) -> None:
    mw = _middleware()
    gen = mw.on_reply(
        agent=_agent(),
        input_kwargs={"inputs": UserMsg(name="user", content="看最近的销售")},
        next_handler=_handler([_required()]),
    )
    async for _ in gen:
        break
    await gen.aclose()

    starts = _records_by_step(recorded, Step.TOOL_START)
    assert len(starts) == 1
    ctx = starts[0]["context"]
    assert ctx["tool_name"] == "ask_user_question"
    assert ctx["tool_id"] == _CALL_ID
    assert ctx["input"] == _TOOL_INPUT
    assert _records_by_step(recorded, Step.TOOL_END) == []


@pytest.mark.asyncio
async def test_answer_emits_tool_end(
    recorded: list[dict[str, Any]],
) -> None:
    mw = _middleware()
    agent = _agent()
    gen = mw.on_reply(
        agent=agent,
        input_kwargs={"inputs": UserMsg(name="user", content="看最近的销售")},
        next_handler=_handler([_required()]),
    )
    async for _ in gen:
        break
    await gen.aclose()

    async for _ in mw.on_reply(
        agent=agent,
        input_kwargs={"inputs": _answered()},
        next_handler=_handler([]),
    ):
        pass

    starts = _records_by_step(recorded, Step.TOOL_START)
    ends = _records_by_step(recorded, Step.TOOL_END)
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["context"]["tool_id"] == ends[0]["context"]["tool_id"] == _CALL_ID
    assert json.loads(ends[0]["context"]["output"]) == _ANSWER

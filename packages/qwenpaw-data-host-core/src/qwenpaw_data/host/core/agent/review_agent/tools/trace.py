# -*- coding: utf-8 -*-
"""Review tool: paged execution timeline of the reviewed session."""

from __future__ import annotations

from typing import Any

from agentscope.tool import ToolChunk
from pydantic import BaseModel, ConfigDict, Field

from qwenpaw_data.host.core.repositories.chat import ChatRepository
from qwenpaw_data.host.core.repositories.chat_event import ChatEventRepository

from .base import ReviewToolBase, page_meta, truncate_text

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_MAX_TEXT_CHARS = 8000
_MAX_TOOL_CHARS = 4000
_MAX_RESULT_CHARS = 4000


class _GetTraceParams(BaseModel):
    # Reject non-expected tool input parameters
    model_config = ConfigDict(extra="forbid")

    chat_id: str = Field(description="要读取的轮次 chat_id。")
    offset: int = Field(
        default=0,
        ge=0,
        description="该轮轨迹的起始位置（0 起）；默认自该轮开头。",
    )
    limit: int = Field(
        default=DEFAULT_LIMIT,
        ge=1,
        le=MAX_LIMIT,
        description=f"本窗口条数，默认前 {DEFAULT_LIMIT} 条，最大 {MAX_LIMIT}。",
    )


class GetTraceTool(ReviewToolBase):
    name = "get_trace"
    description = (
        "读取被审核会话中指定轮次的执行轨迹，chat_id 必填。"
        "events 是该轮按时间顺序排列的事件列表：用户/助手消息的文本、"
        "助手思考过程、运行时注入的提示（hint）、"
        "工具调用的名称、arguments、output 与 state"
        "（结果未记录的调用仅含 arguments）；"
        "超长文本截断并标注。events 以 offset/limit 分页，"
        "total_events 为该轮事件总数；has_more 为 true 表示该轮未读完，"
        "以 next_offset 继续翻页。"
    )

    def __init__(self, run_context: Any) -> None:
        super().__init__(run_context)
        self.input_schema = _GetTraceParams.model_json_schema()

    async def call(self, **kwargs: Any) -> ToolChunk:
        try:
            params = _GetTraceParams.model_validate(kwargs)
            async with self._session_factory()() as db:
                result = await self._run(params, db)
            return self._ok(result)
        except Exception as exc:
            return self._err(exc)

    async def _run(self, params: _GetTraceParams, db: Any) -> dict[str, Any]:
        reviewed = await self._reviewed_session(db)
        identity = self._run_context.identity
        chat = await ChatRepository(db, identity).get(
            params.chat_id, session_id=reviewed.id
        )

        events = await ChatEventRepository(db, identity).list_for_chats([chat.id])
        # Streaming fragments (``content``) dominate the event table by
        # row count; only ``message`` rows feed the timeline and counts.
        # ``in_progress`` rows are earlier drafts of the same message.
        kept: list[Any] = []
        for event in events:
            if event.object != "message":
                continue
            payload = event.payload or {}
            if payload.get("status") != "completed":
                continue
            # Whitelist the conversational roles: an unknown role must
            # not fall into the tool-result pairing in the timeline.
            if payload.get("role") not in ("user", "assistant", "tool"):
                continue
            kept.append(event)

        timeline = self._build_timeline(kept)
        window = timeline[params.offset : params.offset + params.limit]
        return {
            "reviewed_session_id": reviewed.id,
            "chat_id": chat.id,
            "sequence": chat.sequence,
            "total_events": len(timeline),
            "offset": params.offset,
            "limit": params.limit,
            **page_meta(len(timeline), params.offset, len(window)),
            "events": window,
        }

    @staticmethod
    def _build_timeline(events: list[Any]) -> list[dict[str, Any]]:
        """Assemble one chat's timeline in event order."""
        entries: list[dict[str, Any]] = []
        pending: dict[str, dict[str, Any]] = {}
        for event in events:
            payload = event.payload or {}
            role = payload.get("role")
            blocks = [b for b in (payload.get("content") or []) if isinstance(b, dict)]
            if role in ("user", "assistant"):
                text = "\n".join(
                    part
                    for part in (
                        block.get("text") or ""
                        for block in blocks
                        if block.get("type") == "text"
                    )
                    if part
                )
                if text:
                    # Row type tells what the text actually is: ``hint``
                    # rows are runtime-injected reminders (time, context
                    # length), not what the user typed; ``reasoning`` rows
                    # are the assistant's own thinking.
                    msg_type = payload.get("type")
                    entry_role = msg_type if msg_type in ("hint", "reasoning") else role
                    entries.append(
                        {
                            "kind": "message",
                            "seq": event.sequence_number,
                            "role": entry_role,
                            "text": truncate_text(text, _MAX_TEXT_CHARS),
                        }
                    )
            for block in blocks:
                if block.get("type") != "data":
                    continue
                data = block.get("data") or {}
                if not isinstance(data, dict):
                    continue
                call_id = data.get("call_id")
                if not call_id:
                    continue
                name = str(data.get("name") or "")
                if role == "assistant":
                    # The call: hold it until its result arrives.
                    entry = pending.get(call_id) or {
                        "kind": "tool",
                        "call_id": call_id,
                        "name": name,
                    }
                    if not entry["name"]:
                        entry["name"] = name
                    arguments = data.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        entry["arguments"] = truncate_text(arguments, _MAX_TOOL_CHARS)
                    entry["_call_seq"] = event.sequence_number
                    pending[call_id] = entry
                else:
                    # The result: the call materializes here.
                    entry = pending.pop(
                        call_id,
                        {"kind": "tool", "call_id": call_id, "name": name},
                    )
                    if name:
                        entry["name"] = name
                    output = data.get("output")
                    if isinstance(output, str) and output:
                        entry["output"] = truncate_text(output, _MAX_RESULT_CHARS)
                    # ``state`` marks failed / interrupted calls apart
                    # from successful ones.
                    entry["state"] = data.get("state")
                    # The call-position bookkeeping stays internal;
                    # the entry lands at the result position.
                    entry.pop("_call_seq", None)
                    entry["seq"] = event.sequence_number
                    entries.append(entry)
        for entry in pending.values():
            # A call whose result never arrived (interrupted?) is still
            # evidence; list it at its call position.
            entry["seq"] = entry.pop("_call_seq")
            entry["note"] = "result not recorded"
            entries.append(entry)
        entries.sort(key=lambda item: item["seq"])
        return entries

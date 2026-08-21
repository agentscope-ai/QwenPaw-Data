# -*- coding: utf-8 -*-
"""QwenPaw Data Middleware 集合 —— agentscope 2.0 MiddlewareBase 实现。

5 个 Middleware 替代 v1.x 的 mixin override：
- QwenPawDataPromptMiddleware: 构建 master prompt + env hint (on_system_prompt)
- QwenPawDataHintMiddleware: 追加 DAG 状态提示 (on_system_prompt)
- QwenPawDataReplyMiddleware: pending edits 注入 + trigger_msg_id (on_reply)
- QwenPawDataTraceMiddleware: trace 收集 (on_reasoning + on_acting)
- QwenPawDataSandboxContextMiddleware: sandbox context 同步 (on_acting)
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Awaitable
from typing import TYPE_CHECKING, Any, Callable

from agentscope.middleware import MiddlewareBase
from agentscope.message import Msg, TextBlock
from agentscope.tool import ToolResponse

if TYPE_CHECKING:
    from agentscope.agent import Agent

    from .state import RuntimeStateManager

logger = logging.getLogger(__name__)

_SANDBOX_CONTEXT_TOOL_NAMES = {
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
}


def _format_pending_edits(edits: list[dict]) -> str:
    """Render external task graph edits into a compact LLM-readable summary."""
    from ..agent.formatting import format_pending_edits

    return format_pending_edits(edits)


def _format_trace_value(value: Any) -> str:
    if hasattr(value, "value"):
        value = value.value
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text.replace("\n", "\\n")


def _tool_result_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return _format_trace_value(payload)

    content = payload.get("content")
    if not isinstance(content, list):
        return _format_trace_value(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, dict):
            if "text" in block:
                parts.append(str(block.get("text") or ""))
            elif "data" in block:
                parts.append(_format_trace_value(block.get("data")))
            else:
                parts.append(_format_trace_value(block))
        else:
            text = getattr(block, "text", None)
            parts.append(str(text) if text is not None else _format_trace_value(block))
    return "".join(parts)


def _payload_event_type(payload: Any) -> str:
    if not isinstance(payload, dict):
        return type(payload).__name__
    raw_type = (
        payload.get("type")
        or payload.get("event_type")
        or payload.get("__type__")
        or type(payload).__name__
    )
    return _format_trace_value(raw_type)


def _normalized_event_type(payload: Any) -> str:
    return _payload_event_type(payload).replace("_", "").upper()


def _is_start_event(normalized: str) -> bool:
    return normalized.endswith("START") or normalized.endswith("STARTEVENT")


def _is_delta_event(normalized: str) -> bool:
    return normalized.endswith("DELTA") or normalized.endswith("DELTAEVENT")


def _is_end_event(normalized: str) -> bool:
    return normalized.endswith("END") or normalized.endswith("ENDEVENT")


class QwenPawDataPromptMiddleware(MiddlewareBase):
    """构建 QwenPaw Data master prompt + mode prompt + environment hint。

    Agent.__init__ 的 system_prompt="" 设为空，完全由此 middleware 生成。
    """

    def __init__(
        self,
        *,
        mode_getter: Callable[[], str],
        session_id: str,
        prompt_dir: Any = None,
    ) -> None:
        self._mode_getter = mode_getter
        self._session_id = session_id
        self._prompt_dir = prompt_dir

    async def on_system_prompt(self, agent: "Agent", current_prompt: str) -> str:
        from ..prompts import analysis_environment_hint, build_master_prompt

        mode = self._mode_getter()
        sys_prompt = build_master_prompt(mode=mode, prompt_dir=self._prompt_dir)
        env_hint = analysis_environment_hint(
            session_id=self._session_id,
            prompt_dir=self._prompt_dir,
        )
        if env_hint:
            sys_prompt = sys_prompt + "\n\n" + env_hint
        return sys_prompt


class QwenPawDataHintMiddleware(MiddlewareBase):
    """将 DAG 状态提示追加到 system prompt。"""

    def __init__(self, runtime_state: "RuntimeStateManager") -> None:
        self._rs = runtime_state

    async def on_system_prompt(self, agent: "Agent", current_prompt: str) -> str:
        hint = self._rs.get_current_hint()
        if hint:
            return current_prompt + "\n\n" + hint
        return current_prompt


class QwenPawDataReplyMiddleware(MiddlewareBase):
    """处理 pending edits 注入和 trigger_msg_id 设置。"""

    def __init__(self, runtime_state: "RuntimeStateManager") -> None:
        self._rs = runtime_state

    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        inputs = input_kwargs.get("inputs")
        self._set_trigger_msg_id(inputs)
        await self._inject_pending_edits(agent)
        async for event in next_handler():
            yield event

    def _set_trigger_msg_id(self, inputs: Any) -> None:
        trigger_msg_id = ""
        if isinstance(inputs, Msg):
            trigger_msg_id = getattr(inputs, "id", "") or ""
        elif isinstance(inputs, list) and inputs:
            trigger_msg_id = getattr(inputs[0], "id", "") or ""
        self._rs.set_trigger_msg_id(trigger_msg_id)

    async def _inject_pending_edits(self, agent: "Agent") -> None:
        edits = self._rs.pop_pending_edits()
        if not edits:
            return
        edit_text = _format_pending_edits(edits)
        edit_msg = Msg(
            name="system",
            content=[TextBlock(type="text", text=f"[外部变更通知]\n{edit_text}")],
            role="system",
        )
        try:
            memory = getattr(agent, "memory", None)
            if memory is not None:
                await memory.add(edit_msg)
        except Exception:
            logger.warning("Failed to inject QwenPaw Data pending edits", exc_info=True)


class QwenPawDataTraceMiddleware(MiddlewareBase):
    """Collect reasoning/acting events in session trace and active node trace."""

    def __init__(
        self,
        runtime_state: "RuntimeStateManager",
        session_trace_writer: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._rs = runtime_state
        self._session_trace_writer = session_trace_writer
        self._blocks: dict[tuple[str, str, str], list[str]] = {}
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_results: dict[str, dict[str, Any]] = {}

    async def on_reply(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        await self._append_input_trace(input_kwargs.get("inputs"))
        async for event in next_handler():
            yield event

    async def on_reasoning(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        async for event in next_handler():
            await self._maybe_append_trace(event, phase="reasoning")
            yield event

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        if tool_call is not None:
            await self._append_tool_call_trace(tool_call)
            await self._append_tool_result_start_trace(tool_call)

        async for event in next_handler():
            if tool_call is None:
                await self._maybe_append_trace(event, phase="acting")
            else:
                await self._append_tool_result_trace(tool_call, event)
            yield event

    async def _append_input_trace(self, inputs: Any) -> None:
        if inputs is None:
            return
        messages = inputs if isinstance(inputs, list) else [inputs]
        context = self._rs.trace_context()
        for msg in messages:
            payload = self._event_payload(msg)
            if payload is None:
                continue
            await self._append_session_trace(
                payload,
                phase="input",
                context=context,
            )

    async def _maybe_append_trace(self, event: Any, *, phase: str) -> None:
        payload = self._event_payload(event)
        if payload is None:
            return

        await self._append_trace_payload(payload, phase=phase)

    def _event_payload(self, event: Any) -> Any | None:
        if hasattr(event, "to_dict"):
            return event.to_dict()
        if hasattr(event, "model_dump"):
            try:
                return event.model_dump(mode="json")
            except TypeError:
                return event.model_dump()
        return None

    async def _append_trace_payload(self, payload: Any, *, phase: str) -> None:
        self._rs.append_to_trace(payload)
        context = self._rs.trace_context()
        self._record_terminal_trace(payload, phase=phase, context=context)
        await self._append_session_trace(payload, phase=phase, context=context)

    async def _append_tool_call_trace(self, tool_call: Any) -> None:
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        tool_name = str(getattr(tool_call, "name", "") or "")
        tool_input = getattr(tool_call, "input", "")
        for payload in (
            {
                "type": "ToolCallExecutionStartEvent",
                "tool_call_id": tool_call_id,
                "tool_call_name": tool_name,
                "source": "acting",
            },
            {
                "type": "ToolCallExecutionDeltaEvent",
                "tool_call_id": tool_call_id,
                "tool_call_name": tool_name,
                "delta": tool_input,
                "source": "acting",
            },
            {
                "type": "ToolCallExecutionEndEvent",
                "tool_call_id": tool_call_id,
                "tool_call_name": tool_name,
                "source": "acting",
            },
        ):
            await self._append_trace_payload(payload, phase="acting")

    async def _append_tool_result_start_trace(self, tool_call: Any) -> None:
        payload = {
            "type": "ToolResultExecutionStartEvent",
            "tool_call_id": str(getattr(tool_call, "id", "") or ""),
            "tool_call_name": str(getattr(tool_call, "name", "") or ""),
            "source": "acting",
        }
        await self._append_trace_payload(payload, phase="acting")

    async def _append_tool_result_trace(self, tool_call: Any, event: Any) -> None:
        payload = self._event_payload(event)
        if payload is None:
            return

        tool_call_id = str(getattr(tool_call, "id", "") or "")
        base = {
            "tool_call_id": tool_call_id,
            "tool_call_name": str(getattr(tool_call, "name", "") or ""),
            "source": "acting",
        }
        if isinstance(event, ToolResponse):
            trace_payload = {
                "type": "ToolResultExecutionEndEvent",
                **base,
                "state": payload.get("state"),
                "response": payload,
            }
        else:
            trace_payload = {
                "type": "ToolResultExecutionDeltaEvent",
                **base,
                "delta": _tool_result_text(payload),
                "chunk": payload,
            }
        await self._append_trace_payload(trace_payload, phase="acting")

    def _record_terminal_trace(
        self,
        payload: Any,
        *,
        phase: str,
        context: dict[str, str | None],
    ) -> None:
        if not isinstance(payload, dict):
            return

        normalized = _normalized_event_type(payload)
        if "TEXTBLOCK" in normalized:
            self._record_block_trace(
                "text",
                normalized=normalized,
                payload=payload,
                phase=phase,
                context=context,
            )
        elif "THINKINGBLOCK" in normalized:
            self._record_block_trace(
                "thinking",
                normalized=normalized,
                payload=payload,
                phase=phase,
                context=context,
            )
        elif "TOOLCALL" in normalized:
            self._record_tool_call_trace(
                normalized=normalized,
                payload=payload,
                phase=phase,
                context=context,
            )
        elif "TOOLRESULT" in normalized:
            self._record_tool_result_trace(
                normalized=normalized,
                payload=payload,
                phase=phase,
                context=context,
            )

    def _record_block_trace(
        self,
        kind: str,
        *,
        normalized: str,
        payload: dict,
        phase: str,
        context: dict[str, str | None],
    ) -> None:
        key = (
            kind,
            str(payload.get("reply_id") or ""),
            str(payload.get("block_id") or ""),
        )
        if _is_start_event(normalized):
            self._blocks[key] = []
            return
        if _is_delta_event(normalized):
            self._blocks.setdefault(key, []).append(str(payload.get("delta") or ""))
            return
        if not _is_end_event(normalized):
            return

        content = "".join(self._blocks.pop(key, []))
        logger.info(
            "QwenPaw Data trace phase=%s graph_id=%s node_id=%s %s=%s",
            phase,
            context.get("graph_id") or "-",
            context.get("node_id") or "-",
            kind,
            _format_trace_value(content),
        )

    def _record_tool_call_trace(
        self,
        *,
        normalized: str,
        payload: dict,
        phase: str,
        context: dict[str, str | None],
    ) -> None:
        tool_call_id = str(payload.get("tool_call_id") or "")
        if not tool_call_id:
            return

        if _is_start_event(normalized):
            self._tool_calls[tool_call_id] = {
                "tool": payload.get("tool_call_name") or "",
                "input_parts": [],
            }
            return
        if _is_delta_event(normalized):
            entry = self._tool_calls.setdefault(
                tool_call_id,
                {"tool": "", "input_parts": []},
            )
            entry["input_parts"].append(str(payload.get("delta") or ""))
            return
        if not _is_end_event(normalized):
            return

        entry = self._tool_calls.pop(
            tool_call_id,
            {"tool": "", "input_parts": []},
        )
        tool_input = "".join(entry["input_parts"])
        logger.info(
            "QwenPaw Data trace phase=%s graph_id=%s node_id=%s "
            "tool_call tool=%s tool_call_id=%s input=%s",
            phase,
            context.get("graph_id") or "-",
            context.get("node_id") or "-",
            _format_trace_value(entry.get("tool") or ""),
            _format_trace_value(tool_call_id),
            _format_trace_value(tool_input),
        )

    def _record_tool_result_trace(
        self,
        *,
        normalized: str,
        payload: dict,
        phase: str,
        context: dict[str, str | None],
    ) -> None:
        tool_call_id = str(payload.get("tool_call_id") or "")
        if not tool_call_id:
            return

        if _is_start_event(normalized):
            self._tool_results[tool_call_id] = {
                "tool": payload.get("tool_call_name") or "",
                "result_parts": [],
            }
            return
        if _is_delta_event(normalized):
            entry = self._tool_results.setdefault(
                tool_call_id,
                {"tool": "", "result_parts": []},
            )
            if "delta" in payload:
                entry["result_parts"].append(str(payload.get("delta") or ""))
            elif "data" in payload:
                entry["result_parts"].append(_format_trace_value(payload.get("data")))
            return
        if not _is_end_event(normalized):
            return

        entry = self._tool_results.pop(
            tool_call_id,
            {"tool": "", "result_parts": []},
        )
        tool_result = "".join(entry["result_parts"])
        logger.info(
            "QwenPaw Data trace phase=%s graph_id=%s node_id=%s "
            "tool_result tool=%s tool_call_id=%s state=%s result=%s",
            phase,
            context.get("graph_id") or "-",
            context.get("node_id") or "-",
            _format_trace_value(entry.get("tool") or ""),
            _format_trace_value(tool_call_id),
            _format_trace_value(payload.get("state") or ""),
            _format_trace_value(tool_result),
        )

    async def _append_session_trace(
        self,
        payload: Any,
        *,
        phase: str,
        context: dict[str, str | None],
    ) -> None:
        if self._session_trace_writer is None:
            return

        entry = {
            "phase": phase,
            **context,
            "event": payload,
        }
        try:
            await self._session_trace_writer(entry)
        except Exception:
            logger.warning("Failed to append QwenPaw Data session trace", exc_info=True)


class QwenPawDataSandboxContextMiddleware(MiddlewareBase):
    """在每次 tool call 前同步 sandbox context（rel_path, node_id）。"""

    def __init__(
        self,
        runtime_state: "RuntimeStateManager",
        sandbox_manager: Any,
        session_id: str,
    ) -> None:
        self._rs = runtime_state
        self._sm = sandbox_manager
        self._session_id = session_id

    async def on_acting(
        self,
        agent: "Agent",
        input_kwargs: dict,
        next_handler: Callable[..., AsyncGenerator],
    ) -> AsyncGenerator:
        tool_call = input_kwargs.get("tool_call")
        tool_name = getattr(tool_call, "name", "") if tool_call else ""
        if tool_name in _SANDBOX_CONTEXT_TOOL_NAMES:
            self._sync_context(tool_call)
        async for event in next_handler():
            yield event

    def _sync_context(self, tool_call: Any) -> None:
        graph_id = self._rs.current_graph_id
        if graph_id is None:
            rel_path = self._session_id
        else:
            node_id: str | None = None
            for n in self._rs._graph_nodes():
                if n.state == "in_progress":
                    node_id = n.id
                    break
            if node_id:
                rel_path = f"{self._session_id}/{graph_id}/{node_id}"
            else:
                rel_path = f"{self._session_id}/{graph_id}"
        try:
            self._sm.set_current_rel_path(rel_path)
        except ValueError:
            logger.warning(
                "QwenPaw Data sandbox rejected rel_path %r", rel_path, exc_info=True,
            )

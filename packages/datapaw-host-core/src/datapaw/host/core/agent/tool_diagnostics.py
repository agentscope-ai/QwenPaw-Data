"""Diagnostics for AgentScope tool call state."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any

from agentscope.message import ToolCallState

logger = logging.getLogger(__name__)

_LOG_INPUT_LIMIT = 4000
_LOG_CONTENT_PREVIEW_LIMIT = 240


def log_awaiting_tool_calls(
    *,
    agent_name: str,
    state: Any,
    source: str,
) -> None:
    awaiting = _awaiting_tool_call_log_entries(
        agent_name=agent_name,
        state=state,
    )
    if not awaiting:
        return

    logger.error(
        "DataPaw agent is awaiting tool confirmation/external results "
        "during %s; pending_tools=%s",
        source,
        awaiting,
        exc_info=sys.exc_info()[0] is not None,
    )


def _awaiting_tool_call_log_entries(
    *,
    agent_name: str,
    state: Any,
) -> list[dict[str, str]]:
    context = getattr(state, "context", [])
    if not context:
        return []
    last_msg = context[-1]
    if getattr(last_msg, "role", None) != "assistant":
        return []
    if getattr(last_msg, "name", None) != agent_name:
        return []

    try:
        tool_results = last_msg.get_content_blocks("tool_result")
        tool_calls = last_msg.get_content_blocks("tool_call")
    except Exception:
        logger.debug("Failed to inspect pending tool calls", exc_info=True)
        return []

    result_ids = {str(getattr(item, "id", "") or "") for item in tool_results}
    entries: list[dict[str, str]] = []
    for tool_call in tool_calls:
        tool_state = getattr(tool_call, "state", None)
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        is_awaiting_confirmation = tool_state == ToolCallState.ASKING
        is_awaiting_external = (
            tool_state == ToolCallState.SUBMITTED
            and tool_call_id not in result_ids
        )
        if not (is_awaiting_confirmation or is_awaiting_external):
            continue
        entries.append(
            {
                "id": tool_call_id,
                "name": str(getattr(tool_call, "name", "") or ""),
                "state": _tool_call_state_text(tool_state),
                "input": _summarize_tool_call_input(
                    getattr(tool_call, "input", ""),
                ),
            },
        )
    return entries


def _tool_call_state_text(state: Any) -> str:
    return str(getattr(state, "value", state) or "")


def _truncate_log_text(text: str, limit: int = _LOG_INPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


def _summarize_tool_call_input(raw_input: Any) -> str:
    if not isinstance(raw_input, str):
        return _truncate_log_text(str(raw_input))

    if not raw_input.strip():
        return ""

    try:
        payload = json.loads(raw_input)
    except json.JSONDecodeError:
        return _truncate_log_text(raw_input)

    if not isinstance(payload, dict):
        return _truncate_log_text(json.dumps(payload, ensure_ascii=False, default=str))

    parts: list[str] = []
    for key in ("command", "description", "file_path", "path", "sql", "query"):
        if key in payload and payload[key] is not None:
            parts.append(f"{key}={_truncate_log_text(str(payload[key]))}")

    if "content" in payload:
        content = str(payload.get("content") or "")
        parts.append(
            "content_length="
            f"{len(content)} content_preview="
            f"{_truncate_log_text(content, _LOG_CONTENT_PREVIEW_LIMIT)}",
        )

    if parts:
        return " ".join(parts)

    return _truncate_log_text(json.dumps(payload, ensure_ascii=False, default=str))

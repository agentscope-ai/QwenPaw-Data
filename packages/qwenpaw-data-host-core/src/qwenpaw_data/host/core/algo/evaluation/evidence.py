# -*- coding: utf-8 -*-
"""Project one delimited span and its chain blocks into grading evidence."""

from __future__ import annotations

import json
import re
from typing import Any

from qwenpaw_data.host.core.algo.utils.formatting import (
    cap_middle,
    compact,
    output_text,
    truncate,
)

# Truncation budgets for the session record.
USER_LINE_CHAR_CAP = 800
TASK_DESC_CHAR_CAP = 800
TEXT_CHAR_CAP = 1_500
THINKING_CHAR_CAP = 400
HINT_CHAR_CAP = 800
TOOL_LINE_CHAR_CAP = 4_000
RECORD_CHAR_CAP = 60_000
RECORD_TRUNCATION_MARK = "\n\n...[record truncated for length: middle omitted]\n\n"
CONCLUSION_CHAR_CAP = 300
_MARKUP_TAG = re.compile(r"<[^>]+>")

# Context Manager data tools; MCP names arrive as ``mcp__context-manager__<fn>``.
_EXECUTE_READ = "execute_read"
_QUERY_GRAPH = "query_graph"
# What a preview-capped result says about the whole: it must outlive the line cap.
_RESULT_SUMMARY_KEYS = ("total_row_count", "row_count", "truncated", "file_path")


def build_evidence(
    blocks: list[dict[str, Any]] | None = None,
    *,
    plan: list[dict[str, Any]] | None = None,
    subtask: str | None = None,
    completed: list[str] | None = None,
    user_inputs: list[str] | None = None,
    priors: list[tuple[int, str]] | None = None,
) -> str:
    """Project one delimited window of chain blocks into the judge's record."""
    return _render_record(
        blocks or [],
        plan,
        subtask,
        completed or [],
        user_inputs or [],
        priors,
    )


def _render_record(
    blocks: list[dict[str, Any]],
    plan: list[dict[str, Any]] | None,
    subtask: str | None,
    completed: list[str],
    user_inputs: list[str],
    priors: list[tuple[int, str]] | None,
) -> str:
    """The user's asks, the plan context, prior conclusions, then events."""
    lines: list[str] = []
    user_lines = _render_user_inputs(user_inputs)
    if user_lines:
        lines.append("【用户需求】")
        lines.extend(user_lines)
    task_lines = _render_plan_tasks(plan, subtask, completed)
    if task_lines:
        lines.append("【计划上下文】")
        lines.extend(task_lines)
    prior_lines = _render_priors(priors)
    if prior_lines:
        lines.append("【前段结论】")
        lines.extend(prior_lines)
    body: list[str] = []
    for block in blocks:
        line = _render_block(block)
        if line is not None:
            body.append(line)
    if body:
        lines.append("【本段记录】")
        lines.extend(body)
    if not lines:
        return "（本片段无会话记录）"
    return cap_middle("\n".join(lines), RECORD_CHAR_CAP, RECORD_TRUNCATION_MARK)


def _render_user_inputs(user_inputs: list[str]) -> list[str]:
    """The typed input chain, one line per ask, in time order."""
    lines: list[str] = []
    for text in user_inputs:
        flat = " ".join(text.split())
        if flat:
            lines.append(f"用户：{truncate(flat, USER_LINE_CHAR_CAP)}")
    return lines


def _render_plan_tasks(
    plan: list[dict[str, Any]] | None,
    subtask: str | None,
    completed: list[str],
) -> list[str]:
    """Done tasks as one-liners, then the running task with its description."""
    if not plan:
        return []
    lines: list[str] = []
    lines.extend(
        f"- 已完成：{_task_label(task)}"
        for task in plan
        if task.get("id") in completed and _task_label(task)
    )
    running = next(
        (task for task in plan if subtask and task.get("id") == subtask), None
    )
    if running is None:
        candidates = [
            task for task in plan if str(task.get("state") or "") == "in_progress"
        ]
        if len(candidates) > 1:
            labels = "；".join(
                label for task in candidates if (label := _task_label(task))
            )
            if labels:
                lines.append(f"- 正在执行（多个子任务进行中，归属有歧义）：{labels}")
            return lines
        running = candidates[0] if candidates else None
    if running is not None:
        label = _task_label(running)
        desc = " ".join(str(running.get("description") or "").split())
        if label and desc:
            lines.append(f"- 正在执行：{label} — {truncate(desc, TASK_DESC_CHAR_CAP)}")
        elif label:
            lines.append(f"- 正在执行：{label}")
    return lines


def _task_label(task: dict[str, Any]) -> str:
    return " ".join(str(task.get("subject") or "").split())


def _render_priors(priors: list[tuple[int, str]] | None) -> list[str]:
    """Earlier conclusions with their segment ordinal, markup stripped."""
    lines: list[str] = []
    for ordinal, conclusion in priors or []:
        text = _strip_markup(conclusion).strip()
        if text:
            lines.append(f"(段{ordinal}) {_one_line(text, CONCLUSION_CHAR_CAP)}")
    return lines


def _render_block(block: dict[str, Any]) -> str | None:
    """Render one in-window chain block as a single capped line."""
    seq = block.get("seq") or 0
    block_type = block.get("type") or ""
    if block_type == "thinking":
        return (
            f"- [{seq}] 思考："
            f"{_one_line(str(block.get('text') or ''), THINKING_CHAR_CAP)}"
        )
    if block_type == "hint":
        return (
            f"- [{seq}] 用户引导："
            f"{_one_line(str(block.get('text') or ''), HINT_CHAR_CAP)}"
        )
    if block_type == "tool_use":
        label = block.get("name") or "unknown_tool"
        payload = _cm_call_payload(label, block.get("input"))
        if payload is None:
            payload = _tool_payload(block.get("input"))
        return f"- [{seq}] tool_use {label}: {payload}"
    if block_type == "tool_result":
        label = block.get("name") or "unknown_tool"
        suffix = " [error]" if block.get("status") == "error" else ""
        text = output_text(block.get("output"))
        payload = _cm_result_payload(label, text)
        if payload is None:
            payload = _tool_payload(text)
        return f"- [{seq}] tool_result {label}: {payload}{suffix}"
    text = str(block.get("text") or "").strip()
    if block.get("role") == "user":
        if text:
            return f"- [{seq}] 用户：{_one_line(text, USER_LINE_CHAR_CAP)}"
        return None
    if not text:
        data = block.get("data")
        if data is None:
            return None
        return f"- [{seq}] 回复：{_tool_payload(data)}"
    return f"- [{seq}] 回复：{_one_line(text, TEXT_CHAR_CAP)}"


def _cm_tool(label: Any) -> str:
    name = str(label or "").rsplit("__", 1)[-1]
    return name if name in {_EXECUTE_READ, _QUERY_GRAPH} else ""


def _cm_call_payload(label: Any, value: Any) -> str | None:
    """The request of a CM data tool, identity fields first so a long statement
    can only cut itself short."""
    tool = _cm_tool(label)
    if not tool or not isinstance(value, dict):
        return None
    if tool == _EXECUTE_READ:
        parts = [
            f"{key}={value[key]}"
            for key in ("datasource_id", "odps_execution_project")
            if value.get(key)
        ]
        parts.append(f"sql={value.get('sql') or ''}")
    else:
        parts = [f"cypher={value.get('cypher') or ''}"]
        if value.get("params"):
            parts.append(f"params={compact(value['params'])}")
    return _one_line(" ".join(parts), TOOL_LINE_CHAR_CAP)


def _cm_result_payload(label: Any, text: str) -> str | None:
    """A CM data result with its totals ahead of the row preview.

    ``execute_read`` rows are only a preview, and the totals and the CSV path
    sit after them in the JSON; a head-only cut would drop exactly the numbers
    a reply is graded against.
    """
    tool = _cm_tool(label)
    if not tool:
        return None
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        return None
    summary = [
        f"{key}={compact(data[key])}"
        for key in _RESULT_SUMMARY_KEYS
        if data.get(key) is not None
    ]
    parts = [*summary]
    if data.get("columns"):
        parts.append(f"columns={compact(data['columns'])}")
    parts.append(f"rows(preview)={compact(data['rows'])}")
    return _one_line(" ".join(parts), TOOL_LINE_CHAR_CAP)


def _tool_payload(value: Any) -> str:
    """One capped line for any tool input/output the trace recorded."""
    text = compact(value) if not isinstance(value, str) else value
    if not text:
        return "-"
    return _one_line(text, TOOL_LINE_CHAR_CAP)


def _one_line(text: str, cap: int) -> str:
    """Fold line breaks away: the record reads one event per line."""
    return truncate(" ".join(text.split()), cap)


def _strip_markup(text: str) -> str:
    """Drop the rich-text tags conclusions are written with."""
    return _MARKUP_TAG.sub("", text or "")


__all__ = ["build_evidence"]

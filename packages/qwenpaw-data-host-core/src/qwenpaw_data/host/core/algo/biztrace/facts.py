# -*- coding: utf-8 -*-
"""Mechanical facts mined from a delimited segment window.

The extractor prompt names these so the model cannot write "关联用户表" when
the SQL already has a JOIN key, or leave ``input`` null when a table was read.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from qwenpaw_data.host.core.algo.utils.formatting import (
    FILE_PATH_KEYS,
    file_basename,
    first_str,
    is_business_file,
    output_text,
)
from qwenpaw_data.host.core.algo.biztrace.tools import (
    ASK_USER_QUESTION,
    PLAN_SUMMARY_TOOLS,
    canonical_tool_name,
    tool_family,
)

# A plan step's description is capped so one long task cannot crowd the later
# steps out of the extractor prompt. The rendered window still truncates the
# raw PlanCreate arguments; this list is how the full plan reaches the model.
_PLAN_STEP_DESC_CAP = 160

_TABLE_RE = re.compile(
    r"(?:FROM|JOIN)\s+`?([A-Za-z_]\w*)`?(?:\.`?([A-Za-z_]\w*)`?)?",
    re.IGNORECASE,
)
_JOIN_ON_RE = re.compile(
    r"ON\s+(?:`?[\w]+`?\.)?`?(\w+)`?\s*=\s*(?:`?[\w]+`?\.)?`?(\w+)`?",
    re.IGNORECASE,
)
_DATE_COL_RE = re.compile(r"\b(thedate|ds|gmt_\w+|pt)\b", re.IGNORECASE)
_AGG_RE = re.compile(
    r"\b(?:SUM|AVG|COUNT|MAX|MIN|ROUND)\s*\(\s*(?:DISTINCT\s+)?([^)]+)\)",
    re.IGNORECASE,
)
_NO_TABLE_RE = re.compile(
    r"table\s+not\s+found|table\s+does\s+not\s+exist|无表",
    re.IGNORECASE,
)
_NO_PERMISSION_RE = re.compile(
    r"odps:(?:list|select)|no\s+permission|access\s+denied|is\s+forbidden|"
    r"无权|没有权限|缺少项目",
    re.IGNORECASE,
)
_NO_COLUMN_RE = re.compile(
    r"unknown\s+column|column\s+not|field\s+not|字段不存在",
    re.IGNORECASE,
)
_BLOCKED_RE = re.compile(r"intercept|blocked|被拦截", re.IGNORECASE)
_TOOL_UNAVAILABLE_RE = re.compile(
    r"tool\s+(?:not\s+found|does\s+not\s+exist)|工具不存在|工具不可用|"
    r"mcp.{0,40}不存在",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class WindowFacts:
    """Hard facts already present in the window; the extractor must name them."""

    tables: list[str] = field(default_factory=list)
    date_columns: list[str] = field(default_factory=list)
    join_keys: list[str] = field(default_factory=list)
    metric_columns: list[str] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    answered_ask: bool = False
    failure_kinds: list[str] = field(default_factory=list)
    plan_steps: list[str] = field(default_factory=list)
    closing_reply: str = ""

    def has_input_evidence(self) -> bool:
        """Whether the window consumed a table, business file, or answered ask."""
        return bool(self.tables or self.files_read or self.answered_ask)

    def render(self, *, lang: str = "zh") -> str:
        """Render a prompt section the extractor must name, or empty if none."""
        lines: list[str] = []
        english = lang == "en"

        def add(label_zh: str, label_en: str, values: list[str]) -> None:
            if values:
                lines.append(
                    f"- {label_en if english else label_zh}: {', '.join(values)}"
                )

        add("表", "tables", self.tables)
        add("时间/分区字段", "date columns", self.date_columns)
        add("JOIN 键", "JOIN keys", self.join_keys)
        add("指标字段", "metric columns", self.metric_columns)
        add("读到的业务文件", "files read", self.files_read)
        add("写出的文件", "files written", self.files_written)
        if self.answered_ask:
            lines.append("- answered ask: yes" if english else "- 已作答的澄清提问: 是")
        add("失败类型", "failure kinds", self.failure_kinds)
        if self.plan_steps:
            lines.append("- plan steps:" if english else "- 计划步骤:")
            for index, step in enumerate(self.plan_steps, start=1):
                lines.append(f"  {index}. {step}")
        return "\n".join(lines)


def collect_facts(nodes: Iterable[Any]) -> WindowFacts:
    """Mine tables, keys, files, plan steps and the closing reply from chain nodes."""
    tables: list[str] = []
    date_columns: list[str] = []
    join_keys: list[str] = []
    metric_columns: list[str] = []
    files_read: list[str] = []
    files_written: list[str] = []
    failure_kinds: list[str] = []
    plan_steps: list[str] = []
    answered_ask = False
    closing_reply = ""

    for node in nodes:
        for block in getattr(node, "content", ()) or ():
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = str(block.get("text") or "").strip()
                if text:
                    closing_reply = text
                continue
            name = canonical_tool_name(str(block.get("name") or ""))
            family = tool_family(name)
            if block_type == "tool_use":
                payload = block.get("input")
                if family == "sql":
                    _extend(tables, _sql_tables(_sql_text(payload)))
                    _extend(date_columns, _sql_date_columns(_sql_text(payload)))
                    _extend(join_keys, _sql_join_keys(_sql_text(payload)))
                    _extend(metric_columns, _sql_metrics(_sql_text(payload)))
                elif family == "file_read":
                    path = first_str(payload, FILE_PATH_KEYS)
                    if is_business_file(path):
                        _extend(files_read, [file_basename(path)])
                elif family == "file_write":
                    path = first_str(payload, FILE_PATH_KEYS)
                    if is_business_file(path):
                        _extend(files_written, [file_basename(path)])
                elif name in PLAN_SUMMARY_TOOLS:
                    _extend(plan_steps, _plan_step_lines(name, payload))
                continue
            if block_type != "tool_result":
                continue
            if name == ASK_USER_QUESTION and _ask_answered(block.get("output")):
                answered_ask = True
            kind = _result_failure_kind(block)
            if kind:
                _extend(failure_kinds, [kind])

    return WindowFacts(
        tables=tables,
        date_columns=date_columns,
        join_keys=join_keys,
        metric_columns=metric_columns,
        files_read=files_read,
        files_written=files_written,
        answered_ask=answered_ask,
        failure_kinds=failure_kinds,
        plan_steps=plan_steps,
        closing_reply=closing_reply,
    )


def classify_sql_error(text: str) -> str:
    """Map a SQL error string onto the spec's failure kinds.

    Returns one of ``no_table``, ``no_permission``, ``no_column``,
    ``blocked``, ``tool_unavailable``, or ``other``.
    """
    if _NO_TABLE_RE.search(text):
        return "no_table"
    if _NO_PERMISSION_RE.search(text):
        return "no_permission"
    if _NO_COLUMN_RE.search(text):
        return "no_column"
    if _BLOCKED_RE.search(text):
        return "blocked"
    if _TOOL_UNAVAILABLE_RE.search(text):
        return "tool_unavailable"
    return "other"


def _plan_step_lines(name: str, payload: Any) -> list[str]:
    """One line per task in a PlanCreate, or per change in a PlanUpdate."""
    data = payload if isinstance(payload, dict) else {}
    if name == "PlanCreate":
        raw = data.get("tasks")
        build = _create_step
    elif name == "PlanUpdate":
        raw = data.get("changes")
        build = _update_step
    else:
        return []
    lines: list[str] = []
    for item in raw if isinstance(raw, list) else []:
        line = build(item)
        if line:
            lines.append(line)
    return lines


def _create_step(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    subject = _flat(str(item.get("subject") or item.get("id") or ""))
    if not subject:
        return None
    blocked = item.get("blocked_by")
    deps = (
        [_flat(str(dep)) for dep in blocked if _flat(str(dep))]
        if isinstance(blocked, list)
        else []
    )
    head = f"{subject}（依赖：{'、'.join(deps)}）" if deps else subject
    description = _clip(str(item.get("description") or ""))
    return f"{head} — {description}" if description else head


def _update_step(item: Any) -> str | None:
    """Mirror plan._apply_change: action + task_id, plus the nested task."""
    if not isinstance(item, dict):
        return None
    action = _flat(str(item.get("action") or ""))
    task_id = _flat(str(item.get("task_id") or ""))
    if not action or not task_id:
        return None
    task = item.get("task")
    subject = ""
    description = ""
    if isinstance(task, dict):
        subject = _flat(str(task.get("subject") or ""))
        description = _clip(str(task.get("description") or ""))
    line = f"{action} {task_id}: {subject}" if subject else f"{action} {task_id}"
    return f"{line} — {description}" if description else line


def _flat(text: str) -> str:
    return " ".join(text.split())


def _clip(text: str) -> str:
    flat = _flat(text)
    if len(flat) <= _PLAN_STEP_DESC_CAP:
        return flat
    return flat[: _PLAN_STEP_DESC_CAP - 1].rstrip() + "…"


def _sql_text(payload: Any) -> str:
    if isinstance(payload, dict):
        sql = payload.get("sql")
        return sql if isinstance(sql, str) else ""
    return payload if isinstance(payload, str) else ""


def _sql_tables(sql: str) -> list[str]:
    found: list[str] = []
    for match in _TABLE_RE.finditer(sql):
        project, table = match.group(1), match.group(2)
        name = f"{project}.{table}" if table else project
        if name.upper() not in {"SELECT", "WHERE", "ON"}:
            _extend(found, [name])
    return found


def _sql_date_columns(sql: str) -> list[str]:
    found: list[str] = []
    _extend(found, [match.group(1) for match in _DATE_COL_RE.finditer(sql)])
    return found


def _sql_join_keys(sql: str) -> list[str]:
    found: list[str] = []
    for match in _JOIN_ON_RE.finditer(sql):
        left, right = match.group(1), match.group(2)
        _extend(found, [left if left == right else f"{left}={right}"])
    return found


def _sql_metrics(sql: str) -> list[str]:
    found: list[str] = []
    for match in _AGG_RE.finditer(sql):
        expr = re.sub(r"\s+", " ", match.group(1)).strip(" `")
        if expr and expr != "*":
            _extend(found, [expr])
    return found


def _ask_answered(output: Any) -> bool:
    payload = _as_dict(output)
    if payload is None:
        return bool(output_text(output).strip())
    answers = payload.get("answers")
    return isinstance(answers, list) and bool(answers)


def _result_failure_kind(block: dict[str, Any]) -> str | None:
    output = block.get("output")
    payload = _as_dict(output)
    error = ""
    if isinstance(payload, dict):
        raw = payload.get("error")
        if isinstance(raw, str) and raw.strip():
            error = raw
    text = error or output_text(output)
    if block.get("status") == "error" or error:
        return classify_sql_error(text) if text else "other"
    if text and _looks_failed(text):
        return classify_sql_error(text)
    return None


def _looks_failed(text: str) -> bool:
    return bool(
        _NO_TABLE_RE.search(text)
        or _NO_PERMISSION_RE.search(text)
        or _NO_COLUMN_RE.search(text)
        or _BLOCKED_RE.search(text)
        or _TOOL_UNAVAILABLE_RE.search(text)
    )


def _as_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                return None
            return parsed if isinstance(parsed, dict) else None
    return None


def _extend(dest: list[str], values: Iterable[str]) -> None:
    seen = {item.casefold() for item in dest}
    for value in values:
        text = value.strip()
        key = text.casefold()
        if text and key not in seen:
            dest.append(text)
            seen.add(key)


__all__ = [
    "WindowFacts",
    "classify_sql_error",
    "collect_facts",
]

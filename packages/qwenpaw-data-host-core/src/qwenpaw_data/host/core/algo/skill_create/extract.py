# -*- coding: utf-8 -*-
"""Deterministic trace → digest (blur extract_trace, QwenPaw tool names)."""

from __future__ import annotations

import json
import re
from collections import Counter, OrderedDict
from typing import Any

ERROR_PATTERNS = re.compile(
    r"(Error executing tool|\"exec_status\"\s*:\s*\"error\"|FAILED:|ODPS-\d+|"
    r"Traceback \(most recent call last\)|SemanticException|command not found)"
)
DELIVER_TOOLS = {
    "send_file_to_user",
    "write_file",
    "edit_file",
    "Write",
    "Edit",
}
READ_SKILL_TOOLS = {"read_file", "Read", "read"}
SQL_TOOLS = frozenset({"execute_read", "execute_modify", "execute_create"})
GRAPH_QUERY_TOOL = "query_graph"
# Node labels only (CamelCase inside parentheses); relationship types are
# UPPER_SNAKE and never match, and no business literal can leak through a label.
GRAPH_LABEL_RE = re.compile(r"\(\s*\w*\s*:\s*([A-Z][A-Za-z]*)")
USER_TURN_RULES = [
    (
        "疑似缺陷反馈",
        re.compile(
            r"(没有成功|不成功|没显示|没有显示|失败|报错|错误|不对|有问题|异常|乱码|空白)"
        ),
    ),
    (
        "疑似追问澄清",
        re.compile(
            r"(是什么|什么意思|为什么|怎么算|怎么样|如何|哪些|吗[？?]?$|？$|\?$)"
        ),
    ),
    (
        "疑似新增子任务",
        re.compile(
            r"(生成|导出|输出|再帮我|接着|继续下拆|下拆|加在|新增|画|做一个|出一个)"
        ),
    ),
    (
        "疑似口径修正",
        re.compile(r"(应该|要用|改成|按照|口径|注意|必须|只看|不要|不能|需要看到)"),
    ),
]


def blocks_of(msg: dict[str, Any]) -> list[dict[str, Any]]:
    content = msg.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


def text_of(msg: dict[str, Any], kinds: tuple[str, ...] = ("text",)) -> str:
    out = []
    for block in blocks_of(msg):
        if block.get("type") in kinds:
            out.append(block.get("text") or block.get("thinking") or "")
    return "\n".join(t for t in out if t).strip()


def result_text(block: dict[str, Any]) -> str:
    out = block.get("output")
    if isinstance(out, str):
        return out
    if isinstance(out, list):
        parts = []
        for item in out:
            if isinstance(item, dict):
                parts.append(item.get("text") or "")
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if out is None:
        return ""
    return json.dumps(out, ensure_ascii=False)


def inner_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def clip(text: str, limit: int, tail: int = 0) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    if tail:
        head = limit - tail
        return (
            text[:head]
            + "\n... [截断 %d 字符] ...\n" % (len(text) - limit)
            + text[-tail:]
        )
    return text[:limit] + " ... [截断 %d 字符]" % (len(text) - limit)


def classify_user_turn(text: str) -> list[str]:
    labels = [name for name, pat in USER_TURN_RULES if pat.search(text or "")]
    return labels or ["疑似口径修正"]


def compress_runs(names: list[str]) -> str:
    runs: list[list[Any]] = []
    for name in names:
        if runs and runs[-1][0] == name:
            runs[-1][1] += 1
        else:
            runs.append([name, 1])
    return " → ".join(n if c == 1 else "%s×%d" % (n, c) for n, c in runs)


def first_present(payload: dict[str, Any], *keys: str) -> Any:
    """First non-null value among keys: CM reports ``total_row_count`` for a
    preview-capped read, older servers only ``row_count`` / ``preview_row_count``."""
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return None


def raw_tool_name(name: str) -> str:
    return name.rsplit("__", 1)[-1]


def graph_query_step(name: str, args: dict[str, Any]) -> str:
    """Skeleton label for a graph lookup: which kinds of node it walked.

    The Cypher itself names concrete metrics and datasets, which are business
    facts; the labels (``Metric>MetricCaliber``) are the reusable query shape.
    """
    if raw_tool_name(name) != GRAPH_QUERY_TOOL:
        return name
    labels = list(dict.fromkeys(GRAPH_LABEL_RE.findall(str(args.get("cypher") or ""))))
    return "%s[%s]" % (name, ">".join(labels)) if labels else name


def is_sql_tool(name: str) -> bool:
    return raw_tool_name(name) in SQL_TOOLS


def is_skill_consult(name: str, args: dict[str, Any]) -> str:
    if name in {"Skill", "skill"}:
        skill = str(args.get("skill") or args.get("name") or "").strip()
        return f"skills/{skill}/SKILL.md" if skill else ""
    if name in READ_SKILL_TOOLS:
        path = str(args.get("file_path") or args.get("path") or "")
        if "skill" in path.lower():
            return path
    return ""


def extract(
    messages: list[dict[str, Any]],
    *,
    task_instruction: str = "",
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "turn_count": len(messages),
        "role_counts": Counter(),
        "task_instruction": "",
        "user_turns": [],
        "skills_consulted": OrderedDict(),
        "tool_profile": Counter(),
        "tool_order": [],
        "segments": [],
        "sql_success": [],
        "sql_failed": [],
        "errors": OrderedDict(),
        "deliverables": OrderedDict(),
        "subagent_tasks": [],
        "final_summary": "",
    }
    calls: dict[Any, tuple[str, dict[str, Any]]] = {}
    seg: dict[str, Any] | None = None
    # §1 is this text when the caller already knows the first chat's first
    # user input. A later chat's last user message must not take that slot
    # just because it appears first in an unsorted window.
    pinned = task_instruction.strip()
    pinned_seen = False

    def open_segment(idx: int, text: str, labels: list[str]) -> dict[str, Any]:
        return {"start_turn": idx, "user_text": text, "labels": labels, "tools": []}

    def close_segment() -> None:
        nonlocal seg
        if seg:
            ev["segments"].append(seg)
            seg = None

    for idx, msg in enumerate(messages):
        role = msg.get("role") or msg.get("name")
        ev["role_counts"][role] += 1
        if role == "user":
            text = text_of(msg)
            if not text:
                continue
            if pinned:
                is_initial = not pinned_seen and text == pinned
            else:
                is_initial = not ev["task_instruction"]
            if is_initial:
                pinned_seen = True
                ev["task_instruction"] = pinned or text
                close_segment()
                seg = open_segment(idx, "[初始任务]", ["初始任务"])
            else:
                labels = classify_user_turn(text)
                ev["user_turns"].append({"turn": idx, "labels": labels, "text": text})
                close_segment()
                seg = open_segment(idx, text, labels)
            continue

        for block in blocks_of(msg):
            btype = block.get("type")
            if btype == "tool_use":
                name = block.get("name") or "?"
                args = (
                    block.get("input") if isinstance(block.get("input"), dict) else {}
                )
                calls[block.get("id")] = (name, args)
                ev["tool_profile"][name] += 1
                ev["tool_order"].append(name)
                if seg:
                    seg["tools"].append(graph_query_step(name, args))
                skill_path = is_skill_consult(name, args)
                if skill_path:
                    ev["skills_consulted"][skill_path] = (
                        ev["skills_consulted"].get(skill_path, 0) + 1
                    )
                elif name in DELIVER_TOOLS:
                    path = str(args.get("file_path") or args.get("path") or "")
                    if path:
                        ev["deliverables"].setdefault(path, set()).add(name)
            elif btype == "tool_result":
                name = block.get("name") or calls.get(block.get("id"), ("?", {}))[0]
                args = calls.get(block.get("id"), (name, {}))[1]
                text = result_text(block)
                failed = bool(ERROR_PATTERNS.search(text)) or str(
                    block.get("state") or ""
                ).lower() in {"error", "failed"}
                if is_sql_tool(name):
                    sql = str(args.get("sql") or "")
                    parsed = inner_json(text) or {}
                    if not sql:
                        sql = str(parsed.get("sql") or "")
                    if failed or parsed.get("exec_status") == "error":
                        ev["sql_failed"].append(
                            {
                                "sql": sql,
                                "error": clip(str(parsed.get("error") or text), 400),
                            }
                        )
                    else:
                        ev["sql_success"].append(
                            {
                                "sql": sql,
                                "row_count": first_present(
                                    parsed,
                                    "total_row_count",
                                    "row_count",
                                    "preview_row_count",
                                ),
                                "columns": parsed.get("columns") or [],
                            }
                        )
                if failed:
                    parsed = inner_json(text) or {}
                    detail = str(parsed.get("error") or text).strip().splitlines()
                    head = detail[0] if detail else ""
                    key = "%s :: %s" % (name, clip(head, 160))
                    ev["errors"][key] = ev["errors"].get(key, 0) + 1

    if seg:
        ev["segments"].append(seg)
    if pinned and not ev["task_instruction"]:
        ev["task_instruction"] = pinned
    for msg in reversed(messages):
        if (msg.get("role") or msg.get("name")) == "assistant":
            text = text_of(msg)
            if text:
                ev["final_summary"] = text
                break
    return ev


def render(ev: dict[str, Any], budget: int) -> str:
    lines: list[str] = []
    add = lines.append
    add("# Trace 证据摘要")
    add("")
    add("> 本摘要即全部可用证据，原始 trace 不可访问。")
    add("")
    add(
        "**规模**：%d 轮 ｜ %d 次工具调用 ｜ %d 次用户追加 ｜ %d 次失败调用"
        % (
            ev["turn_count"],
            sum(ev["tool_profile"].values()),
            len(ev["user_turns"]),
            sum(ev["errors"].values()),
        )
    )
    add("")
    add("## 1. 初始任务指令（技能要解决的问题）")
    add("")
    add("```text")
    add(clip(ev["task_instruction"], int(budget * 0.12)) or "(缺失)")
    add("```")
    add("")
    add("## 2. 用户后续轮次（口径修正与边界条件的最高信号）")
    add("")
    if not ev["user_turns"]:
        add("_无后续用户轮次：任务一轮完成，边界条件需在访谈阶段补齐。_")
    else:
        add("共 %d 轮。标签为启发式判定，请按原文自行确认。" % len(ev["user_turns"]))
        add("")
        per = max(200, int(budget * 0.22 / max(1, len(ev["user_turns"]))))
        for item in ev["user_turns"]:
            add(
                "- **[轮次 %d｜%s]** %s"
                % (
                    item["turn"],
                    "/".join(item["labels"]),
                    clip(item["text"], per).replace("\n", " ⏎ "),
                )
            )
    add("")
    add("## 3. 已查阅的既有技能（命名避让依据）")
    add("")
    if not ev["skills_consulted"]:
        add("_未查阅任何既有技能。_")
    else:
        add("| 技能文件 | 次数 |")
        add("| --- | --- |")
        for path, count in sorted(
            ev["skills_consulted"].items(), key=lambda kv: -kv[1]
        ):
            add("| `%s` | %d |" % (path, count))
    add("")
    add("## 4. 工作流骨架（按用户轮次分段）")
    add("")
    for seg in ev["segments"][:12]:
        add(
            "- **轮次 %d｜%s**：%s"
            % (
                seg["start_turn"],
                "/".join(seg["labels"]),
                clip(seg["user_text"], 90).replace("\n", " ⏎ "),
            )
        )
        add("  - 工具：`%s`" % (compress_runs(seg["tools"])[:400] or "无"))
    add("")
    add("## 5. SQL / 执行产物")
    add("")
    add("成功 %d 条，失败 %d 条。" % (len(ev["sql_success"]), len(ev["sql_failed"])))
    add("")
    if ev["sql_success"]:
        picks = sorted(ev["sql_success"], key=lambda s: -len(s["sql"]))[:3]
        add("### 5.1 关键成功 SQL")
        add("")
        per = max(400, int(budget * 0.20 / max(1, len(picks))))
        for i, item in enumerate(picks, 1):
            add(
                "**成功 SQL %d**（列：%s）"
                % (i, ", ".join(map(str, item["columns"]))[:160] or "-")
            )
            add("")
            add("```sql")
            add(clip(item["sql"], per, tail=int(per * 0.3)))
            add("```")
            add("")
    if ev["sql_failed"]:
        add("### 5.2 失败 SQL 与报错")
        add("")
        shown = ev["sql_failed"][:4]
        per = max(200, int(budget * 0.12 / max(1, len(shown))))
        for i, item in enumerate(shown, 1):
            add("- **失败 %d**：`%s`" % (i, clip(item["sql"].replace("\n", " "), per)))
            add("  - 报错：%s" % clip(item["error"].replace("\n", " "), 240))
        add("")
    add("## 6. 局部失败（仅作 pitfall 候选，不是技能主题）")
    add("")
    if not ev["errors"]:
        add("_无失败调用。_")
    else:
        add("| 次数 | 工具 :: 报错首行 |")
        add("| --- | --- |")
        for key, count in sorted(ev["errors"].items(), key=lambda kv: -kv[1])[:8]:
            add("| %d | %s |" % (count, key.replace("|", "\\|")))
    add("")
    add("## 7. 产物与交付")
    add("")
    if not ev["deliverables"]:
        add("_无落盘产物。_")
    else:
        for path, kinds in list(ev["deliverables"].items())[:25]:
            add("- `%s` （%s）" % (path, "/".join(sorted(kinds))))
    add("")
    add("## 8. 最终结论/交付说明（末次 assistant 输出）")
    add("")
    add("```text")
    add(clip(ev["final_summary"], int(budget * 0.15)) or "(缺失)")
    add("```")
    return "\n".join(lines)


def serialize_evidence(ev: dict[str, Any]) -> dict[str, Any]:
    payload = dict(ev)
    payload["role_counts"] = dict(ev["role_counts"])
    payload["tool_profile"] = dict(ev["tool_profile"])
    payload["skills_consulted"] = dict(ev["skills_consulted"])
    payload["errors"] = dict(ev["errors"])
    payload["deliverables"] = {
        key: sorted(value) for key, value in ev["deliverables"].items()
    }
    return payload

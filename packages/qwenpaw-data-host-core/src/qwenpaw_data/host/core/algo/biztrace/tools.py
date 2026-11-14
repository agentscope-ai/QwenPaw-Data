# -*- coding: utf-8 -*-
"""Canonical tool names shared by presentation, segmentation and artifacts.

The agent registers AgentScope's built-in tools (``Read`` / ``Write`` / ``Edit``
/ ``Bash`` / ``Glob`` / ``Grep``) plus MCP tools, while both PRDs speak in terms
of ``read_file`` / ``write_file`` / ``edit_file``. Plan tools keep the host's
exact names (``PlanCreate`` / ``PlanUpdate`` / ``TaskStateUpdate``). Everything
downstream keys off the canonical name so a rename on either side stays local
to this module.
"""

from __future__ import annotations

from typing import Literal

ToolFamily = Literal[
    "sql",
    "semantic",
    "file_read",
    "file_write",
    "shell",
    "listing",
    "ask",
    "plan",
    "subtask",
    "skill",
    "spawn_subagent",
    "other",
]

CANONICAL_TOOL_NAMES: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "notebookedit": "edit_file",
    "bash": "execute_shell_command",
    "glob": "glob",
    "grep": "grep",
    "ls": "ls",
    # Host plan tools already use their public names; normalize case only.
    "plancreate": "PlanCreate",
    "planupdate": "PlanUpdate",
    "taskstateupdate": "TaskStateUpdate",
}

ASK_USER_QUESTION = "ask_user_question"
SPAWN_SUBAGENT = "spawn_subagent"
# AgentScope's builtin skill viewer; reads a skill by name, not by path.
SKILL_VIEWER = "Skill"

ORCHESTRATION_TOOLS = frozenset({"PlanCreate", "PlanUpdate", "TaskStateUpdate"})
PLAN_SUMMARY_TOOLS = frozenset({"PlanCreate", "PlanUpdate"})

# Result cards for these say nothing new beyond "the call landed".
NOTE_ONLY_RESULT_TOOLS = frozenset({"TaskStateUpdate"})
# CM's SQL surface is read / modify / create. All three carry SQL text and a
# required ``datasource_id``, so all three get the SQL family's fence,
# failure_kind and table mining.
SQL_TOOLS = frozenset({"execute_read", "execute_modify", "execute_create"})
# CM's graph surface: the schema read and model-authored read-only Cypher. Both
# are business-context lookups, so they share the semantic family's cards.
GRAPH_SCHEMA_TOOL = "get_graph_schema"
QUERY_GRAPH_TOOL = "query_graph"
SEMANTIC_TOOLS = frozenset({GRAPH_SCHEMA_TOOL, QUERY_GRAPH_TOOL})
FILE_READ_TOOLS = frozenset({"read_file"})
FILE_WRITE_TOOLS = frozenset({"write_file", "edit_file"})
SHELL_TOOLS = frozenset({"execute_shell_command"})
LISTING_TOOLS = frozenset({"glob", "grep", "ls"})


# One line on what a tool is for, so a card prompt carrying only the tool name
# still knows what the call was aiming at. Wording follows each tool's own
# registered description. Only tools whose cards reach the LLM are listed;
# rule-only cards (listing, ask, plan, sub-task, skill) never read this.
TOOL_PURPOSES: dict[str, dict[str, str]] = {
    "zh": {
        "execute_read": (
            "在指定数据源（datasource_id）上执行只读 SQL 查询并返回结果，不做任何数据变更。"
        ),
        "execute_modify": (
            "在指定数据源上执行一条 INSERT 或 UPDATE 并立即提交，写入不可回滚。"
        ),
        "execute_create": (
            "在指定数据源上执行一条 CREATE TABLE / VIEW（含 CTAS）并立即提交。"
        ),
        "get_graph_schema": "读取业务图谱的逻辑结构：节点类型、属性与关系。",
        "query_graph": (
            "用只读 Cypher 检索业务图谱：指标与口径、数据集与字段、表与列、"
            "维度取值、已验证查询、知识事件与用户记忆。"
        ),
        "write_file": "新建或覆盖写入工作区文件。",
        "edit_file": "就地修改工作区文件的局部内容。",
        "execute_shell_command": "在工作区执行 shell 命令或脚本。",
    },
    "en": {
        "execute_read": (
            "Runs a read-only SQL query on the given datasource (datasource_id); "
            "never mutates data."
        ),
        "execute_modify": (
            "Runs one INSERT or UPDATE on the given datasource and commits "
            "immediately; the write cannot be rolled back."
        ),
        "execute_create": (
            "Runs one CREATE TABLE / VIEW (including CTAS) on the given "
            "datasource and commits immediately."
        ),
        "get_graph_schema": (
            "Reads the business graph's logical structure: node types, "
            "properties and relationships."
        ),
        "query_graph": (
            "Searches the business graph with read-only Cypher: metrics and "
            "definitions, datasets and fields, tables and columns, dimension "
            "values, verified queries, knowledge events and user memories."
        ),
        "write_file": "Creates or overwrites a workspace file.",
        "edit_file": "Edits part of an existing workspace file in place.",
        "execute_shell_command": "Runs a shell command or script in the workspace.",
    },
}


def canonical_tool_name(tool_name: str | None) -> str:
    """Normalize a registered tool name to the name the PRDs / host use."""
    if not tool_name:
        return ""
    raw = tool_name.rsplit("__", 1)[-1].strip()
    return CANONICAL_TOOL_NAMES.get(raw.lower(), raw)


def tool_purpose(tool_name: str | None, lang: str = "zh") -> str:
    """One line on what a tool is for, empty when the tool is not registered."""
    table = TOOL_PURPOSES["en" if lang == "en" else "zh"]
    return table.get(canonical_tool_name(tool_name), "")


def tool_family(tool_name: str | None) -> ToolFamily:
    """Classify a tool into the extraction family the spec defines."""
    name = canonical_tool_name(tool_name)
    if name in SQL_TOOLS:
        return "sql"
    if name in SEMANTIC_TOOLS:
        return "semantic"
    if name in FILE_READ_TOOLS:
        return "file_read"
    if name in FILE_WRITE_TOOLS:
        return "file_write"
    if name in SHELL_TOOLS:
        return "shell"
    if name in LISTING_TOOLS:
        return "listing"
    if name == ASK_USER_QUESTION:
        return "ask"
    if name in PLAN_SUMMARY_TOOLS:
        return "plan"
    if name == "TaskStateUpdate":
        return "subtask"
    if name == SKILL_VIEWER:
        return "skill"
    if name == SPAWN_SUBAGENT:
        return "spawn_subagent"
    return "other"


__all__ = [
    "ASK_USER_QUESTION",
    "CANONICAL_TOOL_NAMES",
    "FILE_READ_TOOLS",
    "FILE_WRITE_TOOLS",
    "GRAPH_SCHEMA_TOOL",
    "LISTING_TOOLS",
    "NOTE_ONLY_RESULT_TOOLS",
    "ORCHESTRATION_TOOLS",
    "PLAN_SUMMARY_TOOLS",
    "QUERY_GRAPH_TOOL",
    "SEMANTIC_TOOLS",
    "SHELL_TOOLS",
    "SKILL_VIEWER",
    "SPAWN_SUBAGENT",
    "SQL_TOOLS",
    "TOOL_PURPOSES",
    "ToolFamily",
    "canonical_tool_name",
    "tool_family",
    "tool_purpose",
]

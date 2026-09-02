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

CANONICAL_TOOL_NAMES: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "notebookedit": "edit_file",
    "bash": "execute_shell_command",
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


def canonical_tool_name(tool_name: str | None) -> str:
    """Normalize a registered tool name to the name the PRDs / host use."""
    if not tool_name:
        return ""
    raw = tool_name.rsplit("__", 1)[-1].strip()
    return CANONICAL_TOOL_NAMES.get(raw.lower(), raw)


__all__ = [
    "ASK_USER_QUESTION",
    "CANONICAL_TOOL_NAMES",
    "NOTE_ONLY_RESULT_TOOLS",
    "ORCHESTRATION_TOOLS",
    "PLAN_SUMMARY_TOOLS",
    "SKILL_VIEWER",
    "SPAWN_SUBAGENT",
    "canonical_tool_name",
]

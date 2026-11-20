# -*- coding: utf-8 -*-
"""Authoritative skills_used from Skill / Read tool events in raw messages."""

from __future__ import annotations

import json
import re
from typing import Any

# Same idea as followup/collector: Skill(skill=…) and Read of skills/<name>/SKILL.md.
_SKILL_PATH_RE = re.compile(r"(?:^|/)skills/(?:.*/)?([^/]+)/SKILL\.md$")
_PATH_KEYS = ("file_path", "path", "target_file", "output_path", "filename")
_CANONICAL_TOOL_NAMES: dict[str, str] = {
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "multiedit": "edit_file",
    "skill": "Skill",
}


def extract_skills_used(messages: list[dict[str, Any]]) -> list[str]:
    """Return ordered unique skill names from completed Skill/Read plugin_calls."""
    found: list[str] = []
    seen: set[str] = set()

    def add(name: str) -> None:
        n = name.strip()
        if not n or n in seen:
            return
        seen.add(n)
        found.append(n)

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("type") or "") != "plugin_call":
            continue
        status = str(msg.get("status") or "completed")
        if status not in ("completed", "complete", ""):
            continue
        name, arguments = _plugin_call_args(msg)
        canonical = _canonical_tool_name(name)
        if canonical == "Skill":
            skill = str(arguments.get("skill") or "").strip()
            if skill:
                add(skill)
        elif canonical in ("read_file", "Read"):
            path = _first_str(arguments, _PATH_KEYS)
            match = _SKILL_PATH_RE.search(path.replace("\\", "/"))
            if match is not None:
                add(match.group(1))
    return found


def _canonical_tool_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    # MCP-style: mcp__context-manager__Skill → Skill
    if raw.startswith("mcp__") and "__" in raw[5:]:
        raw = raw.rsplit("__", 1)[-1]
    return _CANONICAL_TOOL_NAMES.get(raw.lower(), raw)


def _plugin_call_args(msg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    for block in msg.get("content") or []:
        if not isinstance(block, dict) or block.get("type") != "data":
            continue
        data = block.get("data")
        if not isinstance(data, dict):
            continue
        name = str(data.get("name") or "")
        raw_args = data.get("arguments")
        arguments: dict[str, Any]
        if isinstance(raw_args, dict):
            arguments = raw_args
        elif isinstance(raw_args, str) and raw_args.strip():
            try:
                parsed = json.loads(raw_args)
            except json.JSONDecodeError:
                parsed = {}
            arguments = parsed if isinstance(parsed, dict) else {}
        else:
            arguments = {}
        return name, arguments
    return "", {}


def _first_str(arguments: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        val = arguments.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


__all__ = ["extract_skills_used"]

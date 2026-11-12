# -*- coding: utf-8 -*-
"""Skill draft file helpers: payload vs workspace sidecars."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

PAYLOAD_ROOTS = frozenset({"scripts", "references"})
FLAG_RE = re.compile(r"--[A-Za-z][\w-]*")
PAYLOAD_PATH_RE = re.compile(r"(?:scripts|references)/[A-Za-z0-9_./-]+\.(?:py|md)")
SIDECAR_KEYS = frozenset(
    {
        "skill-card.md",
        "decision.json",
        "report.md",
        "evidence/digest.md",
        "evidence/digest.json",
    }
)


def _relpath(relative_path: str) -> str:
    raw = (relative_path or "").strip().replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if not raw or raw.startswith("/") or raw.endswith("/") or ".." in parts:
        raise ValueError(f"invalid skill relative path: {relative_path!r}")
    return "/".join(parts)


def is_skill_payload_path(relative_path: str) -> bool:
    try:
        rel = _relpath(relative_path)
    except ValueError:
        return False
    if rel == "SKILL.md":
        return True
    parts = PurePosixPath(rel).parts
    return bool(parts) and parts[0] in PAYLOAD_ROOTS and len(parts) > 1


def skill_payload_files(files: dict[str, str]) -> dict[str, str]:
    """Keep only SKILL.md plus scripts/ and references/ for install."""
    return {
        key: value
        for key, value in files.items()
        if isinstance(value, str) and is_skill_payload_path(key)
    }


def sidecar_files(files: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in files.items()
        if key in SIDECAR_KEYS and isinstance(value, str)
    }


def excerpt_for_path(skill_md: str, path: str) -> str:
    """Return SKILL.md sections that mention this payload file."""
    if not skill_md or not path:
        return ""
    name = PurePosixPath(path).name
    lines = skill_md.splitlines()
    blocks: list[str] = []
    used: set[tuple[int, int]] = set()
    for index, line in enumerate(lines):
        if name not in line and path not in line:
            continue
        start = index
        for cursor in range(index, -1, -1):
            if lines[cursor].startswith("#"):
                start = cursor
                break
        end = len(lines)
        for cursor in range(index + 1, len(lines)):
            if lines[cursor].startswith("#"):
                end = cursor
                break
        span = (start, end)
        if span in used:
            continue
        used.add(span)
        blocks.append("\n".join(lines[start:end]))
    return "\n\n".join(blocks)


def flags_in_text(text: str) -> list[str]:
    return FLAG_RE.findall(text or "")


def cited_payload_paths(skill_md: str) -> set[str]:
    return set(PAYLOAD_PATH_RE.findall(skill_md or ""))

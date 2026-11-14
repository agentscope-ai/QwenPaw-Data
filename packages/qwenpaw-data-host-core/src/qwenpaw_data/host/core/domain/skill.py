# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SkillSource = Literal["builtin", "custom"]


class SkillError(Exception):
    """Base error for skill domain operations."""


class SkillConflictError(RuntimeError, SkillError):
    """Skill name or directory already exists."""

    def __init__(self, message: str) -> None:
        super().__init__(f"CONFLICT: {message}")


class SkillNotFoundError(LookupError, SkillError):
    """Skill is missing from the agent workspace."""


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    src_dir: Path
    group: str
    description: str = ""


@dataclass(frozen=True)
class SkillFile:
    """One text file inside a skill directory, relative to the skill root."""

    relative_path: str
    content: str


@dataclass(frozen=True)
class AgentSkillInfo:
    name: str
    enabled: bool
    source: SkillSource
    group: str = ""
    description: str = ""

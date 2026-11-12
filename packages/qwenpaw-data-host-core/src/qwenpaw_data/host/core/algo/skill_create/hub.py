# -*- coding: utf-8 -*-
"""Existing skills as a flat name list for collision avoidance, not a skip gate."""

from __future__ import annotations

from dataclasses import dataclass

from qwenpaw_data.host.core.domain.skill import AgentSkillInfo
from qwenpaw_data.host.core.utils.skill import discover_builtin_skills


@dataclass(frozen=True)
class SkillRef:
    name: str
    description: str


MAX_SKILL_NAME_LENGTH = 64


def uniquify_name(name: str, taken: set[str]) -> str:
    """Return ``name`` or ``name-2`` … so it does not collide with ``taken``.

    The base is truncated before the suffix is added, so every candidate
    stays within the 64-char skill-name limit.
    """
    safe = (name or "generated-skill").strip() or "generated-skill"
    safe = _fit(safe, "")
    if safe not in taken:
        return safe
    for index in range(2, 50):
        candidate = _fit(safe, f"-{index}")
        if candidate not in taken:
            return candidate
    return _fit(safe, "-draft")


def _fit(base: str, suffix: str) -> str:
    head = base[: MAX_SKILL_NAME_LENGTH - len(suffix)].rstrip("-_.")
    return f"{head or 'skill'}{suffix}"


class SkillIndex:
    def __init__(self, skills: list[SkillRef]) -> None:
        self._skills = sorted(skills, key=lambda item: item.name)

    @classmethod
    def from_agent(
        cls,
        *,
        agent_skills: list[AgentSkillInfo] | None = None,
    ) -> SkillIndex:
        found: dict[str, SkillRef] = {}
        for skill in discover_builtin_skills():
            found[skill.name] = SkillRef(
                name=skill.name,
                description=skill.description,
            )
        for item in agent_skills or []:
            found[item.name] = SkillRef(
                name=item.name,
                description=item.description,
            )
        return cls(list(found.values()))

    def names(self) -> set[str]:
        return {skill.name for skill in self._skills}

    def inventory(self) -> str:
        lines: list[str] = []
        for skill in self._skills:
            desc = (skill.description or "").strip()
            if len(desc) > 110:
                desc = desc[:110] + "…"
            lines.append(f"- `{skill.name}`：{desc}")
        return "\n".join(lines)

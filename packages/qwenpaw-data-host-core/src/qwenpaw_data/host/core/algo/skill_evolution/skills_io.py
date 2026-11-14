# -*- coding: utf-8 -*-
"""Skill files on disk read into an in-memory catalog for evolution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from qwenpaw_data.host.core.algo.skill_evolution.models import SkillFileSnapshot
from qwenpaw_data.host.core.algo.skill_evolution.settings import MAX_SKILL_CHARS
from qwenpaw_data.host.core.domain.skill import AgentSkillInfo, SkillFile
from qwenpaw_data.host.core.utils.skill import normalize_rel_path


class SkillReadable(Protocol):
    """The read side of the skill store that evolution is allowed to use."""

    def list_skills(self) -> list[AgentSkillInfo]: ...

    def read_files(self, name: str) -> list[SkillFile]: ...


@dataclass(frozen=True)
class SkillCatalog:
    """Complete skill file snapshot loaded through ``SkillReadable``."""

    infos: tuple[AgentSkillInfo, ...]
    files: dict[tuple[str, str], SkillFileSnapshot]

    @property
    def names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.infos)

    def description(self, name: str) -> str:
        for item in self.infos:
            if item.name == name:
                return item.description
        return ""

    def get(self, skill_name: str, relative_path: str) -> SkillFileSnapshot | None:
        try:
            rel = normalize_rel_path(relative_path)
        except ValueError:
            return None
        return self.files.get((skill_name, rel))


def load_catalog(source: SkillReadable) -> SkillCatalog:
    """Read every skill file of one agent partition."""
    infos = tuple(source.list_skills())
    files: dict[tuple[str, str], SkillFileSnapshot] = {}
    for info in infos:
        for item in source.read_files(info.name):
            files[(info.name, item.relative_path)] = SkillFileSnapshot(
                skill_name=info.name,
                relative_path=item.relative_path,
                content=item.content,
            )
    return SkillCatalog(infos=infos, files=files)


def evolvable_catalog(catalog: SkillCatalog) -> SkillCatalog:
    """Keep custom skills. Packaged built-ins are not a self-evolution target."""
    infos = tuple(item for item in catalog.infos if item.source != "builtin")
    names = {item.name for item in infos}
    files = {key: snap for key, snap in catalog.files.items() if key[0] in names}
    return SkillCatalog(infos=infos, files=files)


def snapshot_skills(
    catalog: SkillCatalog,
    *,
    preferred: list[str],
    max_chars: int = MAX_SKILL_CHARS,
    max_files: int = 12,
) -> list[SkillFileSnapshot]:
    """Pick a prompt-sized subset: preferred skills, SKILL.md first."""
    existing = set(catalog.names)
    ordered = [name for name in preferred if name in existing]
    for name in sorted(existing):
        if name not in ordered:
            ordered.append(name)
    candidates: list[SkillFileSnapshot] = []
    for name in ordered:
        skill_md = catalog.get(name, "SKILL.md")
        if skill_md is not None:
            candidates.append(skill_md)
    for name in ordered:
        for (skill_name, rel), snap in sorted(catalog.files.items()):
            if skill_name != name or rel == "SKILL.md":
                continue
            candidates.append(snap)
    out: list[SkillFileSnapshot] = []
    used = 0
    for snap in candidates:
        if len(out) >= max_files:
            return out
        if used + len(snap.content) > max_chars and out:
            continue
        used += len(snap.content)
        out.append(snap)
    return out


__all__ = [
    "SkillCatalog",
    "SkillReadable",
    "evolvable_catalog",
    "load_catalog",
    "snapshot_skills",
]

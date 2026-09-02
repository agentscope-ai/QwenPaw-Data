# -*- coding: utf-8 -*-
"""Discovery of the skills shipped with the qwenpaw-data-skills package."""

from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Any

import yaml

from qwenpaw_data.host.core.utils.safe_name import require_safe_name


@dataclass(frozen=True)
class BuiltinSkill:
    name: str
    src_dir: Path
    group: str
    description: str = ""


def parse_skill_frontmatter_text(text: str, *, source: str = "") -> dict[str, Any]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"SKILL.md frontmatter is required: {source}")
    for end, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
    else:
        raise ValueError(f"SKILL.md frontmatter is not closed: {source}")
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(meta, dict):
        raise ValueError(f"SKILL.md frontmatter must be a mapping: {source}")
    return meta


def read_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    if not skill_md.is_file():
        raise ValueError(f"SKILL.md is required: {skill_md}")
    return parse_skill_frontmatter_text(
        skill_md.read_text(encoding="utf-8"),
        source=str(skill_md),
    )


def _skills_package_root() -> Path | None:
    # utils/skill.py -> .../packages (uv-workspace source checkout)
    packages_root = Path(__file__).resolve().parents[6]
    source_dir = packages_root / "qwenpaw-data-skills" / "skills"
    if source_dir.is_dir():
        return source_dir
    try:
        dist = distribution("qwenpaw-data-skills")
    except PackageNotFoundError:
        return None
    installed = Path(dist.locate_file("qwenpaw_data_skills/skills"))
    if installed.is_dir():
        return installed
    return None


def discover_builtin_skills() -> list[BuiltinSkill]:
    """Discover packaged skills; identity is frontmatter ``name`` (flat)."""
    skills_root = _skills_package_root()
    if skills_root is None:
        raise FileNotFoundError(
            "qwenpaw-data-skills package skills directory not found",
        )

    found: dict[str, BuiltinSkill] = {}
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        if not skill_md.is_file():
            continue
        src_dir = skill_md.parent
        relative = src_dir.relative_to(skills_root)
        group = relative.parts[0] if relative.parts else ""
        meta = read_skill_frontmatter(skill_md)
        name = require_safe_name(str(meta.get("name") or "").strip())
        if name in found:
            raise RuntimeError(
                f"CONFLICT: duplicate builtin skill name {name!r} "
                f"({found[name].src_dir} vs {src_dir})"
            )
        found[name] = BuiltinSkill(
            name=name,
            src_dir=src_dir,
            group=group,
            description=str(meta.get("description") or ""),
        )
    if not found:
        raise FileNotFoundError(f"no SKILL.md found under {skills_root}")
    return [found[key] for key in sorted(found)]

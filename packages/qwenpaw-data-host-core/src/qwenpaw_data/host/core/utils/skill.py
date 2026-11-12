# -*- coding: utf-8 -*-
"""Filesystem helpers for skill directories and packaged builtin skills."""

from __future__ import annotations

import re
import shutil
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.domain.skill import (
    BuiltinSkill,
    SkillConflictError,
    SkillNotFoundError,
)

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.DOTALL)


def normalize_rel_path(relative_path: str) -> str:
    """Normalize a path inside a skill: ``SKILL.md`` or any bundled file."""
    raw = (relative_path or "").strip().replace("\\", "/")
    if not raw or raw.startswith("/") or ".." in PurePosixPath(raw).parts:
        raise ValueError(f"invalid skill relative path: {relative_path!r}")
    if raw.endswith("/"):
        raise ValueError(f"path must be a file, not a directory: {relative_path!r}")
    parts = PurePosixPath(raw).parts
    if not parts or not parts[-1]:
        raise ValueError(f"support file path incomplete: {relative_path!r}")
    return "/".join(parts)


def is_skill_document(relative_path: str) -> bool:
    """True for the skill root document under any letter case.

    Case-insensitive filesystems (macOS, Windows) map ``skill.md`` onto
    ``SKILL.md``, so protection checks must not be fooled by casing.
    """
    return relative_path.casefold() == "skill.md"


def _skill_relpath(relative_path: str) -> str:
    raw = (relative_path or "").strip().replace("\\", "/")
    parts = PurePosixPath(raw).parts
    if not raw or raw.startswith("/") or raw.endswith("/") or ".." in parts:
        raise ValueError(f"invalid skill relative path: {relative_path!r}")
    return "/".join(parts)


def write_skill_files(dest: Path, files: dict[str, str]) -> None:
    """Write a skill directory from relative-path → text. Requires SKILL.md."""
    if dest.exists():
        raise SkillConflictError(f"skill directory already exists: {dest}")
    dest.mkdir(parents=True)
    try:
        for rel, content in files.items():
            if not isinstance(content, str):
                raise ValueError(f"skill file must be text: {rel!r}")
            path = dest / _skill_relpath(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        if not (dest / "SKILL.md").is_file():
            raise ValueError("SKILL.md is required")
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise


def copy_skill_dir(src_dir: Path, dst_dir: Path) -> None:
    """Copy one skill directory; requires ``SKILL.md`` and refuses overwrite."""
    if not (src_dir / "SKILL.md").is_file():
        raise ValueError(f"SKILL.md is required under {src_dir}")
    if dst_dir.exists():
        raise SkillConflictError(f"skill directory already exists: {dst_dir}")
    dst_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_dir, dst_dir)


def move_skills_into(partition: Path) -> None:
    """Move leftover ``skills/<name>/`` and ``skills/.seed/<name>/`` into ``partition``."""
    root = Path(partition).parent
    if root.name != "skills" or not root.is_dir():
        return
    partition.mkdir(parents=True, exist_ok=True)
    for src in (root, root / ".seed"):
        if not src.is_dir():
            continue
        for child in src.iterdir():
            dest = partition / child.name
            if child == partition or dest.exists():
                continue
            if (child / "SKILL.md").is_file():
                shutil.move(child, dest)


def parse_skill_frontmatter_text(content: str, *, source: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(content)
    if match is None:
        raise ValueError(f"SKILL.md missing YAML frontmatter: {source}")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError(f"SKILL.md frontmatter must be a mapping: {source}")
    return data


def read_skill_frontmatter(skill_md: Path) -> dict[str, Any]:
    if not skill_md.is_file():
        raise ValueError(f"SKILL.md is required: {skill_md}")
    return parse_skill_frontmatter_text(
        skill_md.read_text(encoding="utf-8"),
        source=str(skill_md),
    )


def require_skill_document(name: str, content: str) -> str:
    """Validate SKILL.md text; frontmatter ``name`` must equal directory name."""
    safe = require_safe_name(name)
    if not content:
        raise ValueError("SKILL.md content is required")
    meta = parse_skill_frontmatter_text(content, source="SKILL.md")
    raw = meta.get("name")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("SKILL.md frontmatter name is required")
    if raw != raw.strip():
        raise ValueError(
            "SKILL.md frontmatter name must not contain surrounding whitespace"
        )
    if raw != safe:
        raise ValueError(
            f"SKILL.md frontmatter name {raw!r} must equal skill name {safe!r}"
        )
    return safe


def write_skill_md(skill_dir: Path, content: str) -> None:
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


def require_skill_dir(root: Path, name: str) -> Path:
    """Resolve ``name`` under the skill partition; missing dir or SKILL.md fails."""
    safe = require_safe_name(name)
    path = root / safe
    if not path.is_dir():
        raise SkillNotFoundError(f"skill not found: {safe}")
    if not (path / "SKILL.md").is_file():
        raise ValueError(f"SKILL.md is required under {path}")
    return path


def _skills_package_root() -> Path | None:
    # utils/skill.py -> .../packages
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


@lru_cache(maxsize=1)
def discover_builtin_skills() -> tuple[BuiltinSkill, ...]:
    """Discover packaged skills; identity is frontmatter ``name`` (flat)."""
    skills_root = _skills_package_root()
    if skills_root is None:
        raise FileNotFoundError("qwenpaw-data-skills package skills directory not found")

    found: dict[str, BuiltinSkill] = {}
    for skill_md in sorted(skills_root.rglob("SKILL.md")):
        if not skill_md.is_file():
            continue
        src_dir = skill_md.parent
        relative = src_dir.relative_to(skills_root)
        group = relative.parts[0] if relative.parts else ""
        # Runtime policy is implemented by Host. Legacy runtime guides remain
        # packaged for compatibility, but are not agent-selectable data skills.
        if group == "runtime":
            continue
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
    return tuple(found[key] for key in sorted(found))

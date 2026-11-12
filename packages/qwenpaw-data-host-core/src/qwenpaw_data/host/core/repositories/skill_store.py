# -*- coding: utf-8 -*-
from __future__ import annotations

import shutil
from pathlib import Path

from qwenpaw_data.host.core.domain.skill import (
    AgentSkillInfo,
    BuiltinSkill,
    SkillConflictError,
    SkillFile,
)
from qwenpaw_data.host.core.utils.safe_name import require_safe_name
from qwenpaw_data.host.core.utils.skill import (
    copy_skill_dir,
    discover_builtin_skills,
    move_skills_into,
    normalize_rel_path,
    read_skill_frontmatter,
    require_skill_dir,
    require_skill_document,
    write_skill_md,
)

# Entries that live inside the partition or a skill dir but are never content.
SKIP_NAMES = frozenset(
    {
        ".git",
        "__pycache__",
        ".DS_Store",
        ".index",
        ".qwenpaw-data-builtin",
    }
)

MAX_SKILL_TOTAL_CHARS = 5 * 1024 * 1024
_BUILTIN_MARKER = ".qwenpaw-data-builtin"


def _crosses_symlink(root: Path, parts: tuple[str, ...]) -> bool:
    """True when any component below ``root`` (incl. the leaf) is a symlink."""
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


class SkillStoreRepo:
    """本 agent 技能分区的文件视图

    workspace/skills/<agent_id>/
      .index              # AgentScope owns it
      <name>/SKILL.md     # plus any bundled support files
    """

    def __init__(self, skills_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)

    def ensure_ready(self) -> list[str]:
        """Install packaged builtins. Unmarked custom directories are left alone."""
        move_skills_into(self.skills_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        packaged = {item.name: item for item in discover_builtin_skills()}
        self._drop_retired(set(packaged))
        synced: list[str] = []
        for item in packaged.values():
            dest = self.skills_dir / item.name
            if dest.exists() and not (dest / _BUILTIN_MARKER).is_file():
                continue
            if dest.exists():
                shutil.rmtree(dest)
            copy_skill_dir(item.src_dir, dest)
            self._mark_builtin(dest)
            synced.append(item.name)
        return synced

    def _drop_retired(self, packaged_names: set[str]) -> None:
        """Delete marked builtin checkouts the resource pack no longer ships."""
        for child in list(self.skills_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if child.name in packaged_names:
                continue
            if (child / _BUILTIN_MARKER).is_file():
                shutil.rmtree(child)

    @staticmethod
    def _mark_builtin(dest: Path) -> None:
        marker = dest / _BUILTIN_MARKER
        if not marker.is_file():
            marker.write_text("", encoding="utf-8")

    def _reject_reserved_name(self, name: str) -> None:
        if self._builtin(name) is not None:
            raise SkillConflictError(f"skill name is reserved by builtin: {name}")

    def list_skills(self) -> list[AgentSkillInfo]:
        if not self.skills_dir.is_dir():
            return []
        return [
            self._info(path.name)
            for path in sorted(self.skills_dir.iterdir())
            if path.is_dir()
            and not path.name.startswith(".")
            and (path / "SKILL.md").is_file()
        ]

    def info(self, name: str) -> AgentSkillInfo:
        require_skill_dir(self.skills_dir, name)
        return self._info(name)

    def require(self, name: str) -> Path:
        """Resolve one skill directory; unknown name or missing SKILL.md fails."""
        return require_skill_dir(self.skills_dir, name)

    def read_files(self, name: str) -> list[SkillFile]:
        """Snapshot every text file of one skill, relative to its root.

        Symlinked files/dirs are skipped and every file must resolve inside
        the skill root, so a planted link cannot leak Host files.
        """
        root = self.require(name)
        resolved_root = root.resolve()
        files: list[SkillFile] = []
        used = 0
        for path in sorted(root.rglob("*")):
            rel_parts = path.relative_to(root).parts
            if any(part in SKIP_NAMES for part in rel_parts):
                continue
            if _crosses_symlink(root, rel_parts):
                continue
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(resolved_root):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"skill file is not utf-8 text: {path}") from exc
            used += len(content)
            if used > MAX_SKILL_TOTAL_CHARS:
                raise ValueError(f"skill exceeds {MAX_SKILL_TOTAL_CHARS} chars: {name}")
            files.append(SkillFile(relative_path="/".join(rel_parts), content=content))
        return files

    def replace_files(self, name: str, files: list[SkillFile]) -> None:
        """Make the skill directory match ``files`` exactly, SKILL.md required.

        Every target is validated (resolved containment, no symlinks) before
        anything is deleted, so a rejected update leaves the skill untouched.
        """
        root = self.require(name)
        resolved_root = root.resolve()
        wanted = {
            normalize_rel_path(item.relative_path): item.content for item in files
        }
        if "SKILL.md" not in wanted:
            raise ValueError(f"SKILL.md is required for skill: {name}")
        targets: dict[str, Path] = {}
        for rel in wanted:
            parts = tuple(rel.split("/"))
            if any(part in SKIP_NAMES for part in parts):
                raise ValueError(f"reserved skill path rejected: {rel}")
            target = root.joinpath(*parts)
            if _crosses_symlink(root, parts) or not target.resolve().is_relative_to(
                resolved_root
            ):
                raise ValueError(f"path escape rejected: {rel}")
            if target.is_dir():
                raise ValueError(f"path is a directory: {rel}")
            targets[rel] = target
        parents = {
            "/".join(rel.split("/")[:depth])
            for rel in wanted
            for depth in range(1, rel.count("/") + 1)
        }
        if clash := sorted(parents & set(wanted)):
            raise ValueError(f"path is both a file and a directory: {clash[0]}")
        for path in sorted(root.rglob("*"), reverse=True):
            rel_parts = path.relative_to(root).parts
            if any(part in SKIP_NAMES for part in rel_parts):
                continue
            if _crosses_symlink(root, rel_parts[:-1]):
                continue  # never act through a linked directory
            if path.is_symlink():
                # Targets are link-free, so any link is stray: drop the link
                # itself, never what it points at.
                path.unlink()
            elif path.is_file() and "/".join(rel_parts) not in wanted:
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        for rel, target in targets.items():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(wanted[rel], encoding="utf-8")

    def create_skill(self, name: str, content: str) -> str:
        skill_name = require_skill_document(name, content)
        self._reject_reserved_name(skill_name)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        dest = self.skills_dir / skill_name
        if dest.exists():
            raise SkillConflictError(f"skill already exists: {skill_name}")
        try:
            write_skill_md(dest, content)
        except Exception:
            shutil.rmtree(dest, ignore_errors=True)
            raise
        return skill_name

    def import_skill_dir(self, src_dir: Path) -> str:
        meta = read_skill_frontmatter(src_dir / "SKILL.md")
        skill_name = require_safe_name(str(meta.get("name") or "").strip())
        self._reject_reserved_name(skill_name)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        dest = self.skills_dir / skill_name
        if dest.exists():
            raise SkillConflictError(f"skill already exists: {skill_name}")
        copy_skill_dir(src_dir, dest)
        return skill_name

    def uninstall_skill(self, name: str) -> None:
        shutil.rmtree(self.require(name))

    def _builtin(self, name: str) -> BuiltinSkill | None:
        for item in discover_builtin_skills():
            if item.name == name:
                return item
        return None

    def _info(self, name: str) -> AgentSkillInfo:
        dest = self.skills_dir / name
        builtin = self._builtin(name)
        installed = builtin is not None and (dest / _BUILTIN_MARKER).is_file()
        description = ""
        try:
            meta = read_skill_frontmatter(dest / "SKILL.md")
            description = str(meta.get("description") or "")
        except ValueError:
            description = ""
        return AgentSkillInfo(
            name=name,
            enabled=True,
            source="builtin" if installed else "custom",
            group=builtin.group if installed else "",
            description=description,
        )

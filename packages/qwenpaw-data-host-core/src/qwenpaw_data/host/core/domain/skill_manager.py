# -*- coding: utf-8 -*-
"""The only write path for agent skills: disk change plus its revision."""

from __future__ import annotations

import tempfile
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from qwenpaw_data.host.core.db.tables import SkillRevisionOrigin
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.skill import (
    AgentSkillInfo,
    SkillConflictError,
    SkillFile,
)
from qwenpaw_data.host.core.paths import UserAgentPaths
from qwenpaw_data.host.core.repositories.skill_revision import SkillRevisionRepository
from qwenpaw_data.host.core.repositories.skill_state import SkillStateRepository
from qwenpaw_data.host.core.repositories.skill_store import SkillStoreRepo
from qwenpaw_data.host.core.utils.skill import normalize_rel_path, require_skill_document


class SkillManager:
    """Owns agent skills: file content, enable state, and revision history."""

    def __init__(
        self,
        db: AsyncSession,
        *,
        identity: Identity,
        paths: UserAgentPaths,
    ) -> None:
        self.store = SkillStoreRepo(paths.skills)
        self._revisions = SkillRevisionRepository(db, identity, agent_id=paths.agent_id)
        self._states = SkillStateRepository(db, identity, agent_id=paths.agent_id)

    def ensure_ready(self) -> list[str]:
        return self.store.ensure_ready()

    async def list_skills(self) -> list[AgentSkillInfo]:
        disabled = await self._states.disabled_names()
        return [
            replace(info, enabled=info.name not in disabled)
            for info in self.store.list_skills()
        ]

    async def read_skill(self, name: str) -> tuple[AgentSkillInfo, str]:
        """Return the skill and its ``SKILL.md`` content."""
        info = self.store.info(name)
        disabled = await self._states.disabled_names()
        files = {item.relative_path: item.content for item in self.read_files(name)}
        return replace(info, enabled=name not in disabled), files.get("SKILL.md", "")

    def read_files(self, name: str) -> list[SkillFile]:
        """Snapshot every text file of one skill."""
        return self.store.read_files(name)

    async def enable(self, name: str) -> None:
        self.store.require(name)
        await self._states.set_disabled(name, False)

    async def disable(self, name: str) -> None:
        self.store.require(name)
        await self._states.set_disabled(name, True)

    async def create(self, name: str, content: str) -> str:
        skill_name = self.store.create_skill(name, content)
        await self._append(skill_name, origin="create")
        return skill_name

    async def import_zip(self, zip_path: Path) -> list[str]:
        imported: list[str] = []
        with tempfile.TemporaryDirectory(prefix="qwenpaw-data-skill-import-") as tmp:
            root = Path(tmp)
            with zipfile.ZipFile(zip_path) as zf:
                zf.extractall(root)
            skill_dirs = [md.parent for md in sorted(root.rglob("SKILL.md"))]
            if not skill_dirs:
                raise ValueError("zip contains no SKILL.md")
            for src_dir in skill_dirs:
                imported.append(await self.import_dir(src_dir))
        return imported

    async def import_dir(
        self,
        src_dir: Path,
        *,
        source_ref: str = "",
        note: str = "",
    ) -> str:
        skill_name = self.store.import_skill_dir(src_dir)
        await self._append(
            skill_name, origin="import", source_ref=source_ref, note=note
        )
        return skill_name

    async def save(
        self,
        name: str,
        content: str,
        *,
        base_content: str | None = None,
    ) -> str:
        """Manual edit of ``SKILL.md``; builtins stay read-only for humans.

        Rename is not supported: directory name and frontmatter ``name`` stay fixed.
        """
        if self.store.info(name).source == "builtin":
            raise PermissionError(f"cannot modify builtin skill: {name}")
        require_skill_document(name, content)
        expect = (
            [SkillFile(relative_path="SKILL.md", content=base_content)]
            if base_content is not None
            else []
        )
        await self.apply_files(
            name,
            [SkillFile(relative_path="SKILL.md", content=content)],
            expect=expect,
            origin="manual_edit",
        )
        return name

    async def apply_files(
        self,
        name: str,
        files: list[SkillFile],
        *,
        expect: list[SkillFile],
        origin: SkillRevisionOrigin,
        source_ref: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Write changed files of one skill, keeping its other files intact.

        ``expect`` carries the content each changed file had when the caller
        computed ``files``; an empty string means the file did not exist.
        """
        merged = {
            item.relative_path: item.content for item in self.store.read_files(name)
        }
        for item in expect:
            rel = normalize_rel_path(item.relative_path)
            if merged.get(rel, "") != item.content:
                raise SkillConflictError(
                    f"skill file changed since it was read: {name}/{rel}"
                )
        for item in files:
            merged[normalize_rel_path(item.relative_path)] = item.content
        return await self._write(
            name,
            [
                SkillFile(relative_path=rel, content=merged[rel])
                for rel in sorted(merged)
            ],
            origin=origin,
            source_ref=source_ref,
            note=note,
        )

    async def uninstall(self, name: str) -> None:
        if self.store.info(name).source == "builtin":
            raise PermissionError(f"cannot uninstall builtin skill: {name}")
        await self._ensure_base(name)
        self.store.uninstall_skill(name)
        await self._states.forget(name)

    async def history(self, name: str) -> list[dict[str, Any]]:
        await self._ensure_base(name)
        return await self._revisions.list_history(name)

    async def revision(self, name: str, revision_no: int) -> dict[str, Any]:
        return await self._revisions.get(name, revision_no)

    async def revert(self, name: str, revision_no: int) -> dict[str, Any]:
        target = await self._revisions.get(name, revision_no)
        return await self._write(
            name,
            [
                SkillFile(
                    relative_path=str(entry["relative_path"]),
                    content=str(entry["content"]),
                )
                for entry in target["files"]
            ],
            origin="revert",
            source_ref=str(target["id"]),
            note=f"回滚到版本 {revision_no}",
        )

    async def _write(
        self,
        name: str,
        files: list[SkillFile],
        *,
        origin: SkillRevisionOrigin,
        source_ref: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Record first, then write: a failed write leaves no orphan revision."""
        await self._ensure_base(name)
        revision = await self._append(
            name, files=files, origin=origin, source_ref=source_ref, note=note
        )
        self.store.replace_files(name, files)
        return revision

    async def _ensure_base(self, name: str) -> None:
        """Seeded and imported skills get their base revision on first change."""
        if await self._revisions.max_revision_no(name) is None:
            await self._append(name, origin="install", note="初始版本")

    async def _append(
        self,
        name: str,
        *,
        files: list[SkillFile] | None = None,
        origin: SkillRevisionOrigin,
        source_ref: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """``files`` defaults to whatever is on disk, for copy-made skills."""
        snapshot = self.store.read_files(name) if files is None else files
        return await self._revisions.append(
            name,
            files=[
                {"relative_path": item.relative_path, "content": item.content}
                for item in snapshot
            ],
            origin=origin,
            source_ref=source_ref,
            note=note,
        )

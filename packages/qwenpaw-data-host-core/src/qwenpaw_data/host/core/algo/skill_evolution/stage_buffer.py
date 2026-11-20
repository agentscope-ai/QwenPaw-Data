# -*- coding: utf-8 -*-
"""In-memory staging buffer for skill evolution fork tools."""

from __future__ import annotations

from dataclasses import dataclass, field

from qwenpaw_data.host.core.algo.skill_evolution.models import ProposedPatch


@dataclass
class StageBuffer:
    """Collect staged patches; never writes skill files to disk."""

    max_patches: int = 8
    max_files: int = 12
    summary: str = ""
    done: bool = False
    patches: list[ProposedPatch] = field(default_factory=list)
    viewed: set[tuple[str, str]] = field(default_factory=set)
    _files: set[tuple[str, str]] = field(default_factory=set)
    _composed: dict[tuple[str, str], str] = field(default_factory=dict)

    def mark_viewed(self, skill_name: str, relative_path: str) -> None:
        self.viewed.add((skill_name, relative_path))

    def was_viewed(self, skill_name: str, relative_path: str) -> bool:
        return (skill_name, relative_path) in self.viewed

    def current_text(self, skill_name: str, relative_path: str) -> str | None:
        """Staged file text after earlier edits, or ``None`` if never staged."""
        return self._composed.get((skill_name, relative_path))

    def stage(self, patch: ProposedPatch, *, after: str | None = None) -> str | None:
        """Stage a patch. Returns an error string on refusal, else None.

        Multiple edits against the same file are allowed (each with its own
        reason); ``max_files`` still caps distinct skill files.
        """
        if not (patch.reason or "").strip():
            return "reason is required: cite the conversation evidence for this edit"
        key = (patch.skill_name, patch.relative_path)
        if key not in self._files and len(self._files) >= self.max_files:
            return f"max_files ({self.max_files}) reached"
        if len(self.patches) >= self.max_patches:
            return f"max_patches ({self.max_patches}) reached"
        self._files.add(key)
        self.patches.append(patch)
        if after is not None:
            self._composed[key] = after
        return None

    def set_done(self, summary: str) -> None:
        self.summary = (summary or "").strip() or "Skill 修订提案"
        self.done = True


__all__ = ["StageBuffer"]

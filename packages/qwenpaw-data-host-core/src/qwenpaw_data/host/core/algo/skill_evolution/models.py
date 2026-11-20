# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

SkillPatchStatus = Literal["pending", "accepted", "rejected"]


def patch_dict(
    *,
    patch_id: str,
    skill_name: str,
    relative_path: str,
    before: str,
    after: str,
    diff: str,
    status: SkillPatchStatus = "pending",
    hunks: list[dict[str, Any]] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    return {
        "patch_id": patch_id,
        "skill_name": skill_name,
        "relative_path": relative_path,
        "before": before,
        "after": after,
        "diff": diff,
        "reason": reason,
        "hunks": list(hunks or []),
        "status": status,
    }


@dataclass
class SignalState:
    user_feedback_count: int = 0
    plan_edit_pending: bool = False


@dataclass
class TraceContext:
    """Session traces accumulated before the triggering chat."""

    chat_id: str
    segments_text: str = ""
    biz_events_text: str = ""
    skills_used: list[str] = field(default_factory=list)
    feedback_texts: list[str] = field(default_factory=list)
    plan_edit_note: str = ""


@dataclass
class SkillFileSnapshot:
    skill_name: str
    relative_path: str
    content: str


@dataclass
class ProposedPatch:
    skill_name: str
    relative_path: str
    old_string: str | None
    new_string: str
    # True when creating a new support file under an existing skill.
    create_file: bool = False
    # Why this edit should be applied (shown next to the selectable diff).
    reason: str = ""


@dataclass
class EvolutionProposal:
    """One session-level proposal for list + review (no trigger label)."""

    summary: str
    skills: list[str] = field(default_factory=list)
    patch_count: int = 0
    patches: list[dict[str, Any]] = field(default_factory=list)


__all__ = [
    "EvolutionProposal",
    "ProposedPatch",
    "SignalState",
    "SkillFileSnapshot",
    "SkillPatchStatus",
    "TraceContext",
    "patch_dict",
]

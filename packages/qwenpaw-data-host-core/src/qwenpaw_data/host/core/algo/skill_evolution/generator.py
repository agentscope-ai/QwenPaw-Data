# -*- coding: utf-8 -*-
"""Prompt rendering and legacy JSON proposal parsing for skill evolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution.models import (
    EvolutionProposal,
    ProposedPatch,
    SkillFileSnapshot,
    TraceContext,
)

_FIRST_RUN_MODE = "首次：上方历史是本 Session 迄今对话（含本轮）。可据此提出做法修订。"
_INCREMENTAL_RUN_MODE = (
    "增量：上方历史只有本轮 Chat。已接受的做法在 skill 文件里，先 `skill_view`。"
    "只根据本轮反馈提出新增或覆盖同一处的 hunk；未改动的旧 hunk 由 Host 保留。"
    "不要重放未出现在本轮的旧对话。"
)

PROMPT_TEMPLATE_PATH = Path(__file__).parent / "prompts" / "SKILL_EVOLUTION.md"


def format_prior_pending(prior_pending: EvolutionProposal | None) -> str:
    """Compact digest of a still-pending proposal for the fork prompt."""
    if prior_pending is None or not prior_pending.patches:
        return "（无）"
    lines = [f"摘要：{prior_pending.summary or '（无）'}"]
    for patch in prior_pending.patches:
        if not isinstance(patch, dict):
            continue
        path = f"{patch.get('skill_name') or '?'}/{patch.get('relative_path') or '?'}"
        hunks = patch.get("hunks") or []
        if not isinstance(hunks, list) or not hunks:
            reason = str(patch.get("reason") or "").strip()
            suffix = f" · {reason}" if reason else ""
            lines.append(f"- {path}{suffix}")
            continue
        for hunk in hunks:
            if not isinstance(hunk, dict):
                continue
            hunk_id = str(hunk.get("hunk_id") or "")
            header = str(hunk.get("header") or "").strip()
            reason = str(hunk.get("reason") or "").strip()
            bits = [path]
            if hunk_id:
                bits.append(hunk_id)
            if header:
                bits.append(header)
            if reason:
                bits.append(reason)
            lines.append("- " + " · ".join(bits))
    return "\n".join(lines) if len(lines) > 1 else "（无）"


def render_review_prompt(
    *,
    feedback_texts: list[str],
    skills_used: list[str],
    plan_edit_note: str = "",
    incremental: bool = False,
    prior_pending: EvolutionProposal | None = None,
    template_path: Path = PROMPT_TEMPLATE_PATH,
) -> str:
    """Render the Chinese tool-oriented review prompt injected after history."""
    values = {
        "run_mode": _INCREMENTAL_RUN_MODE if incremental else _FIRST_RUN_MODE,
        "prior_pending": format_prior_pending(prior_pending),
        "feedback_texts": "\n".join(f"- {t}" for t in feedback_texts) or "（无）",
        "plan_edit_note": plan_edit_note or "（无）",
        "skills_used": "、".join(skills_used) or "（未识别）",
    }
    prompt = template_path.read_text(encoding="utf-8")
    for key, value in values.items():
        prompt = prompt.replace("{" + key + "}", value)
    return prompt


def render_prompt(
    *,
    trace: TraceContext,
    skill_files: list[SkillFileSnapshot] | None = None,
    template_path: Path = PROMPT_TEMPLATE_PATH,
) -> str:
    """Backward-compatible wrapper around :func:`render_review_prompt`."""
    del skill_files  # no longer inlined; fork views files via tools
    return render_review_prompt(
        feedback_texts=trace.feedback_texts,
        skills_used=trace.skills_used,
        plan_edit_note=trace.plan_edit_note,
        template_path=template_path,
    )


def parse_proposals(
    payload: dict[str, Any],
) -> tuple[str, list[ProposedPatch]]:
    """Parse legacy one-shot JSON proposals (kept for tests / compatibility)."""
    summary = str(payload.get("summary") or "").strip() or "Skill 修订提案"
    raw = payload.get("patches")
    if not isinstance(raw, list):
        return summary, []
    out: list[ProposedPatch] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        skill_name = str(item.get("skill_name") or "").strip()
        relative_path = str(item.get("relative_path") or "").strip()
        new_string = item.get("new_string")
        if not skill_name or not relative_path or not isinstance(new_string, str):
            continue
        old = item.get("old_string")
        if old is not None and not isinstance(old, str):
            old = None
        out.append(
            ProposedPatch(
                skill_name=skill_name,
                relative_path=relative_path,
                old_string=old,
                new_string=new_string,
                create_file=bool(item.get("create_file")),
                reason=str(item.get("reason") or "").strip(),
            )
        )
    return summary, out


__all__ = [
    "PROMPT_TEMPLATE_PATH",
    "format_prior_pending",
    "parse_proposals",
    "render_prompt",
    "render_review_prompt",
]

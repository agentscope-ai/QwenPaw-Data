# -*- coding: utf-8 -*-
"""Merge a later fork's patches into a still-pending proposal."""

from __future__ import annotations

import logging
from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution.diffutil import unified_diff
from qwenpaw_data.host.core.algo.skill_evolution.hunks import (
    context_snippet,
    ensure_patch_hunks,
    materialize_after_from_hunks,
)
from qwenpaw_data.host.core.algo.skill_evolution.models import EvolutionProposal
from qwenpaw_data.host.core.algo.skill_evolution.settings import MAX_FILES

logger = logging.getLogger(__name__)


def merge_pending_proposal(
    prior: EvolutionProposal | None,
    incoming: EvolutionProposal,
    *,
    max_files: int = MAX_FILES,
) -> EvolutionProposal:
    """Union incoming hunks onto ``prior``; overlapping spans take the later edit."""
    if prior is None or not prior.patches:
        patches = list(incoming.patches)
        if len(patches) > max_files:
            patches = patches[:max_files]
        return EvolutionProposal(
            summary=incoming.summary,
            skills=list(incoming.skills),
            patch_count=len(patches),
            patches=patches,
        )
    if not incoming.patches:
        return EvolutionProposal(
            summary=prior.summary,
            skills=list(prior.skills),
            patch_count=len(prior.patches),
            patches=[dict(item) for item in prior.patches],
        )
    patches = merge_pending_patches(
        list(prior.patches),
        list(incoming.patches),
        max_files=max_files,
    )
    return EvolutionProposal(
        summary=_merge_summary(prior.summary, incoming.summary),
        skills=_merge_skills(prior.skills, incoming.skills),
        patch_count=len(patches),
        patches=patches,
    )


def merge_pending_patches(
    prior: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
    *,
    max_files: int = MAX_FILES,
) -> list[dict[str, Any]]:
    """Keep prior files/hunks; add incoming; replace overlapping change spans."""
    prior_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for patch in prior:
        if not isinstance(patch, dict):
            continue
        key = _file_key(patch)
        if key[0] and key not in prior_by_key:
            prior_by_key[key] = dict(patch)
            order.append(key)

    incoming_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    incoming_order: list[tuple[str, str]] = []
    for patch in incoming:
        if not isinstance(patch, dict):
            continue
        key = _file_key(patch)
        if not key[0]:
            continue
        if key not in incoming_by_key:
            incoming_order.append(key)
        incoming_by_key[key] = dict(patch)

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for key in order:
        seen.add(key)
        current = prior_by_key[key]
        extra = incoming_by_key.get(key)
        out.append(_merge_file(current, extra) if extra is not None else current)

    for key in incoming_order:
        if key in seen:
            continue
        if len(out) >= max_files:
            logger.warning(
                "skill evolution skip new file over max_files=%s: %s/%s",
                max_files,
                key[0],
                key[1],
            )
            continue
        out.append(incoming_by_key[key])
        seen.add(key)
    return out


def _file_key(patch: dict[str, Any]) -> tuple[str, str]:
    return (
        str(patch.get("skill_name") or "").strip(),
        str(patch.get("relative_path") or "").strip(),
    )


def _merge_file(
    prior: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any]:
    prior_before = str(prior.get("before") or "")
    incoming_before = str(incoming.get("before") or "")
    patch_id = str(prior.get("patch_id") or incoming.get("patch_id") or "")
    if prior_before != incoming_before:
        merged = dict(incoming)
        if patch_id:
            merged["patch_id"] = patch_id
        return merged

    prior_hunks = list(ensure_patch_hunks(prior))
    incoming_hunks = list(ensure_patch_hunks(incoming))
    incoming_spans = [_hunk_span(hunk, incoming_before) for hunk in incoming_hunks]
    kept: list[dict[str, Any]] = []
    for hunk in prior_hunks:
        span = _hunk_span(hunk, prior_before)
        if span is None or any(
            other is None or _spans_overlap(span, other) for other in incoming_spans
        ):
            continue
        kept.append(hunk)
    merged_hunks = kept + incoming_hunks
    try:
        after = materialize_after_from_hunks(prior_before, merged_hunks, None)
    except ValueError:
        logger.warning(
            "skill evolution merge rematerialize failed; using incoming file %s/%s",
            prior.get("skill_name"),
            prior.get("relative_path"),
        )
        merged = dict(incoming)
        if patch_id:
            merged["patch_id"] = patch_id
        return merged

    skill_name = str(prior.get("skill_name") or incoming.get("skill_name") or "")
    relative_path = str(
        prior.get("relative_path") or incoming.get("relative_path") or ""
    )
    reason = "；".join(
        dict.fromkeys(
            str(hunk.get("reason") or "").strip()
            for hunk in merged_hunks
            if str(hunk.get("reason") or "").strip()
        )
    )
    return {
        **prior,
        "patch_id": patch_id or prior.get("patch_id"),
        "skill_name": skill_name,
        "relative_path": relative_path,
        "before": prior_before,
        "after": after,
        "diff": unified_diff(prior_before, after, path=f"{skill_name}/{relative_path}"),
        "reason": reason,
        "hunks": merged_hunks,
        "status": str(prior.get("status") or "pending"),
    }


def _hunk_span(hunk: dict[str, Any], before: str) -> tuple[int, int] | None:
    start = hunk.get("change_old_start")
    end = hunk.get("change_old_end")
    if isinstance(start, int) and start > 0 and isinstance(end, int) and end > 0:
        return start, end
    old = hunk.get("old_string")
    new = hunk.get("new_string")
    if not isinstance(old, str):
        return None
    loc = context_snippet(before, old, new if isinstance(new, str) else "")
    start = loc.get("change_old_start")
    end = loc.get("change_old_end")
    if isinstance(start, int) and start > 0 and isinstance(end, int) and end > 0:
        return start, end
    return None


def _spans_overlap(left: tuple[int, int], right: tuple[int, int]) -> bool:
    return left[0] <= right[1] and right[0] <= left[1]


def _merge_summary(prior: str, incoming: str) -> str:
    parts: list[str] = []
    for raw in (prior, incoming):
        text = (raw or "").strip()
        if text and text not in parts:
            parts.append(text)
    return "；".join(parts)


def _merge_skills(prior: list[str], incoming: list[str]) -> list[str]:
    return list(dict.fromkeys([*(prior or []), *(incoming or [])]))


__all__ = ["merge_pending_patches", "merge_pending_proposal"]

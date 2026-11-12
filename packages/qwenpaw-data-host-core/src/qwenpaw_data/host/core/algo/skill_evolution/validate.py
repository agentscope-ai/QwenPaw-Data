# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from collections import OrderedDict
from qwenpaw_data.host.core.algo.skill_evolution.diffutil import (
    apply_old_new_fuzzy,
    unified_diff,
)
from qwenpaw_data.host.core.algo.skill_evolution.hunks import build_hunk, parse_hunks
from qwenpaw_data.host.core.algo.skill_evolution.models import (
    ProposedPatch,
    SkillFileSnapshot,
    patch_dict,
)
from qwenpaw_data.host.core.utils.skill import is_skill_document, normalize_rel_path
from qwenpaw_data.host.core.utils.ids import create_id
from qwenpaw_data.host.core.utils.safe_name import require_safe_name

logger = logging.getLogger(__name__)


def _hunks_for_edit(
    *,
    before: str,
    after: str,
    reason: str,
    patch_id: str,
    start_index: int,
) -> list[dict]:
    """Split one staged edit into selectable contiguous blocks, sharing ``reason``."""
    diff = unified_diff(before, after, path="staged")
    parsed = parse_hunks(diff, patch_id=patch_id, before=before)
    if not parsed:
        return [
            build_hunk(
                patch_id=patch_id,
                index=start_index,
                old_string=before if after != before else "",
                new_string=after,
                before=before,
                reason=reason,
            )
        ]
    out: list[dict] = []
    for offset, item in enumerate(parsed):
        index = start_index + offset
        header = str(item.get("header") or "")
        suffix = header.split(" · ", 1)[1] if " · " in header else ""
        item["hunk_id"] = f"{patch_id}:e{index}"
        item["header"] = (
            f"edit {index + 1} · {suffix}" if suffix else f"edit {index + 1}"
        )
        item["reason"] = reason
        item["status"] = "pending"
        out.append(item)
    return out


def _apply_edit(cursor: str, item: ProposedPatch) -> str | None:
    """Apply one staged edit. ``None`` when the edit cannot be applied."""
    old = item.old_string if item.old_string is not None else cursor
    try:
        if old == "":
            next_after = item.new_string
        else:
            next_after = apply_old_new_fuzzy(cursor, old, item.new_string)
    except ValueError as exc:
        logger.warning(
            "skill evolution skip edit %s/%s: %s",
            item.skill_name,
            item.relative_path,
            exc,
        )
        return None
    if next_after == cursor:
        return None
    return next_after


def _materialize_file(
    skill_name: str,
    rel: str,
    items: list[ProposedPatch],
    *,
    snap: SkillFileSnapshot | None,
) -> dict | None:
    first = items[0]
    create_file = bool(first.create_file) or (first.old_string is None and snap is None)
    if create_file and snap is not None and snap.content:
        logger.warning(
            "skill evolution skip create; file exists: %s/%s", skill_name, rel
        )
        return None

    if create_file:
        if is_skill_document(rel):
            logger.warning("skill evolution skip create SKILL.md for %s", skill_name)
            return None
        before = ""
        cursor = first.new_string
        if before == cursor:
            return None
        patch_id = create_id("skpatch")
        reason = (first.reason or "").strip()
        hunks = [
            build_hunk(
                patch_id=patch_id,
                index=0,
                old_string="",
                new_string=cursor,
                before="",
                reason=reason,
            )
        ]
        rest = items[1:]
    else:
        if snap is None:
            logger.warning("skill evolution skip missing file: %s/%s", skill_name, rel)
            return None
        before = snap.content
        cursor = before
        patch_id = create_id("skpatch")
        hunks = []
        rest = items

    for item in rest:
        next_after = _apply_edit(cursor, item)
        if next_after is None:
            continue
        hunks.extend(
            _hunks_for_edit(
                before=cursor,
                after=next_after,
                reason=(item.reason or "").strip(),
                patch_id=patch_id,
                start_index=len(hunks),
            )
        )
        cursor = next_after

    after = cursor
    if before == after or not hunks:
        return None
    file_reason = "；".join(
        dict.fromkeys(h["reason"] for h in hunks if h.get("reason"))
    )
    return patch_dict(
        patch_id=patch_id,
        skill_name=skill_name,
        relative_path=rel,
        before=before,
        after=after,
        diff=unified_diff(before, after, path=f"{skill_name}/{rel}"),
        hunks=hunks,
        reason=file_reason,
    )


def validate_and_materialize(
    proposals: list[ProposedPatch],
    *,
    snapshots: dict[tuple[str, str], SkillFileSnapshot],
    existing_skills: set[str],
    max_patches: int,
    max_files: int,
) -> list[dict]:
    """Validate staged edits and build persisted patch dicts with diffs.

    Multiple ``ProposedPatch`` rows for the same file are merged into one
    file-level patch. Each staged edit may split into contiguous selectable
    hunks that inherit that edit's ``reason``.

    A bad skill, path, or edit is skipped; other files still materialize.
    """
    if len(proposals) > max_patches:
        proposals = proposals[:max_patches]

    grouped: OrderedDict[tuple[str, str], list[ProposedPatch]] = OrderedDict()
    for item in proposals:
        try:
            skill_name = require_safe_name(item.skill_name.strip())
        except Exception:
            logger.warning(
                "skill evolution skip invalid skill name: %r", item.skill_name
            )
            continue
        if skill_name not in existing_skills:
            logger.warning("skill evolution skip unknown skill: %s", skill_name)
            continue
        try:
            rel = normalize_rel_path(item.relative_path)
        except ValueError:
            logger.warning(
                "skill evolution skip invalid path %s/%r",
                skill_name,
                item.relative_path,
            )
            continue
        key = (skill_name, rel)
        if key not in grouped and len(grouped) >= max_files:
            break
        grouped.setdefault(key, []).append(item)

    out: list[dict] = []
    for (skill_name, rel), items in grouped.items():
        materialized = _materialize_file(
            skill_name,
            rel,
            items,
            snap=snapshots.get((skill_name, rel)),
        )
        if materialized is not None:
            out.append(materialized)
    return out


__all__ = ["validate_and_materialize"]

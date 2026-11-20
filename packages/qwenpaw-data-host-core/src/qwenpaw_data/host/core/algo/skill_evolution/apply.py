# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.skill_evolution.diffutil import unified_diff
from qwenpaw_data.host.core.algo.skill_evolution.hunks import (
    ensure_patch_hunks,
    materialize_after_from_hunks,
)


def select_accepted_patches(
    patches: list[dict[str, Any]],
    patch_ids: list[str] | None = None,
    hunk_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Keep selected hunks and recompute each file's ``after``.

    ``hunk_ids`` wins when not None; else ``patch_ids`` takes those files'
    hunks; both None means all hunks. Empty selection raises ``ValueError``.
    """
    if hunk_ids is not None:
        selected = {str(item) for item in hunk_ids}
    elif patch_ids is not None:
        wanted = {str(item) for item in patch_ids}
        selected = {
            str(hunk.get("hunk_id") or "")
            for patch in patches
            if str(patch.get("patch_id") or "") in wanted
            for hunk in ensure_patch_hunks(patch)
            if hunk.get("hunk_id")
        }
    else:
        selected = {
            str(hunk.get("hunk_id") or "")
            for patch in patches
            for hunk in ensure_patch_hunks(patch)
            if hunk.get("hunk_id")
        }
    selected.discard("")
    if not selected:
        raise ValueError("no hunks selected")

    accepted: list[dict[str, Any]] = []
    for patch in patches:
        hunks = ensure_patch_hunks(patch)
        chosen = [hunk for hunk in hunks if str(hunk.get("hunk_id") or "") in selected]
        if not chosen:
            continue
        before = str(patch.get("before") or "")
        after = materialize_after_from_hunks(before, hunks, selected)
        skill_name = str(patch.get("skill_name") or "")
        relative_path = str(patch.get("relative_path") or "")
        next_patch = dict(patch)
        next_patch["after"] = after
        next_patch["diff"] = unified_diff(
            before, after, path=f"{skill_name}/{relative_path}"
        )
        next_patch["hunks"] = [
            {
                **hunk,
                "status": (
                    "accepted"
                    if str(hunk.get("hunk_id") or "") in selected
                    else "rejected"
                ),
            }
            for hunk in hunks
        ]
        next_patch["status"] = "accepted"
        accepted.append(next_patch)
    if not accepted:
        raise ValueError("no hunks selected")
    return accepted


__all__ = ["select_accepted_patches"]

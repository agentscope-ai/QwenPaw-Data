# -*- coding: utf-8 -*-
from __future__ import annotations

from qwenpaw_data.host.core.algo.skill_evolution.diffutil import unified_diff
from qwenpaw_data.host.core.algo.skill_evolution.hunks import (
    ensure_patch_hunks,
    materialize_after_from_hunks,
    parse_hunks,
)
from qwenpaw_data.host.core.algo.skill_evolution.models import (
    ProposedPatch,
    SkillFileSnapshot,
)
from qwenpaw_data.host.core.algo.skill_evolution.validate import validate_and_materialize


def test_parse_splits_merged_hunk_into_edits() -> None:
    before = "a\nb\nc\nd\ne\n"
    after = "a\nB\nc\nd\nE\n"
    diff = unified_diff(before, after, path="SKILL.md")
    edits = parse_hunks(diff, patch_id="p1")
    assert len(edits) == 2
    assert edits[0]["old_string"] == "b"
    assert edits[0]["new_string"] == "B"
    assert edits[1]["old_string"] == "e"
    assert edits[1]["new_string"] == "E"


def test_materialize_applies_only_selected_edits() -> None:
    before = "a\nb\nc\nd\ne\n"
    after = "a\nB\nc\nd\nE\n"
    diff = unified_diff(before, after, path="SKILL.md")
    edits = parse_hunks(diff, patch_id="p1")
    partial = materialize_after_from_hunks(before, edits, {edits[0]["hunk_id"]})
    assert partial == "a\nB\nc\nd\ne\n"
    full = materialize_after_from_hunks(before, edits, {e["hunk_id"] for e in edits})
    assert full == after


def test_validate_attaches_hunks() -> None:
    snaps = {
        ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
            skill_name="sql-analyst",
            relative_path="SKILL.md",
            content="step 1\nkeep\nstep 2\n",
        )
    }
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step 1\nkeep\nstep 2\n",
                new_string="step 1x\nkeep\nstep 2y\n",
                reason="一次整文件修订",
            )
        ],
        snapshots=snaps,
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(patches) == 1
    assert len(patches[0]["hunks"]) == 2
    assert patches[0]["hunks"][0]["reason"] == "一次整文件修订"


def test_ensure_patch_hunks_reparses_legacy() -> None:
    before = "a\nb\n"
    after = "a\nB\n"
    patch = {
        "patch_id": "skpatch_1",
        "before": before,
        "after": after,
        "diff": unified_diff(before, after, path="x"),
    }
    hunks = ensure_patch_hunks(patch)
    assert hunks and hunks[0]["old_string"] == "b"
    assert patch["hunks"] is hunks
    assert hunks[0]["old_start"] == 1
    assert " a" in hunks[0]["diff"]
    assert hunks[0]["header"].startswith("edit 1 · L2")


def test_context_snippet_includes_surrounding_lines() -> None:
    before = "a\nb\nc\nd\ne\n"
    after = "a\nB\nc\nd\ne\n"
    diff = unified_diff(before, after, path="SKILL.md")
    edits = parse_hunks(diff, patch_id="p1", before=before)
    assert len(edits) == 1
    hunk = edits[0]
    assert hunk["old_string"] == "b"
    assert hunk["new_string"] == "B"
    assert hunk["old_start"] == 1
    assert "L2" in hunk["header"]
    snippet = hunk["diff"]
    assert snippet.startswith("@@ -1,")
    assert " a" in snippet
    assert "-b" in snippet
    assert "+B" in snippet
    assert " c" in snippet
    assert (
        " d" in snippet
    )  # 3 lines of trailing context from line 2 → c,d,e? end=2, trail 2:5 = c,d,e
    # Apply still uses old/new, not the context window.
    partial = materialize_after_from_hunks(before, edits, {hunk["hunk_id"]})
    assert partial == after


def test_ensure_enriches_legacy_stripped_hunk() -> None:
    before = "keep-a\nchange-me\nkeep-b\nkeep-c\nkeep-d\n"
    patch = {
        "patch_id": "skpatch_legacy",
        "before": before,
        "after": "keep-a\nchanged\nkeep-b\nkeep-c\nkeep-d\n",
        "hunks": [
            {
                "hunk_id": "skpatch_legacy:e0",
                "header": "edit 1",
                "old_string": "change-me",
                "new_string": "changed",
                "diff": "@@ -1,1 +1,1 @@\n-change-me\n+changed",
                "status": "pending",
            }
        ],
    }
    hunks = ensure_patch_hunks(patch)
    assert hunks[0]["old_string"] == "change-me"
    assert hunks[0]["new_string"] == "changed"
    assert hunks[0]["old_start"] == 1
    assert " keep-a" in hunks[0]["diff"]
    assert "-change-me" in hunks[0]["diff"]
    assert "+changed" in hunks[0]["diff"]
    assert " keep-b" in hunks[0]["diff"]
    assert "L2" in hunks[0]["header"]
    applied = materialize_after_from_hunks(before, hunks, {hunks[0]["hunk_id"]})
    assert applied == patch["after"]

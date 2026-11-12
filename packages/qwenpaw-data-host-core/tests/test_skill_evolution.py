# -*- coding: utf-8 -*-
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from qwenpaw_data.host.core.algo.skill_evolution.apply import select_accepted_patches
from qwenpaw_data.host.core.algo.skill_evolution.diffutil import apply_old_new, unified_diff
from qwenpaw_data.host.core.algo.skill_evolution.generator import (
    parse_proposals,
    render_prompt,
)
from qwenpaw_data.host.core.algo.skill_evolution.merge import (
    merge_pending_patches,
    merge_pending_proposal,
)
from qwenpaw_data.host.core.algo.skill_evolution.models import (
    EvolutionProposal,
    ProposedPatch,
    SignalState,
    SkillFileSnapshot,
)
from qwenpaw_data.host.core.algo.skill_evolution.skills_io import (
    SkillCatalog,
    evolvable_catalog,
    load_catalog,
    snapshot_skills,
)
from qwenpaw_data.host.core.algo.skill_evolution.stage_buffer import StageBuffer
from qwenpaw_data.host.core.algo.skill_evolution.tools import _EvolutionTools
from qwenpaw_data.host.core.algo.skill_evolution.trace import build_trace_context
from qwenpaw_data.host.core.algo.skill_evolution.trigger import evaluate_trigger
from qwenpaw_data.host.core.algo.skill_evolution.validate import validate_and_materialize
from qwenpaw_data.host.core.repositories.skill_store import SkillStoreRepo


def _catalog(root: Path, skills: dict[str, dict[str, str]]) -> SkillCatalog:
    for name, files in skills.items():
        for rel, content in files.items():
            path = root / name / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            text = content
            if rel == "SKILL.md" and not content.lstrip().startswith("---"):
                text = f"---\nname: {name}\ndescription: d\n---\n{content}"
            path.write_text(text, encoding="utf-8")
    return load_catalog(SkillStoreRepo(root))

def test_evaluate_trigger_thresholds() -> None:
    assert not evaluate_trigger(
        SignalState(user_feedback_count=0, plan_edit_pending=False)
    )
    assert evaluate_trigger(SignalState(user_feedback_count=1, plan_edit_pending=False))
    assert evaluate_trigger(SignalState(user_feedback_count=0, plan_edit_pending=True))
    assert evaluate_trigger(SignalState(user_feedback_count=1, plan_edit_pending=True))


def test_validate_skips_unknown_skill_and_path_escape() -> None:
    snaps = {
        ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
            skill_name="sql-analyst",
            relative_path="SKILL.md",
            content="# skill\nstep 1\n",
        )
    }
    assert (
        validate_and_materialize(
            [
                ProposedPatch(
                    skill_name="brand-new",
                    relative_path="SKILL.md",
                    old_string="step 1",
                    new_string="step 1 improved",
                )
            ],
            snapshots=snaps,
            existing_skills={"sql-analyst"},
            max_patches=8,
            max_files=12,
        )
        == []
    )
    assert (
        validate_and_materialize(
            [
                ProposedPatch(
                    skill_name="sql-analyst",
                    relative_path="../etc/passwd",
                    old_string="x",
                    new_string="y",
                )
            ],
            snapshots=snaps,
            existing_skills={"sql-analyst"},
            max_patches=8,
            max_files=12,
        )
        == []
    )
    mixed = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="brand-new",
                relative_path="SKILL.md",
                old_string="step 1",
                new_string="step 1 improved",
                reason="未知 skill",
            ),
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step 1",
                new_string="step 1\nstep 2",
                reason="补步骤",
            ),
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="missing.md",
                old_string="x",
                new_string="y",
                reason="不存在的文件",
            ),
        ],
        snapshots=snaps,
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(mixed) == 1
    assert mixed[0]["relative_path"] == "SKILL.md"
    assert "step 2" in mixed[0]["after"]


def test_validate_skips_failed_edit_keeps_other_hunk() -> None:
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step 1",
                new_string="step 1\nstep 2",
                reason="补第二步",
            ),
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="does-not-exist",
                new_string="nope",
                reason="对不上",
            ),
        ],
        snapshots={
            ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                content="# skill\nstep 1\n",
            )
        },
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(patches) == 1
    assert "step 2" in patches[0]["after"]
    assert "nope" not in patches[0]["after"]


def test_validate_create_then_patch_same_new_file() -> None:
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                old_string=None,
                new_string="select 1\n",
                create_file=True,
                reason="先建示例",
            ),
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                old_string="select 1",
                new_string="select 2",
                reason="再改示例",
            ),
        ],
        snapshots={},
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(patches) == 1
    assert patches[0]["before"] == ""
    assert "select 2" in patches[0]["after"]
    assert len(patches[0]["hunks"]) >= 2


def test_validate_and_apply_skill_md_and_references() -> None:
    snaps = {
        ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
            skill_name="sql-analyst",
            relative_path="SKILL.md",
            content="# skill\nstep 1\n",
        ),
        ("sql-analyst", "references/tips.md"): SkillFileSnapshot(
            skill_name="sql-analyst",
            relative_path="references/tips.md",
            content="old tip\n",
        ),
    }
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step 1",
                new_string="step 1\nstep 2",
            ),
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="references/tips.md",
                old_string="old tip",
                new_string="new tip",
            ),
        ],
        snapshots=snaps,
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(patches) == 2
    assert "step 2" in patches[0]["after"]
    assert patches[0]["diff"]
    assert "new tip" in patches[1]["after"]


def test_apply_old_new_and_unified_diff() -> None:
    after = apply_old_new("hello world", "world", "there")
    assert after == "hello there"
    diff = unified_diff("a\n", "b\n", path="SKILL.md")
    assert "-a" in diff and "+b" in diff


def test_snapshot_and_list_skills(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        {
            "metric-drill": {
                "SKILL.md": "name: metric-drill\n",
                "examples/query.md": "select 1\n",
            }
        },
    )
    pycache = tmp_path / "metric-drill" / "__pycache__"
    pycache.mkdir()
    (pycache / "x.pyc").write_bytes(b"\x00")
    catalog = load_catalog(SkillStoreRepo(tmp_path))
    assert catalog.names == frozenset({"metric-drill"})
    snaps = snapshot_skills(catalog, preferred=["metric-drill"])
    assert snaps[0].skill_name == "metric-drill"
    assert snaps[0].relative_path == "SKILL.md"
    assert any(item.relative_path == "examples/query.md" for item in snaps)
    assert not any("__pycache__" in item.relative_path for item in snaps)


def test_snapshot_prefers_all_skill_md_before_support_files(tmp_path: Path) -> None:
    extras = {f"examples/n{i}.md": f"extra {i}\n" for i in range(8)}
    catalog = _catalog(
        tmp_path,
        {
            "aaa-first": {"SKILL.md": "# first\n", **extras},
            "bi-report-generation": {"SKILL.md": "# report\nstep 1\n"},
        },
    )
    snaps = snapshot_skills(catalog, preferred=["aaa-first"], max_files=4)
    paths = {(item.skill_name, item.relative_path) for item in snaps}
    assert ("bi-report-generation", "SKILL.md") in paths
    assert ("aaa-first", "SKILL.md") in paths


def test_validate_accepts_examples_resource() -> None:
    snaps = {
        ("sql-analyst", "examples/query.md"): SkillFileSnapshot(
            skill_name="sql-analyst",
            relative_path="examples/query.md",
            content="old query\n",
        )
    }
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                old_string="old query",
                new_string="new query",
                reason="用户要求改示例",
            )
        ],
        snapshots=snaps,
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(patches) == 1
    assert patches[0]["relative_path"] == "examples/query.md"
    assert "new query" in patches[0]["after"]


def test_write_file_stages_examples_rejects_skill_md(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, {"sql-analyst": {"SKILL.md": "# skill\n"}})
    skill = tmp_path / "sql-analyst"
    buffer = StageBuffer()
    tools = _EvolutionTools(catalog=catalog, buffer=buffer)

    async def scenario() -> None:
        viewed = await tools.skill_view(name="sql-analyst", file_path="SKILL.md")
        assert viewed.state.value == "success"
        blocked = await tools.skill_propose_write_file(
            name="sql-analyst",
            file_path="SKILL.md",
            content="# rewritten\n",
            reason="用户要求改正文",
        )
        assert blocked.state.value == "error"
        assert "SKILL.md" in blocked.content[0].text
        staged = await tools.skill_propose_write_file(
            name="sql-analyst",
            file_path="examples/query.md",
            content="select 1\n",
            reason="用户要求补一个查询示例",
        )
        assert staged.state.value == "success"
        assert buffer.patches[0].relative_path == "examples/query.md"
        assert buffer.patches[0].create_file is True
        assert not (skill / "examples" / "query.md").exists()

        patched = await tools.skill_propose_patch(
            name="sql-analyst",
            file_path="examples/query.md",
            old_string="select 1",
            new_string="select 2",
            reason="用户要求改成 select 2",
        )
        assert patched.state.value == "success"
        assert buffer.current_text("sql-analyst", "examples/query.md") == "select 2\n"
        assert not (skill / "examples" / "query.md").exists()

    asyncio.run(scenario())


def test_tools_second_patch_uses_composed_buffer(tmp_path: Path) -> None:
    original = "---\nname: sql-analyst\ndescription: d\n---\n# skill\nstep 1\n"
    catalog = _catalog(tmp_path, {"sql-analyst": {"SKILL.md": original}})
    skill = tmp_path / "sql-analyst"
    buffer = StageBuffer()
    tools = _EvolutionTools(catalog=catalog, buffer=buffer)

    async def scenario() -> None:
        viewed = await tools.skill_view(name="sql-analyst", file_path="SKILL.md")
        assert viewed.state.value == "success"
        first = await tools.skill_propose_patch(
            name="sql-analyst",
            file_path="SKILL.md",
            old_string="step 1",
            new_string="step 1\nstep 2",
            reason="用户要求补第二步",
        )
        assert first.state.value == "success"
        second = await tools.skill_propose_patch(
            name="sql-analyst",
            file_path="SKILL.md",
            old_string="step 2",
            new_string="step 2\nstep 3",
            reason="用户要求再补第三步",
        )
        assert second.state.value == "success"
        assert buffer.current_text("sql-analyst", "SKILL.md") == (
            "---\nname: sql-analyst\ndescription: d\n---\n# skill\nstep 1\nstep 2\nstep 3\n"
        )
        viewed_again = await tools.skill_view(name="sql-analyst", file_path="SKILL.md")
        assert "step 3" in viewed_again.content[0].text
        assert (skill / "SKILL.md").read_text(encoding="utf-8") == original

    asyncio.run(scenario())


def test_build_trace_and_render_prompt() -> None:
    trace = build_trace_context(
        chat_id="chat_1",
        segments=[
            {
                "title": "用 sql-analyst 查数",
                "behavior": "调用 sql-analyst",
                "conclusion": "完成",
            }
        ],
        biz_events=[{"title": "执行 sql-analyst"}],
        feedback_texts=["请按周汇总", "再拆到渠道"],
        plan_edit_pending=True,
    )
    assert "sql-analyst" in trace.skills_used
    prompt = render_prompt(
        trace=trace,
        skill_files=[
            SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                content="# skill\n",
            )
        ],
    )
    assert "计划修改" in prompt
    assert "sql-analyst" in prompt
    assert "再拆到渠道" in prompt
    # Hermes-style policy keywords (Chinese tool-oriented prompt).
    assert "正在使用的 skill" in prompt or "skills_used" in prompt
    assert "不要捕获" in prompt
    assert "skill_view" in prompt
    assert "skill_evolution_done" in prompt


def test_parse_proposals() -> None:
    summary, patches = parse_proposals(
        {
            "summary": "调整工作流",
            "patches": [
                {
                    "skill_name": "sql-analyst",
                    "relative_path": "SKILL.md",
                    "old_string": "a",
                    "new_string": "b",
                    "create_file": False,
                }
            ],
        }
    )
    assert summary == "调整工作流"
    assert patches[0].new_string == "b"


def test_select_accepted_patches_by_hunk() -> None:
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step 1",
                new_string="step 1\nstep 2",
                reason="补步骤",
            )
        ],
        snapshots={
            ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                content="# skill\nstep 1\n",
            )
        },
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    hunk_id = patches[0]["hunks"][0]["hunk_id"]
    accepted = select_accepted_patches(patches, hunk_ids=[hunk_id])
    assert accepted[0]["status"] == "accepted"
    assert "step 2" in accepted[0]["after"]
    with pytest.raises(ValueError, match="no hunks selected"):
        select_accepted_patches(patches, hunk_ids=[])


def _file_patch(*, old: str, new: str, reason: str) -> list[dict]:
    return validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string=old,
                new_string=new,
                reason=reason,
            )
        ],
        snapshots={
            ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                content="step 1\nkeep\nstep 2\n",
            )
        },
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )


def test_merge_adds_non_overlapping_hunk_keeps_sibling_file() -> None:
    prior_skill = _file_patch(old="step 1", new="step 1x", reason="改第一步")
    prior_extra = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                old_string="old query",
                new_string="new query",
                reason="改示例",
            )
        ],
        snapshots={
            ("sql-analyst", "examples/query.md"): SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                content="old query\n",
            )
        },
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    incoming = _file_patch(old="step 2", new="step 2y", reason="改第二步")
    merged = merge_pending_patches(prior_skill + prior_extra, incoming)
    by_path = {item["relative_path"]: item for item in merged}
    assert set(by_path) == {"SKILL.md", "examples/query.md"}
    assert "step 1x" in by_path["SKILL.md"]["after"]
    assert "step 2y" in by_path["SKILL.md"]["after"]
    assert len(by_path["SKILL.md"]["hunks"]) == 2
    assert by_path["SKILL.md"]["patch_id"] == prior_skill[0]["patch_id"]
    assert by_path["examples/query.md"]["after"] == prior_extra[0]["after"]


def test_merge_replaces_overlapping_hunk() -> None:
    prior = _file_patch(old="step 1", new="step 1a", reason="先改")
    incoming = _file_patch(old="step 1", new="step 1b", reason="后改")
    merged = merge_pending_patches(prior, incoming)
    assert len(merged) == 1
    assert "step 1b" in merged[0]["after"]
    assert "step 1a" not in merged[0]["after"]
    assert merged[0]["patch_id"] == prior[0]["patch_id"]
    assert incoming[0]["hunks"][0]["hunk_id"] in {
        hunk["hunk_id"] for hunk in merged[0]["hunks"]
    }


def test_merge_empty_incoming_keeps_prior() -> None:
    prior = EvolutionProposal(
        summary="已暂存",
        skills=["sql-analyst"],
        patch_count=1,
        patches=_file_patch(old="step 1", new="step 1x", reason="改第一步"),
    )
    incoming = EvolutionProposal(
        summary="无需修改", skills=[], patch_count=0, patches=[]
    )
    merged = merge_pending_proposal(prior, incoming)
    assert merged.patches == prior.patches
    assert merged.summary == "已暂存"


def test_merge_skips_extra_new_files_over_cap() -> None:
    prior = _file_patch(old="step 1", new="step 1x", reason="改第一步")
    extra = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                old_string="old query",
                new_string="new query",
                reason="新文件",
            )
        ],
        snapshots={
            ("sql-analyst", "examples/query.md"): SkillFileSnapshot(
                skill_name="sql-analyst",
                relative_path="examples/query.md",
                content="old query\n",
            )
        },
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    merged = merge_pending_patches(prior, extra, max_files=1)
    assert [item["relative_path"] for item in merged] == ["SKILL.md"]


def test_evolvable_catalog_drops_builtin_and_patch_is_rejected(tmp_path: Path) -> None:
    _catalog(
        tmp_path,
        {
            "fetch-data": {"SKILL.md": "# fetch\n"},
            "my-note": {"SKILL.md": "# note\n"},
        },
    )
    (tmp_path / "fetch-data" / ".qwenpaw-data-builtin").write_text("", encoding="utf-8")
    full = load_catalog(SkillStoreRepo(tmp_path))
    assert "fetch-data" in full.names
    assert next(item.source for item in full.infos if item.name == "fetch-data") == (
        "builtin"
    )
    catalog = evolvable_catalog(full)
    assert set(catalog.names) == {"my-note"}
    assert catalog.get("fetch-data", "SKILL.md") is None

    tools = _EvolutionTools(catalog=catalog, buffer=StageBuffer())

    async def scenario() -> None:
        listed = await tools.skills_list()
        text = listed.content[0].text
        assert "my-note" in text
        assert "fetch-data" not in text
        blocked = await tools.skill_propose_patch(
            name="fetch-data",
            file_path="SKILL.md",
            old_string="# fetch",
            new_string="# fetch\nmore",
            reason="用户要求补一步",
        )
        assert blocked.state.value == "error"
        assert "unknown skill" in blocked.content[0].text

    asyncio.run(scenario())

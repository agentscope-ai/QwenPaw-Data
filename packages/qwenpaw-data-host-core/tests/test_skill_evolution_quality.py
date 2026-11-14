# -*- coding: utf-8 -*-
"""Quality upgrades: transcript, skills_used, fuzzy patch, stage tools, fork."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from qwenpaw_data.host.core.algo.skill_evolution.diffutil import (
    apply_old_new,
    fuzzy_find_and_replace,
)
from qwenpaw_data.host.core.algo.skill_evolution.generator import render_review_prompt
from qwenpaw_data.host.core.algo.skill_evolution.models import (
    EvolutionProposal,
    ProposedPatch,
    SignalState,
    SkillFileSnapshot,
)
from qwenpaw_data.host.core.algo.skill_evolution.propose import propose_evolution
from qwenpaw_data.host.core.algo.skill_evolution.skills_io import SkillCatalog, load_catalog
from qwenpaw_data.host.core.algo.skill_evolution.skills_used import extract_skills_used
from qwenpaw_data.host.core.algo.skill_evolution.stage_buffer import StageBuffer
from qwenpaw_data.host.core.algo.skill_evolution.tools import _EvolutionTools
from qwenpaw_data.host.core.algo.skill_evolution.transcript import (
    HistoryTurn,
    digest_history,
    messages_to_history,
)
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

def _msg(
    *,
    type: str,
    role: str,
    content: list[dict[str, Any]],
    status: str = "completed",
    chat_id: str = "c1",
    user_input: str = "",
) -> dict[str, Any]:
    return {
        "object": "message",
        "type": type,
        "role": role,
        "status": status,
        "content": content,
        "_chat_id": chat_id,
        "_chat_user_input": user_input,
    }


def test_messages_to_history_ordering_and_user_input() -> None:
    messages = [
        _msg(
            type="message",
            role="assistant",
            content=[{"type": "text", "text": "先回答"}],
            chat_id="c1",
            user_input="查一下指标",
        ),
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "Skill",
                        "arguments": '{"skill":"sql-analyst"}',
                    },
                }
            ],
            chat_id="c1",
            user_input="查一下指标",
        ),
        _msg(
            type="message",
            role="user",
            content=[{"type": "text", "text": "再拆渠道"}],
            chat_id="c2",
            user_input="再拆渠道",
        ),
    ]
    turns = messages_to_history(messages, chat_user_inputs=["查一下指标", "再拆渠道"])
    assert turns[0].role == "user"
    assert turns[0].content == "查一下指标"
    assert any(t.content.startswith("[tool_call] Skill") for t in turns)
    assert turns[-1].content == "再拆渠道"


def test_messages_to_history_keeps_tool_errors_only() -> None:
    messages = [
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {"name": "execute_read", "arguments": '{"sql":"select 1"}'},
                }
            ],
            chat_id="c1",
            user_input="查一下",
        ),
        _msg(
            type="plugin_call_output",
            role="tool",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "execute_read",
                        "output": "gmv,100\n" * 40,
                        "state": "success",
                    },
                }
            ],
            chat_id="c1",
            user_input="查一下",
        ),
        _msg(
            type="plugin_call_output",
            role="tool",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "execute_read",
                        "output": "ODPS-0130071: column foo does not exist",
                        "state": "error",
                    },
                }
            ],
            chat_id="c1",
            user_input="查一下",
        ),
    ]
    turns = messages_to_history(messages, chat_user_inputs=["查一下"])
    results = [t.content for t in turns if t.content.startswith("[tool_")]
    assert any(c.startswith("[tool_call] execute_read") for c in results)
    assert any(
        c.startswith("[tool_error] execute_read:") and "column foo does not exist" in c
        for c in results
    )
    assert not any("[tool_result]" in c or "gmv,100" in c for c in results)


def test_digest_history_keeps_tail() -> None:
    turns = [
        HistoryTurn(role="user", content=("old-" + str(i)) * 200) for i in range(10)
    ] + [
        HistoryTurn(role="user", content="recent-user"),
        HistoryTurn(role="assistant", content="recent-assistant"),
    ]
    out = digest_history(turns, max_chars=500, tail_turns=2)
    assert out[-1].content == "recent-assistant"
    assert out[0].content.startswith("[Earlier conversation digest")
    assert "old-0" in out[0].content or "USER:" in out[0].content


def test_extract_skills_used_from_skill_and_read() -> None:
    messages = [
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "Skill",
                        "arguments": '{"skill":"sql-analyst"}',
                    },
                }
            ],
        ),
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "Read",
                        "arguments": {
                            "file_path": "workspace/skills/metric-drill/SKILL.md"
                        },
                    },
                }
            ],
        ),
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {"name": "execute_read", "arguments": '{"sql":"select 1"}'},
                }
            ],
        ),
    ]
    used = extract_skills_used(messages)
    assert used == ["sql-analyst", "metric-drill"]


def test_fuzzy_exact_and_whitespace_and_indent() -> None:
    content = "line1\n  step one  \n\n\nline3\n"
    # Exact
    new_c, old, new, strategy, err = fuzzy_find_and_replace(content, "line1", "LINE1")
    assert err is None and strategy == "exact"
    assert "LINE1" in new_c and old == "line1"

    # Trailing space / blank-line normalize
    content2 = "# title\nstep one\n\n\nnext\n"
    needle = "# title\nstep one  \n\nnext"
    new_c, old, new, strategy, err = fuzzy_find_and_replace(
        content2, needle, "# title\nstep one\n\nnext\nextra"
    )
    assert err is None
    assert strategy in ("whitespace_normalized", "exact", "indentation_flexible")

    # Indent mismatch
    content3 = "root:\n    - item\n    - other\n"
    new_c, old, new, strategy, err = fuzzy_find_and_replace(
        content3,
        "root:\n  - item\n  - other",
        "root:\n  - item\n  - other\n  - third",
    )
    assert err is None
    assert strategy in ("indentation_flexible", "whitespace_normalized", "block_anchor")
    assert "- third" in new_c or "third" in new


def test_fuzzy_ambiguous_fails_closed() -> None:
    content = "aaa\nbbb\naaa\n"
    _c, _o, _n, strategy, err = fuzzy_find_and_replace(content, "aaa", "zzz")
    assert strategy is None
    assert err is not None
    assert "matches" in err.lower() or "Could not find" in err


def test_fuzzy_block_anchor() -> None:
    content = "\n".join(
        [
            "intro",
            "START HERE",
            "middle A",
            "middle B",
            "END HERE",
            "outro",
        ]
    )
    needle = "START HERE\nmiddle X\nmiddle Y\nEND HERE"
    new_c, old, new, strategy, err = fuzzy_find_and_replace(
        content, needle, "START HERE\nmiddle Z\nEND HERE"
    )
    assert err is None
    assert strategy == "block_anchor"
    assert "middle Z" in new_c
    assert "middle A" not in new_c


def test_tools_view_required_unknown_refused_no_disk_write(tmp_path: Path) -> None:
    original = "---\nname: sql-analyst\ndescription: d\n---\n# skill\nstep 1\n"
    catalog = _catalog(tmp_path, {"sql-analyst": {"SKILL.md": original}})
    skill_md = tmp_path / "sql-analyst" / "SKILL.md"
    buffer = StageBuffer()
    tools = _EvolutionTools(catalog=catalog, buffer=buffer)

    async def scenario() -> None:
        bad = await tools.skill_propose_patch(
            name="sql-analyst",
            file_path="SKILL.md",
            old_string="step 1",
            new_string="step 1\nstep 2",
            reason="用户要求补充步骤",
        )
        assert bad.state.value == "error"
        assert "skill_view" in bad.content[0].text

        unknown = await tools.skill_view(name="no-such-skill")
        assert unknown.state.value == "error"

        viewed = await tools.skill_view(name="sql-analyst", file_path="SKILL.md")
        assert viewed.state.value == "success"

        missing_reason = await tools.skill_propose_patch(
            name="sql-analyst",
            file_path="SKILL.md",
            old_string="step 1",
            new_string="step 1\nstep 2",
            reason="",
        )
        assert missing_reason.state.value == "error"
        assert "reason" in missing_reason.content[0].text

        staged = await tools.skill_propose_patch(
            name="sql-analyst",
            file_path="SKILL.md",
            old_string="step 1",
            new_string="step 1\nstep 2",
            reason="用户要求按周汇总，需补充分批步骤",
        )
        assert staged.state.value == "success"
        assert len(buffer.patches) == 1
        assert buffer.patches[0].reason.startswith("用户要求")
        # Disk unchanged
        assert skill_md.read_text(encoding="utf-8") == original

        done = await tools.skill_evolution_done(summary="补充步骤")
        assert done.state.value == "success"
        assert buffer.done and buffer.summary == "补充步骤"

    asyncio.run(scenario())


def test_validate_fuzzy_materialize(tmp_path: Path) -> None:
    content = "# skill\n  step one\n"
    snaps = {
        ("sql-analyst", "SKILL.md"): SkillFileSnapshot(
            skill_name="sql-analyst",
            relative_path="SKILL.md",
            content=content,
        )
    }
    patches = validate_and_materialize(
        [
            ProposedPatch(
                skill_name="sql-analyst",
                relative_path="SKILL.md",
                old_string="step one",  # exact substring still works
                new_string="step one\nstep two",
                reason="对话纠正：需补充第二步",
            )
        ],
        snapshots=snaps,
        existing_skills={"sql-analyst"},
        max_patches=8,
        max_files=12,
    )
    assert len(patches) == 1
    assert "step two" in patches[0]["after"]
    assert patches[0]["hunks"][0]["reason"] == "对话纠正：需补充第二步"
    # Legacy exact helper still available
    assert apply_old_new("a b", "b", "c") == "a c"


def test_render_review_prompt_policy_keywords() -> None:
    prompt = render_review_prompt(
        feedback_texts=["改计划步骤"],
        skills_used=["sql-analyst"],
        plan_edit_note="本 Chat 存在用户计划修改信号",
    )
    assert "正在使用的 skill" in prompt
    assert "不要捕获" in prompt
    assert "未验证的失败路径" in prompt or "失败路径" in prompt
    assert "指标定义" in prompt and "计算口径" in prompt
    assert "skill_evolution_done" in prompt
    assert "reason" in prompt
    assert "sql-analyst" in prompt
    assert "首次" in prompt
    assert "本 Session 迄今" in prompt
    assert "已暂存提案" in prompt


def test_render_review_prompt_incremental_prior_pending_digest() -> None:
    prior = EvolutionProposal(
        summary="补步骤",
        skills=["sql-analyst"],
        patch_count=1,
        patches=[
            {
                "skill_name": "sql-analyst",
                "relative_path": "SKILL.md",
                "before": "SHOULD_NOT_APPEAR_BEFORE",
                "after": "SHOULD_NOT_APPEAR_AFTER",
                "reason": "文件级理由",
                "hunks": [
                    {
                        "hunk_id": "skpatch_1:e0",
                        "header": "@@ -1,2 +1,3 @@",
                        "reason": "用户要求补第二步",
                    }
                ],
            }
        ],
    )
    prompt = render_review_prompt(
        feedback_texts=["再补一步"],
        skills_used=["sql-analyst"],
        incremental=True,
        prior_pending=prior,
    )
    assert "增量" in prompt
    assert "本轮" in prompt
    assert "skill_view" in prompt
    assert "只输出新增" in prompt or "Host 会保留" in prompt
    assert "补步骤" in prompt
    assert "sql-analyst/SKILL.md" in prompt
    assert "skpatch_1:e0" in prompt
    assert "@@ -1,2 +1,3 @@" in prompt
    assert "用户要求补第二步" in prompt
    assert "SHOULD_NOT_APPEAR_BEFORE" not in prompt
    assert "SHOULD_NOT_APPEAR_AFTER" not in prompt


def test_render_review_prompt_empty_prior_pending_is_placeholder() -> None:
    prompt = render_review_prompt(
        feedback_texts=[],
        skills_used=[],
        incremental=True,
        prior_pending=None,
    )
    assert "增量" in prompt
    assert "（无）" in prompt


def test_run_evolution_fork_max_iters_constant() -> None:
    from qwenpaw_data.host.core.algo.skill_evolution.fork import FORK_MAX_ITERS

    assert FORK_MAX_ITERS == 32


def _skill_catalog(tmp_path: Path) -> SkillCatalog:
    return _catalog(
        tmp_path,
        {
            "sql-analyst": {
                "SKILL.md": (
                    "---\nname: sql-analyst\ndescription: d\n---\n# skill\nstep 1\n"
                )
            }
        },
    )


def test_propose_evolution_skips_when_signal_not_met(tmp_path: Path) -> None:
    async def scenario() -> None:
        with patch(
            "qwenpaw_data.host.core.algo.skill_evolution.propose.run_evolution_fork",
            new=AsyncMock(side_effect=AssertionError("fork should not run")),
        ):
            result = await propose_evolution(
                model=object(),
                catalog=_skill_catalog(tmp_path),
                messages=[],
                signal=SignalState(user_feedback_count=0, plan_edit_pending=False),
                feedback_texts=["再拆渠道"],
            )
        assert result is None

    asyncio.run(scenario())


def test_propose_evolution_builds_proposal_from_stub_fork(tmp_path: Path) -> None:
    catalog = _skill_catalog(tmp_path)

    async def fake_fork(**_kwargs: Any):
        return (
            "补充按周汇总步骤",
            [
                ProposedPatch(
                    skill_name="sql-analyst",
                    relative_path="SKILL.md",
                    old_string="step 1",
                    new_string="step 1\nstep 2",
                    reason="用户说请按周汇总",
                )
            ],
        )

    async def scenario() -> None:
        with patch(
            "qwenpaw_data.host.core.algo.skill_evolution.propose.run_evolution_fork",
            new=AsyncMock(side_effect=fake_fork),
        ):
            result = await propose_evolution(
                model=object(),
                catalog=catalog,
                messages=[],
                signal=SignalState(user_feedback_count=1, plan_edit_pending=False),
                feedback_texts=["请按周汇总"],
            )
        assert result is not None
        assert result.summary == "补充按周汇总步骤"
        assert result.skills == ["sql-analyst"]
        assert result.patch_count == 1
        assert "step 2" in result.patches[0]["after"]
        assert "step 2" not in (tmp_path / "sql-analyst" / "SKILL.md").read_text(
            encoding="utf-8"
        )

    asyncio.run(scenario())


def test_propose_evolution_loads_missing_snapshot_from_catalog(tmp_path: Path) -> None:
    catalog = _catalog(
        tmp_path,
        {
            "bi-report-generation": {
                "SKILL.md": (
                    "---\nname: bi-report-generation\ndescription: d\n---\n"
                    "# report\nstep 1\n"
                )
            }
        },
    )

    async def fake_fork(**_kwargs: Any):
        return (
            "补充报告步骤",
            [
                ProposedPatch(
                    skill_name="bi-report-generation",
                    relative_path="SKILL.md",
                    old_string="step 1",
                    new_string="step 1\nstep 2",
                    reason="用户反馈",
                )
            ],
        )

    async def scenario() -> None:
        with (
            patch(
                "qwenpaw_data.host.core.algo.skill_evolution.propose.snapshot_skills",
                return_value=[],
            ),
            patch(
                "qwenpaw_data.host.core.algo.skill_evolution.propose.run_evolution_fork",
                new=AsyncMock(side_effect=fake_fork),
            ),
        ):
            result = await propose_evolution(
                model=object(),
                catalog=catalog,
                messages=[],
                signal=SignalState(user_feedback_count=1, plan_edit_pending=False),
                feedback_texts=["补一步"],
            )
        assert result is not None
        assert result.skills == ["bi-report-generation"]
        assert "step 2" in result.patches[0]["after"]

    asyncio.run(scenario())


def test_propose_evolution_keeps_good_file_when_other_missing(tmp_path: Path) -> None:
    catalog = _skill_catalog(tmp_path)

    async def fake_fork(**_kwargs: Any):
        return (
            "部分可用",
            [
                ProposedPatch(
                    skill_name="missing-skill",
                    relative_path="SKILL.md",
                    old_string="x",
                    new_string="y",
                    reason="不存在",
                ),
                ProposedPatch(
                    skill_name="sql-analyst",
                    relative_path="SKILL.md",
                    old_string="step 1",
                    new_string="step 1\nstep 2",
                    reason="用户说请按周汇总",
                ),
            ],
        )

    async def scenario() -> None:
        with patch(
            "qwenpaw_data.host.core.algo.skill_evolution.propose.run_evolution_fork",
            new=AsyncMock(side_effect=fake_fork),
        ):
            result = await propose_evolution(
                model=object(),
                catalog=catalog,
                messages=[],
                signal=SignalState(user_feedback_count=1, plan_edit_pending=False),
                feedback_texts=["请按周汇总"],
            )
        assert result is not None
        assert result.skills == ["sql-analyst"]
        assert result.patch_count == 1

    asyncio.run(scenario())


def test_propose_evolution_forwards_incremental_kwargs(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}
    prior = EvolutionProposal(
        summary="已暂存",
        skills=["sql-analyst"],
        patch_count=1,
        patches=[{"skill_name": "sql-analyst", "relative_path": "SKILL.md"}],
    )

    async def fake_fork(**kwargs: Any):
        captured.update(kwargs)
        return (
            "增量修订",
            [
                ProposedPatch(
                    skill_name="sql-analyst",
                    relative_path="SKILL.md",
                    old_string="step 1",
                    new_string="step 1\nstep 2",
                    reason="本轮纠正",
                )
            ],
        )

    async def scenario() -> None:
        with patch(
            "qwenpaw_data.host.core.algo.skill_evolution.propose.run_evolution_fork",
            new=AsyncMock(side_effect=fake_fork),
        ):
            result = await propose_evolution(
                model=object(),
                catalog=_skill_catalog(tmp_path),
                messages=[],
                signal=SignalState(user_feedback_count=1, plan_edit_pending=False),
                feedback_texts=["再短一点"],
                incremental=True,
                prior_pending=prior,
            )
        assert result is not None
        assert captured["incremental"] is True
        assert captured["prior_pending"] is prior

    asyncio.run(scenario())


def test_propose_evolution_empty_patches_returns_none(tmp_path: Path) -> None:
    async def scenario() -> None:
        with patch(
            "qwenpaw_data.host.core.algo.skill_evolution.propose.run_evolution_fork",
            new=AsyncMock(return_value=("无需修改", [])),
        ):
            result = await propose_evolution(
                model=object(),
                catalog=_skill_catalog(tmp_path),
                messages=[],
                signal=SignalState(user_feedback_count=0, plan_edit_pending=True),
                feedback_texts=[],
                plan_edit_note="本 Chat 存在用户计划修改信号",
            )
        assert result is None

    asyncio.run(scenario())


def test_graph_and_read_calls_keep_their_shape_in_the_history() -> None:
    cypher = (
        "MATCH (m:Metric)-[:HAS_CALIBER]->(c:MetricCaliber)-[:CALIBER_ON]->"
        "(ds:Dataset)-[:AVAILABLE_IN]->(src:DataSource) WHERE m.name = $name "
        "RETURN m.name AS metric, c.expr AS expr, src.datasource_id AS datasource_id"
    )
    messages = [
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "mcp__context-manager__query_graph",
                        "arguments": json.dumps(
                            {"cypher": cypher, "params": {"name": "DAU"}, "limit": 20}
                        ),
                    },
                }
            ],
            chat_id="c1",
            user_input="查 DAU",
        ),
        _msg(
            type="plugin_call",
            role="assistant",
            content=[
                {
                    "type": "data",
                    "data": {
                        "name": "mcp__context-manager__execute_read",
                        "arguments": json.dumps(
                            {"datasource_id": "ds_demo", "sql": "select 1"}
                        ),
                    },
                }
            ],
            chat_id="c1",
            user_input="查 DAU",
        ),
    ]

    turns = messages_to_history(messages, chat_user_inputs=["查 DAU"])
    calls = [t.content for t in turns if t.content.startswith("[tool_call]")]

    assert "MATCH (m:Metric)" in calls[0] and "AVAILABLE_IN" in calls[0]
    assert '"datasource_id": "ds_demo"' in calls[1]


def test_the_evolution_prompt_separates_graph_usage_from_business_facts() -> None:
    from qwenpaw_data.host.core.algo.skill_evolution import generator

    template = (
        Path(generator.__file__).parent / "prompts" / "SKILL_EVOLUTION.md"
    ).read_text(encoding="utf-8")

    assert "`datasource_id`" in template
    assert "图谱 key" in template
    assert "get_graph_schema" in template

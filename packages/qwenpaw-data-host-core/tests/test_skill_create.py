# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path

import pytest

from qwenpaw_data.host.core.algo.skill_create.assets import (
    checklist_for_prompt,
    evidence_guide_for_prompt,
    reference,
)
from qwenpaw_data.host.core.algo.skill_create.digest import (
    build_digest,
    chats_to_messages,
    initial_task_instruction,
)
from qwenpaw_data.host.core.algo.skill_create.extract import extract, first_present
from qwenpaw_data.host.core.algo.skill_create.hub import SkillIndex, SkillRef, uniquify_name
from qwenpaw_data.host.core.algo.skill_create.llm import (
    Decision,
    FileSpec,
    coerce_author_file,
    normalize_file_plan,
)
from qwenpaw_data.host.core.algo.skill_create.manager import SkillCreateManager
from qwenpaw_data.host.core.algo.skill_create.validate import validate_files
from qwenpaw_data.host.core.algo.skill_create.prompts import (
    SYSTEM_SKILL_WRITER,
    WRITE_BOUNDARY,
    author_bundle_prompt,
    author_skill_md_prompt,
    induct_prompt,
)
from qwenpaw_data.host.core.algo.skill_create.trace_export import console_messages_to_trace
from qwenpaw_data.host.core.db.engine import (
    TEST_TENANT_ID,
    TEST_USER_ID,
    TEST_WORKSPACE_ID,
    get_session_factory,
    init_db,
    reset_engine,
)
from qwenpaw_data.host.core.domain.chat import Chat
from qwenpaw_data.host.core.domain.identity import Identity
from qwenpaw_data.host.core.domain.session import Session
from qwenpaw_data.host.core.repositories import (
    ChatRepository,
    SessionRepository,
    SkillDraftRepository,
)
from qwenpaw_data.host.core.settings import clear_settings

TEST_IDENTITY = Identity(
    user_id=TEST_USER_ID,
    tenant_id=TEST_TENANT_ID,
    workspace_id=TEST_WORKSPACE_ID,
)

VALID_SKILL_MD = """---
name: retry-then-summarize
description: 当取数失败后需要按报错收窄查询并再交付摘要时触发。适用于通用失败恢复，不绑定具体业务口径。
---

# retry-then-summarize

## 目标

把一次失败的取数恢复成可复用的重试步骤，而不是把某次任务的取值写进技能。

## 执行步骤

1. 阅读报错首行，判断是权限、语法还是空结果。
2. 按错误类型收窄查询范围后重试一次。
3. 成功后用摘要而不是原始表交付。

## 约束

- 不要把某次任务的具体取值写进步骤。
- 需要其它技能时按技能名引用，不写跨目录路径。
"""

SKILL_WITH_TREND = """---
name: retry-then-summarize
description: 当取数失败后需要按报错收窄查询并再交付摘要时触发。适用于通用失败恢复，不绑定具体业务口径。
---

# retry-then-summarize

## 目标

把一次失败的取数恢复成可复用的重试步骤，而不是把某次任务的取值写进技能。

## 执行步骤

1. 阅读报错首行，判断是权限、语法还是空结果。
2. 按错误类型收窄查询范围后重试一次。
3. 成功后用摘要而不是原始表交付。

使用 `scripts/observe_trend.py`：

```bash
python <skill-dir>/scripts/observe_trend.py --input data.csv --output out.json
```

| 参数 | 说明 | 默认值 |
| ---- | ---- | ---- |
| `--input` | 输入 CSV `(必填)` | - |
| `--output` | 输出 JSON `(必填)` | - |

## fallback

脚本失败时用标准库手工汇总。

## 约束

- 不要把某次任务的具体取值写进步骤。
"""

SCRIPT_TREND = """#!/usr/bin/env python3
import argparse
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump({"input": args.input}, handle)

if __name__ == "__main__":
    main()
"""

CONSOLE_MESSAGES = [
    {
        "type": "message",
        "role": "user",
        "status": "completed",
        "content": [{"type": "text", "text": "对比漏斗转化"}],
    },
    {
        "type": "plugin_call",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "Skill",
                    "call_id": "c1",
                    "arguments": {"skill": "fetch-data"},
                },
            }
        ],
    },
    {
        "type": "plugin_call",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "Read",
                    "call_id": "c2",
                    "arguments": {"file_path": "skills/fetch-data/SKILL.md"},
                },
            }
        ],
    },
    {
        "type": "plugin_call",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "context-manager__query_graph",
                    "call_id": "c2g",
                    "arguments": {
                        "cypher": (
                            "MATCH (m:Metric)-[:HAS_CALIBER]->(c:MetricCaliber)"
                            "-[:CALIBER_ON]->(ds:Dataset) WHERE m.name = $name "
                            "RETURN c.expr AS expr"
                        ),
                        "params": {"name": "漏斗转化率"},
                    },
                },
            }
        ],
    },
    {
        "type": "plugin_call_output",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "context-manager__query_graph",
                    "call_id": "c2g",
                    "output": '{"columns": ["expr"], "rows": [], "row_count": 0}',
                },
            }
        ],
    },
    {
        "type": "plugin_call",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "context-manager__execute_read",
                    "call_id": "c3",
                    "arguments": {"datasource_id": "ds_demo", "sql": "SELECT 1"},
                },
            }
        ],
    },
    {
        "type": "plugin_call_output",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "context-manager__execute_read",
                    "call_id": "c3",
                    "output": (
                        '{"columns": ["x"], "rows": [{"x": 1}], "row_count": 1, '
                        '"total_row_count": 1200, "truncated": true}'
                    ),
                },
            }
        ],
    },
    {
        "type": "plugin_call",
        "status": "completed",
        "content": [
            {
                "type": "data",
                "data": {
                    "name": "Write",
                    "call_id": "c4",
                    "arguments": {"file_path": "artifacts/funnel.md"},
                },
            }
        ],
    },
    {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "text", "text": "已交付漏斗对比摘要"}],
    },
]


class ScriptedWriter:
    def __init__(
        self,
        *,
        induct: list[tuple[Decision, str] | BaseException] | None = None,
        authors: list[dict[str, str] | BaseException] | None = None,
    ) -> None:
        self.induct_prompts: list[str] = []
        self.author_prompts: list[str] = []
        self.author_paths: list[str] = []
        self._induct = list(
            induct
            or [
                (
                    Decision(
                        reason="new workflow",
                        skill_name="retry-then-summarize",
                    ),
                    "# card\n可复用的失败恢复步骤",
                )
            ]
        )
        self._authors = list(authors or [{"SKILL.md": VALID_SKILL_MD}])

    async def induct(self, prompt: str) -> tuple[Decision, str]:
        self.induct_prompts.append(prompt)
        item = self._induct.pop(0) if len(self._induct) > 1 else self._induct[0]
        if isinstance(item, BaseException):
            raise item
        return item

    async def author_file(
        self,
        prompt: str,
        *,
        expected_path: str,
        timeout: float | None = None,
    ) -> dict[str, str]:
        self.author_prompts.append(prompt)
        self.author_paths.append(expected_path)
        if not self._authors:
            return {expected_path: VALID_SKILL_MD}
        item = self._authors.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def test_console_messages_extract_digest_channels() -> None:
    messages = console_messages_to_trace(CONSOLE_MESSAGES)
    evidence = extract(messages)
    digest = build_digest(messages, task_instruction="对比漏斗转化")

    assert evidence["task_instruction"] == "对比漏斗转化"
    assert any(
        "skills/fetch-data/SKILL.md" in key for key in evidence["skills_consulted"]
    )
    assert any(name.endswith("execute_read") for name in evidence["tool_profile"])
    assert evidence["sql_success"]
    assert evidence["sql_success"][0]["row_count"] == 1200
    graph_steps = [
        step
        for seg in evidence["segments"]
        for step in seg["tools"]
        if "query_graph" in step
    ]
    assert graph_steps == ["context-manager__query_graph[Metric>MetricCaliber>Dataset]"]
    assert "漏斗转化率" not in digest.markdown.split("## 4.")[1].split("## 5.")[0]
    assert "artifacts/funnel.md" in evidence["deliverables"]
    assert "已交付漏斗对比摘要" in evidence["final_summary"]
    assert "初始任务指令" in digest.markdown
    assert "局部失败（仅作 pitfall 候选，不是技能主题）" in digest.markdown
    assert (
        "context-manager__execute_read" in digest.markdown
        or "execute_read" in digest.markdown
    )


def test_legacy_preview_row_count_is_still_read() -> None:
    keys = ("total_row_count", "row_count", "preview_row_count")
    assert first_present({"preview_row_count": 3}, *keys) == 3
    assert first_present({"row_count": 0, "total_row_count": 9}, *keys) == 9
    assert first_present({"row_count": 0}, *keys) == 0


def test_write_boundary_keeps_graph_identity_out_of_a_skill() -> None:
    assert "datasource_id" in WRITE_BOUNDARY
    assert "Cypher" in WRITE_BOUNDARY


def test_initial_task_instruction_is_first_chat_user_input() -> None:
    first = Chat.start(
        session_id="ses_demo",
        identity=TEST_IDENTITY,
        sequence=1,
        datasource_id="ds_demo",
        text="分析十二月 DAU 趋势，按下国家拆",
    )
    later = Chat.start(
        session_id="ses_demo",
        identity=TEST_IDENTITY,
        sequence=4,
        datasource_id="ds_demo",
        text="让数据报告变得简约、清晰",
    )
    first.mark_status("completed")
    later.mark_status("completed")
    history = [later, first]
    assert initial_task_instruction(history) == "分析十二月 DAU 趋势，按下国家拆"

    def _user(text: str) -> dict:
        return {
            "type": "message",
            "role": "user",
            "status": "completed",
            "content": [{"type": "text", "text": text}],
        }

    messages = chats_to_messages(
        history,
        {
            first.id: [_user("再按城市拆一下")],
            later.id: [_user("字号再大一点")],
        },
    )
    digest = build_digest(
        messages,
        task_instruction=initial_task_instruction(history),
    )
    assert digest.task_instruction == "分析十二月 DAU 趋势，按下国家拆"
    assert digest.payload["task_instruction"] == "分析十二月 DAU 趋势，按下国家拆"
    assert "分析十二月 DAU 趋势，按下国家拆" in digest.markdown
    assert "让数据报告变得简约、清晰" in digest.markdown
    assert "再按城市拆一下" in digest.markdown
    assert "字号再大一点" in digest.markdown
    section = digest.markdown.split("## 2.", 1)[0]
    assert "字号再大一点" not in section
    assert "让数据报告变得简约、清晰" not in section


def test_skill_index_inventory_is_flat() -> None:
    inventory = SkillIndex(
        [
            SkillRef(name="fetch-data", description="取数"),
            SkillRef(name="metric-trend-breakdown", description="趋势下拆"),
        ]
    ).inventory()
    assert inventory == ("- `fetch-data`：取数\n- `metric-trend-breakdown`：趋势下拆")
    assert "routing/" not in inventory
    assert "workflows/" not in inventory
    assert not any(line.startswith("### ") for line in inventory.splitlines())

    live = SkillIndex.from_agent().inventory()
    assert "（`routing/`）" not in live
    assert "（`workflows/`）" not in live
    assert not any(line.startswith("### ") for line in live.splitlines())


def test_induct_prompt_is_process_first_and_has_no_layer() -> None:
    text = induct_prompt(
        capture_boundary="# boundary",
        evidence_guide="# evidence",
        digest="# digest\n## 1. task",
        skill_inventory="- `fetch-data`",
        task_query="示例站点2025年12月的访问趋势分析，访问指标只看DAU，维度下拆只看国家",
    )
    assert "layer-classification" not in text
    assert "不要做层级判定" not in text
    assert "不要输出层级" not in text
    assert "禁止改写、补丁或建议覆盖任何既有技能正文" in SYSTEM_SKILL_WRITER
    assert "简体中文" in SYSTEM_SKILL_WRITER
    assert "第 1 节任务" in text and "第 4 节工作流骨架" in text
    assert "semantic-layer-fallback" in text
    assert "metric-trend-breakdown" in text
    assert "只新建" in SYSTEM_SKILL_WRITER
    assert "files" in text
    assert "不要撰写任何文件正文" in text
    assert "不要一次输出全部技能文件正文" in SYSTEM_SKILL_WRITER
    assert "<authoring_checklist>" not in text
    author = author_skill_md_prompt(
        skill_card="# card",
        skill_name="metric-trend-breakdown",
        file_plan="- `SKILL.md`",
        effective_guide=reference("effective-skill"),
        checklist=checklist_for_prompt(),
    )
    assert "`name` 必须恰好是 `metric-trend-breakdown`" in author
    assert "整段分析流程" in author
    assert "一次只输出这一个文件" in author
    assert "渐进式披露" in author
    assert "<effective_skill_guide>" in author
    assert "<authoring_checklist>" in author
    assert "简体中文" not in author
    assert "无 SQL 代码块" in author
    assert "git status" not in author
    assert "<meta_skill>" not in text
    assert "<meta_skill>" not in author
    bundle = author_bundle_prompt(
        expected_path="scripts/observe_trend.py",
        purpose="趋势",
        contract="--input --output",
        skill_excerpt="--input --output",
        effective_guide=reference("effective-skill"),
        checklist=checklist_for_prompt(),
    )
    assert "一次只输出这一个文件" in bundle
    assert "argparse" in bundle
    assert "渐进式披露" in bundle
    assert "<effective_skill_guide>" in bundle
    assert "<authoring_checklist>" in bundle
    assert "简体中文" not in bundle
    assert "<skill_card>" not in bundle
    assert "<meta_skill>" not in bundle


def test_prompt_assets_ship_with_skill_create() -> None:
    assert "捕获边界" in reference("capture-boundary")
    assert "第 1 节任务" in reference("evidence-to-skill")
    assert "Skill-Hub" not in reference("effective-skill")
    assert reference("layer-classification") == ""
    guide = evidence_guide_for_prompt()
    assert "技能卡字段" in guide
    assert guide == reference("evidence-to-skill")
    checklist = checklist_for_prompt()
    assert "无 SQL 代码块" in checklist
    assert "简体中文" not in checklist
    assert "## fallback" in checklist
    assert "git status" not in checklist
    assert "grep -nE" not in checklist


def test_validate_files_requires_chinese_human_text() -> None:
    english = """---
name: metric-trend-breakdown
description: Use this skill when the user asks for a metric trend and a single-dimension breakdown.
---

# metric-trend-breakdown

## Goal

Confirm the metric and time window, fetch the series, then deliver a short summary.

## Steps

1. Confirm parameters.
2. Fetch the time series.
3. Break down by one dimension and deliver.
"""
    report = validate_files({"SKILL.md": english}, taken_names=set())
    assert not report.ok
    assert any(
        item.name == "human_text_zh" and not item.passed for item in report.checks
    )

    report = validate_files({"SKILL.md": VALID_SKILL_MD}, taken_names=set())
    assert all(item.passed for item in report.checks if item.name == "human_text_zh")


def test_uniquify_name_avoids_hub_collision() -> None:
    assert uniquify_name("fetch-data", {"fetch-data"}) == "fetch-data-2"


async def _seed_draft() -> tuple[str, str, str]:
    await reset_engine()
    await init_db()
    factory = get_session_factory()
    async with factory() as db:
        session = Session.create(identity=TEST_IDENTITY, title="skill")
        chat = session.open_chat(
            text="对比漏斗转化",
            datasource_id="ds_demo",
            has_active_chat=False,
        )
        chat.mark_status("completed")
        await SessionRepository(db, TEST_IDENTITY).add(session)
        await ChatRepository(db, TEST_IDENTITY).add(chat)
        draft = await SkillDraftRepository(db, TEST_IDENTITY).add(
            session_id=session.id,
            source_chat_id=chat.id,
        )
        await db.commit()
        return session.id, chat.id, draft["id"]


async def _draft_files(session_id: str, draft_id: str) -> dict:
    async with get_session_factory()() as db:
        return await SkillDraftRepository(db, TEST_IDENTITY).get(
            draft_id, session_id=session_id
        )


@pytest.mark.asyncio
async def test_generate_always_creates_skill_even_when_name_overlaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    session_id, chat_id, draft_id = await _seed_draft()
    writer = ScriptedWriter(
        induct=[
            (
                Decision(
                    reason="overlap but still create",
                    skill_name="fetch-data",
                    coverage="full",
                    existing_overlap=["fetch-data"],
                ),
                "# card\n收窄后的取数失败恢复",
            )
        ],
        authors=[
            {"SKILL.md": VALID_SKILL_MD.replace("retry-then-summarize", "fetch-data")}
        ],
    )
    try:
        await SkillCreateManager.generate(
            get_session_factory(),
            identity=TEST_IDENTITY,
            session_id=session_id,
            chat_id=chat_id,
            draft_id=draft_id,
            writer=writer,
        )
        draft = await _draft_files(session_id, draft_id)
        assert draft["status"] == "ready"
        assert "SKILL.md" in draft["files"]
        assert draft["name"] != "fetch-data"
        assert draft["name"].startswith("fetch-data")
        assert f"name: {draft['name']}" in draft["files"]["SKILL.md"]
        assert "evidence/digest.md" in draft["files"]
        assert "decision.json" in draft["files"]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_generate_retries_broken_author_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    session_id, chat_id, draft_id = await _seed_draft()
    writer = ScriptedWriter(
        authors=[
            {"SKILL.md": "not a skill"},
            {"SKILL.md": VALID_SKILL_MD},
        ]
    )
    try:
        await SkillCreateManager.generate(
            get_session_factory(),
            identity=TEST_IDENTITY,
            session_id=session_id,
            chat_id=chat_id,
            draft_id=draft_id,
            writer=writer,
        )
        draft = await _draft_files(session_id, draft_id)
        assert draft["status"] == "ready"
        assert draft["name"] == "retry-then-summarize"
        assert len(writer.author_prompts) == 2
        assert "skill_md_nonempty" in writer.author_prompts[1]
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_generate_fails_only_after_attempt_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    session_id, chat_id, draft_id = await _seed_draft()
    writer = ScriptedWriter(
        authors=[
            {"SKILL.md": "bad-1"},
            {"SKILL.md": "bad-2"},
            {"SKILL.md": "bad-3"},
        ]
    )
    try:
        await SkillCreateManager.generate(
            get_session_factory(),
            identity=TEST_IDENTITY,
            session_id=session_id,
            chat_id=chat_id,
            draft_id=draft_id,
            writer=writer,
        )
        draft = await _draft_files(session_id, draft_id)
        assert draft["status"] == "failed"
        assert "report.md" in draft["files"]
        assert "author SKILL.md attempt 3" in draft["files"]["report.md"]
        assert len(writer.author_prompts) == 3
        assert not any(key.startswith("scripts/") for key in draft["files"])
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_revise_rewrites_skill_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    session_id, _chat_id, draft_id = await _seed_draft()
    revised = VALID_SKILL_MD.replace(
        "把一次失败的取数恢复成可复用的重试步骤",
        "按用户意见把范围收窄到只处理权限类失败",
    )
    writer = ScriptedWriter(authors=[{"SKILL.md": revised}])
    async with get_session_factory()() as db:
        await SkillDraftRepository(db, TEST_IDENTITY).save_files(
            draft_id,
            session_id=session_id,
            files={
                "SKILL.md": VALID_SKILL_MD,
                "evidence/digest.md": "# digest\n对比漏斗",
            },
            status="revising",
        )
        await db.commit()
    try:
        await SkillCreateManager.revise(
            get_session_factory(),
            identity=TEST_IDENTITY,
            session_id=session_id,
            draft_id=draft_id,
            text="收窄到权限失败",
            writer=writer,
        )
        draft = await _draft_files(session_id, draft_id)
        assert draft["status"] == "ready"
        assert "收窄到只处理权限类失败" in draft["files"]["SKILL.md"]
        assert "收窄到权限失败" in writer.author_prompts[0]
        assert "evidence/digest.md" in draft["files"]
        assert writer.author_paths == ["SKILL.md"]
    finally:
        await reset_engine()


def test_normalize_file_plan_caps_and_requires_script_contract() -> None:
    plan = normalize_file_plan(
        [
            FileSpec(path="scripts/a.py", purpose="a", contract="no flags"),
            FileSpec(path="scripts/b.py", purpose="b", contract="--input"),
            FileSpec(path="scripts/c.py", purpose="c", contract="--out"),
            FileSpec(path="scripts/d.py", purpose="d", contract="--x"),
            FileSpec(path="scripts/e.py", purpose="e", contract="--y"),
            FileSpec(path="references/one.md", purpose="r1"),
            FileSpec(path="references/two.md", purpose="r2"),
            FileSpec(path="references/three.md", purpose="r3"),
            FileSpec(path="../escape.py", purpose="bad"),
        ]
    )
    assert plan[0].path == "SKILL.md"
    assert [item.path for item in plan[1:]] == [
        "scripts/b.py",
        "scripts/c.py",
        "scripts/d.py",
        "references/one.md",
        "references/two.md",
    ]


def test_coerce_author_file_accepts_stringified_and_wrapped_payloads() -> None:
    expected = "scripts/observe_trend.py"
    body = "import argparse\n"
    assert coerce_author_file({"path": "wrong.py", "content": body}, expected) == {
        expected: body
    }
    wrapped = json.dumps([{"path": expected, "content": body}])
    assert coerce_author_file({"files": wrapped}, expected) == {expected: body}
    assert coerce_author_file(
        {"files": [{"path": expected, "content": body}]}, expected
    ) == {expected: body}


@pytest.mark.asyncio
async def test_generate_authors_skill_md_then_each_planned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    session_id, chat_id, draft_id = await _seed_draft()
    writer = ScriptedWriter(
        induct=[
            (
                Decision(
                    reason="plan scripts",
                    skill_name="retry-then-summarize",
                    files=[
                        FileSpec(path="SKILL.md", purpose="流程"),
                        FileSpec(
                            path="scripts/observe_trend.py",
                            purpose="趋势",
                            contract="--input --output",
                        ),
                    ],
                ),
                "# card\n带脚本的流程",
            )
        ],
        authors=[
            {"SKILL.md": SKILL_WITH_TREND},
            {"scripts/observe_trend.py": SCRIPT_TREND},
        ],
    )
    try:
        await SkillCreateManager.generate(
            get_session_factory(),
            identity=TEST_IDENTITY,
            session_id=session_id,
            chat_id=chat_id,
            draft_id=draft_id,
            writer=writer,
        )
        draft = await _draft_files(session_id, draft_id)
        assert draft["status"] == "ready"
        assert writer.author_paths == ["SKILL.md", "scripts/observe_trend.py"]
        assert "scripts/observe_trend.py" in draft["files"]
        assert "import argparse" in draft["files"]["scripts/observe_trend.py"]
        decision = json.loads(draft["files"]["decision.json"])
        assert decision["files"][0]["path"] == "SKILL.md"
        assert decision["files"][1]["path"] == "scripts/observe_trend.py"
    finally:
        await reset_engine()


@pytest.mark.asyncio
async def test_generate_drops_failed_bundle_and_syncs_skill_md(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_HOME", str(tmp_path / "home"))
    clear_settings()
    session_id, chat_id, draft_id = await _seed_draft()
    writer = ScriptedWriter(
        induct=[
            (
                Decision(
                    reason="plan scripts",
                    skill_name="retry-then-summarize",
                    files=[
                        FileSpec(
                            path="scripts/observe_trend.py",
                            purpose="趋势",
                            contract="--input --output",
                        )
                    ],
                ),
                "# card\n带脚本的流程",
            )
        ],
        authors=[
            {"SKILL.md": SKILL_WITH_TREND},
            RuntimeError("boom-1"),
            RuntimeError("boom-2"),
            RuntimeError("boom-3"),
            {"SKILL.md": VALID_SKILL_MD},
        ],
    )
    try:
        await SkillCreateManager.generate(
            get_session_factory(),
            identity=TEST_IDENTITY,
            session_id=session_id,
            chat_id=chat_id,
            draft_id=draft_id,
            writer=writer,
        )
        draft = await _draft_files(session_id, draft_id)
        assert draft["status"] == "ready"
        assert "scripts/observe_trend.py" not in draft["files"]
        assert "observe_trend.py" not in draft["files"]["SKILL.md"]
        assert writer.author_paths == [
            "SKILL.md",
            "scripts/observe_trend.py",
            "scripts/observe_trend.py",
            "scripts/observe_trend.py",
            "SKILL.md",
        ]
        assert "dropped scripts/observe_trend.py" in draft["files"]["report.md"]
    finally:
        await reset_engine()


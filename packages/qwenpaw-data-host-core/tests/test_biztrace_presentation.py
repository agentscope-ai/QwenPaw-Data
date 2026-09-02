# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLMError
from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, OrchestrationInfo, Presentation
from qwenpaw_data.host.core.algo.biztrace.presentation import (
    DESCRIPTION_CHAR_CAP,
    THINKING_CHAR_CAP,
    PresentationBuilder,
)


class FakeLLM:
    """Returns canned payloads by schema name, or raises to force a fallback."""

    def __init__(
        self, payloads: dict[str, dict[str, Any]] | None = None, *, fail: bool = False
    ) -> None:
        self.payloads = payloads or {}
        self.fail = fail
        self.calls: list[tuple[str, str, str]] = []

    async def complete(
        self, *, system: str, user: str, schema_name: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        _ = schema
        self.calls.append((schema_name, user, system))
        if self.fail:
            raise StructuredLLMError("model unavailable")
        return self.payloads.get(schema_name, {})


def _tool_use(name: str, payload: Any, **fields: Any) -> BizEvent:
    return BizEvent(
        event_id="call-1",
        kind="tool_use",
        block_id="call-1",
        tool_name=name,
        input=payload,
        **fields,
    )


def _tool_result(name: str, output: Any, **fields: Any) -> BizEvent:
    return BizEvent(
        event_id="call-1:result",
        kind="tool_result",
        block_id="call-1",
        tool_name=name,
        output=output,
        **fields,
    )


async def test_reply_cards_keep_raw_text_and_skip_the_model() -> None:
    llm = FakeLLM({"presentation_body": {"body": "这是摘要"}})
    builder = PresentationBuilder(llm=llm)
    source = "结论是周末效应。\n建议按周复盘。"

    card = await builder.build(
        BizEvent(event_id="t", kind="assistant_text", content=source)
    )

    assert card.card_type == "text"
    assert card.caption == "模型回复"
    assert card.body == source
    assert builder.llm_calls == 0
    assert llm.calls == []


async def test_thinking_cards_keep_key_decisions_in_the_prompt() -> None:
    llm = FakeLLM({"presentation_body": {"body": "取数失败后改查周表"}})
    builder = PresentationBuilder(llm=llm)

    card = await builder.build(
        BizEvent(
            event_id="t",
            kind="assistant_thinking",
            content="SQL 报错缺分区，先改查周表再出结论。",
        )
    )

    assert card.body == "取数失败后改查周表"
    assert builder.llm_calls == 1
    _schema, user, system = llm.calls[0]
    assert "错误分析" in user
    assert "关键决策" in system
    assert "2 句" not in system


async def test_thinking_fallback_keeps_more_than_the_short_card_cap() -> None:
    builder = PresentationBuilder()
    source = "决策" * 300

    card = await builder.build(
        BizEvent(event_id="t", kind="assistant_thinking", content=source)
    )

    assert card.card_type == "thinking"
    assert len(card.body) > DESCRIPTION_CHAR_CAP
    assert len(card.body) <= THINKING_CHAR_CAP


async def test_user_and_text_cards_come_from_rules_without_a_model() -> None:
    builder = PresentationBuilder()

    user = await builder.build(
        BizEvent(event_id="u", kind="user", content="分析日活下降")
    )
    text = await builder.build(
        BizEvent(event_id="t", kind="assistant_text", content="结论是周末效应")
    )

    assert user.card_type == "user"
    assert user.caption == "用户输入"
    assert user.body == "分析日活下降"
    assert text.card_type == "text"
    assert text.body == "结论是周末效应"
    assert builder.llm_calls == 0


async def test_read_of_a_skill_names_it_and_the_result_carries_the_description() -> (
    None
):
    builder = PresentationBuilder()
    call = await builder.build(
        _tool_use("Read", {"file_path": "skills/query-odps/SKILL.md"})
    )
    result = await builder.build(
        _tool_result(
            "Read",
            "---\nname: query-odps\ndescription: 查询 ODPS 表\n---\n\n正文",
        ),
        call_card=call,
    )

    assert call.caption == "读取「query-odps」技能"
    assert result.caption == call.caption
    assert "查询 ODPS 表" in result.body


async def test_skill_viewer_matches_the_read_file_skill_card(
    monkeypatch,
) -> None:
    """AgentScope's Skill tool returns body-only markdown; look up description."""
    import qwenpaw_data.host.core.algo.biztrace.presentation as presentation_module

    monkeypatch.setattr(
        presentation_module,
        "_lookup_skill_description",
        lambda name: "查询 ODPS 表" if name == "query-odps" else None,
    )
    builder = PresentationBuilder()
    call = await builder.build(_tool_use("Skill", {"skill": "query-odps"}))
    result = await builder.build(
        _tool_result(
            "Skill",
            "# query-odps\n\n正文",
            input={"skill": "query-odps"},
        ),
        call_card=call,
    )

    assert call.caption == "读取「query-odps」技能"
    assert call.body == ""
    assert result.caption == call.caption
    assert result.body == "## 技能描述\n\n查询 ODPS 表"
    assert builder.llm_calls == 0


async def test_orchestration_cards_are_templates() -> None:
    builder = PresentationBuilder()

    subtask = await builder.build(
        _tool_use(
            "TaskStateUpdate",
            {"task_id": "n1", "state": "completed"},
            orchestration=OrchestrationInfo(
                category="TaskStateUpdate",
                subtask_state="completed",
                node_name="取数",
            ),
        )
    )

    assert subtask.caption == "更新子任务状态"
    assert subtask.body == "将子任务「取数」的状态更新为 completed。"
    assert builder.llm_calls == 0


async def test_bodies_are_capped_at_the_description_limit() -> None:
    builder = PresentationBuilder(
        llm=FakeLLM({"tool_output_presentation": {"output_summary": "长" * 500}})
    )

    card = await builder.build(_tool_result("Bash", "x" * 5000))

    assert len(card.body) <= DESCRIPTION_CHAR_CAP + len("## Output\n\n")
    assert card.body.endswith("...")


async def test_a_failing_model_falls_back_to_the_template() -> None:
    llm = FakeLLM(fail=True)
    builder = PresentationBuilder(llm=llm)

    card = await builder.build(_tool_use("Bash", {"command": "echo hi"}))

    # Cards name the canonical tool, as they already do for Write / Edit.
    assert card.caption == "调用 execute_shell_command"
    assert "echo hi" in card.body
    assert builder.llm_failures == 1


async def test_the_result_card_reuses_the_call_caption_and_context() -> None:
    llm = FakeLLM({"tool_output_presentation": {"output_summary": "共 12 行"}})
    builder = PresentationBuilder(llm=llm)
    call = Presentation(card_type="tool", caption="查询日活", body="## Input\n\nSQL")

    card = await builder.build(_tool_result("Bash", "12 rows"), call_card=call)

    assert card.caption == "查询日活"
    assert "共 12 行" in card.body
    assert "## Input\n\nSQL" in llm.calls[0][1]
    assert "只填写 output_summary" in llm.calls[0][1]


async def test_output_summary_strips_echoed_call_card_fields() -> None:
    llm = FakeLLM(
        {
            "tool_output_presentation": {
                "output_summary": (
                    "卡片小标题：执行Python脚本分析DAU趋势失败。\n"
                    "目的：在指定工作目录下运行analyze_trend.py。\n"
                    "输入摘要：包含一个shell命令。\n"
                    "输出摘要：调用失败，KeyError: trend_o。"
                )
            }
        }
    )
    builder = PresentationBuilder(llm=llm)

    card = await builder.build(
        _tool_result("Bash", "Traceback...", status="error"),
        call_card=Presentation(
            card_type="tool",
            caption="执行Python脚本分析DAU趋势",
            body="在指定工作目录下运行analyze_trend.py。\n\n## Input\n\nshell命令",
        ),
    )

    assert card.body == "## Output\n\n调用失败，KeyError: trend_o。"
    assert "卡片小标题" not in card.body
    assert "输入摘要" not in card.body


async def test_a_failed_result_says_so() -> None:
    builder = PresentationBuilder()

    card = await builder.build(
        _tool_result("PlanCreate", "", status="error"), note="调用被拒绝执行。"
    )

    assert card.body.startswith("调用失败。")
    assert card.body.endswith("调用被拒绝执行。")


async def test_hint_cards_never_reach_the_model() -> None:
    llm = FakeLLM()
    builder = PresentationBuilder(llm=llm)

    card = await builder.build(
        BizEvent(event_id="h", kind="hint", source="interaction-strategy"),
        hint=[
            {"type": "text", "text": "先确认口径"},
            {"type": "image", "source": {"media_type": "image/png", "url": "u"}},
        ],
    )

    assert card.card_type == "hint"
    assert card.body.startswith("先确认口径")
    assert "[image/png](u)" in card.body
    assert "interaction-strategy" in card.body
    assert llm.calls == []


async def test_identical_requests_hit_the_cache() -> None:
    llm = FakeLLM({"presentation_body": {"body": "取数"}})
    builder = PresentationBuilder(llm=llm)
    event = BizEvent(event_id="t", kind="assistant_thinking", content="我要先取数")

    first = await builder.build(event)
    second = await builder.build(event)

    assert first.body == second.body == "取数"
    assert builder.llm_calls == 1
    assert builder.cache_hits == 1


async def test_entity_links_are_injected_into_the_body_only() -> None:
    builder = PresentationBuilder(
        linker=lambda body: body.replace("日活", "[日活](http://cm/metric)")
    )

    card = await builder.build(
        BizEvent(event_id="t", kind="assistant_text", content="日活下降")
    )

    assert card.body == "[日活](http://cm/metric)下降"
    assert card.caption == "模型回复"

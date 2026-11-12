# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.biztrace.llm import StructuredLLMError
from qwenpaw_data.host.core.algo.biztrace.models import (
    BizEvent,
    OrchestrationInfo,
    Presentation,
)
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
        self, *, system: str, user: str, step: Any, schema: dict[str, Any]
    ) -> dict[str, Any]:
        _ = schema
        name = str(step).removeprefix("biztrace.")
        self.calls.append((name, user, system))
        if self.fail:
            raise StructuredLLMError("model unavailable")
        return self.payloads.get(name, {})


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
    assert "不要输出 JSON" in system
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

    card = await builder.build(_tool_result("custom_widget", "x" * 5000))

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

    card = await builder.build(_tool_result("custom_widget", "12 rows"), call_card=call)

    assert card.caption == "查询日活"
    assert "共 12 行" in card.body
    assert "## Input\n\nSQL" in llm.calls[0][1]
    assert "只概括「出参」" in llm.calls[0][1]


async def test_prompts_say_what_a_registered_tool_is_for() -> None:
    llm = FakeLLM({"semantic_call": {"caption": "检索日活语义", "purpose": "锁定口径"}})
    builder = PresentationBuilder(llm=llm)

    await builder.build(
        _tool_use(
            "mcp__context-manager__query_graph",
            {
                "cypher": "MATCH (m:Metric) WHERE m.name = $n RETURN m.name",
                "params": {"n": "DAU"},
            },
        )
    )

    _schema, user, _system = llm.calls[0]
    assert "用只读 Cypher 检索业务图谱" in user
    assert "不要照抄" in user
    # The model sees the Cypher and its parameters, not a bare JSON envelope.
    assert "cypher: MATCH (m:Metric)" in user
    assert "params: " in user
    assert "DAU" in user.split("params: ", 1)[1]


async def test_query_graph_card_names_the_business_object_not_the_cypher() -> None:
    llm = FakeLLM(
        {
            "semantic_call": {
                "caption": "检索 DAU 口径",
                "purpose": "查询名称为 DAU 的指标及其口径",
                "input_summary": "名称为 DAU 的指标及全部口径",
            },
            "semantic_result": {
                "output_summary": "命中指标 DAU，口径在 dws_chat_dau_1d。"
            },
        }
    )
    builder = PresentationBuilder(llm=llm)
    call = await builder.build(
        _tool_use("query_graph", {"cypher": "MATCH (m:Metric) RETURN m.name AS name"})
    )
    result = await builder.build(
        _tool_result("query_graph", '{"columns":["name"],"rows":[{"name":"DAU"}]}'),
        call_card=call,
    )

    assert call.caption == "检索 DAU 口径"
    assert "名称为 DAU 的指标及全部口径" in call.body
    assert "MATCH" not in call.body
    assert result.caption == "检索 DAU 口径"
    assert "命中指标 DAU" in result.body


async def test_get_graph_schema_cards_are_rule_only() -> None:
    llm = FakeLLM()
    builder = PresentationBuilder(llm=llm)

    call = await builder.build(_tool_use("mcp__context-manager__get_graph_schema", {}))
    result = await builder.build(
        _tool_result("get_graph_schema", '{"version":16,"nodes":[]}'), call_card=call
    )
    failed = await builder.build(
        _tool_result("get_graph_schema", "boom", status="error"), call_card=call
    )

    assert llm.calls == []
    assert call.caption == "读取业务图谱结构"
    assert result.caption == call.caption
    assert result.body == "调用完成。"
    assert failed.body == "调用失败。"


async def test_prompts_omit_the_purpose_of_an_unregistered_tool() -> None:
    llm = FakeLLM({"tool_presentation": {"caption": "调用组件"}})
    builder = PresentationBuilder(llm=llm)

    await builder.build(_tool_use("custom_widget", {"a": 1}))

    assert "## 工具用途" not in llm.calls[0][1]


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
        _tool_result("custom_widget", "Traceback...", status="error"),
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


async def test_sql_call_appends_the_statement_as_a_code_block() -> None:
    llm = FakeLLM(
        {
            "sql_call": {
                "caption": "查最新分区",
                "purpose": "查询 analytics_data.ads_gaap 的最新分区。",
            }
        }
    )
    builder = PresentationBuilder(llm=llm)
    sql = "SELECT MAX(ds) as max_ds FROM analytics_data.ads_gaap"

    card = await builder.build(_tool_use("execute_read", {"sql": sql}))

    assert card.caption == "查最新分区"
    assert "```sql" in card.body
    assert sql in card.body
    assert llm.calls[0][0] == "sql_call"


async def test_ask_user_question_renders_spec_markdown() -> None:
    builder = PresentationBuilder()
    payload = {
        "title": "周报复刻任务确认",
        "questions": [
            {
                "question": "目标复刻的参考报告是哪个版本？",
                "options": [{"label": "W1 merged"}, {"label": "其他版本"}],
            },
            {
                "question": "是否继续补齐缺失环节？",
                "options": [{"label": "继续"}, {"label": "先停下"}],
            },
        ],
    }

    call = await builder.build(_tool_use("ask_user_question", payload))
    assert call is not None
    assert call.caption == "澄清提问：周报复刻任务确认"
    assert "## 周报复刻任务确认" not in call.body
    assert "1. 目标复刻的参考报告是哪个版本？" in call.body
    assert "2. 是否继续补齐缺失环节？" in call.body
    assert "**" not in call.body
    assert call.body.index("1. ") < call.body.index("2. ")
    assert "- W1 merged" in call.body

    blank = builder.blank_ask_call(payload)
    assert blank.caption == call.caption
    assert blank.body == ""

    result = await builder.build(
        _tool_result(
            "ask_user_question",
            {
                "status": "answered",
                "answers": [
                    {
                        "question": "目标复刻的参考报告是哪个版本？",
                        "selected_options": ["W1 merged"],
                        "custom_text": None,
                    }
                ],
            },
        ),
        call_input=payload,
    )
    assert result is not None
    assert result.caption == "澄清提问：周报复刻任务确认"
    assert "## 周报复刻任务确认" not in result.body
    assert "*W1 merged*" in result.body
    assert "**" not in result.body
    assert builder.llm_calls == 0

    untitled = await builder.build(
        _tool_use("ask_user_question", {"question": "看哪个口径?"})
    )
    assert untitled is not None
    assert untitled.caption == "澄清提问"
    assert untitled.body == "1. 看哪个口径?"

    english = PresentationBuilder(lang="en")
    en_card = await english.build(_tool_use("ask_user_question", payload))
    assert en_card is not None
    assert en_card.caption == "Clarifying question: 周报复刻任务确认"


async def test_listing_cards_stay_terse() -> None:
    builder = PresentationBuilder()
    call = await builder.build(_tool_use("Glob", {"pattern": "**/*.sql"}))
    assert call.caption == "定位文件"
    assert "*.sql" in call.body

    result = await builder.build(
        _tool_result("Glob", "skills/foo/SKILL.md\nreport.html"),
        call_card=call,
    )
    assert "report.html" in result.body
    assert "SKILL.md" not in result.body
    assert builder.llm_calls == 0


async def test_sql_failure_kind_is_labelled() -> None:
    llm = FakeLLM(
        {
            "sql_result": {
                "output_summary": "ads_gaap 不存在。",
                "failure_kind": "no_table",
            }
        }
    )
    builder = PresentationBuilder(llm=llm)

    card = await builder.build(
        _tool_result(
            "execute_read",
            {"error": "Table not found: ads_gaap"},
            status="error",
        )
    )

    assert "无表" in card.body
    assert "ads_gaap 不存在" in card.body


async def test_non_skill_read_result_is_a_done_note() -> None:
    builder = PresentationBuilder()
    card = await builder.build(
        _tool_result("read_file", "file contents"),
        call_input={"path": "out/june_gaap.csv"},
    )
    assert card.body == "调用完成。"
    assert builder.llm_calls == 0


async def test_text_cards_get_artifact_links_and_tool_cards_do_not() -> None:
    def linker(body: str) -> str:
        return body.replace("report.html", "[report.html](#artifact:report.html)")

    builder = PresentationBuilder(artifact_linker=linker)
    text = await builder.build(
        BizEvent(event_id="t", kind="assistant_text", content="见 report.html")
    )
    tool = await builder.build(_tool_use("Read", {"file_path": "report.html"}))

    assert text.body == "见 [report.html](#artifact:report.html)"
    assert "#artifact:" not in tool.body
    assert "report.html" in tool.body

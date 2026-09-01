# -*- coding: utf-8 -*-
"""What the collector can recover from a streamed run, and what it refuses to."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from qwenpaw_data.host.core.algo.followup.collector import SignalCollector
from qwenpaw_data.host.core.algo.followup.models import SignalSnapshot
from qwenpaw_data.host.core.algo.followup.settings import ANSWER_LIMIT, USER_INPUT_LIMIT


def _agent_event(**payload: Any) -> dict[str, Any]:
    payload.setdefault("reply_id", "reply-1")
    return {"kind": "agent_event", "payload": payload}


def _user_input(text: str) -> dict[str, Any]:
    return {
        "kind": "user_input",
        "payload": {"content": [{"type": "text", "text": text}]},
    }


def _text_block(
    block_id: str, text: str, *, closed: bool = True
) -> list[dict[str, Any]]:
    events = [
        _agent_event(type="TEXT_BLOCK_START", block_id=block_id),
        _agent_event(type="TEXT_BLOCK_DELTA", block_id=block_id, delta=text),
    ]
    if closed:
        events.append(_agent_event(type="TEXT_BLOCK_END", block_id=block_id))
    return events


def _tool_call(
    name: str, arguments: dict[str, Any], *, call_id: str = "call-1"
) -> list[dict[str, Any]]:
    """Stream the arguments in two fragments, as a model would emit them."""
    encoded = json.dumps(arguments, ensure_ascii=False)
    middle = len(encoded) // 2
    return [
        _agent_event(
            type="TOOL_CALL_START", tool_call_id=call_id, tool_call_name=name
        ),
        _agent_event(
            type="TOOL_CALL_DELTA", tool_call_id=call_id, delta=encoded[:middle]
        ),
        _agent_event(
            type="TOOL_CALL_DELTA", tool_call_id=call_id, delta=encoded[middle:]
        ),
        _agent_event(type="TOOL_CALL_END", tool_call_id=call_id),
    ]


def _tool_result(
    text: str,
    *,
    call_id: str = "call-1",
    state: str = "success",
    metadata: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return [
        _agent_event(
            type="TOOL_RESULT_TEXT_DELTA",
            tool_call_id=call_id,
            delta=text,
            metadata=metadata or {},
        ),
        _agent_event(type="TOOL_RESULT_END", tool_call_id=call_id, state=state),
    ]


async def _collect(entries: list[dict[str, Any]], **kwargs: Any) -> SignalSnapshot:
    collector = SignalCollector(**kwargs)
    collector.start()
    for entry in entries:
        collector.submit(entry)
    return await collector.freeze()


async def test_a_streamed_tool_call_is_reassembled_before_it_is_read() -> None:
    snapshot = await _collect(_tool_call("get_metric", {"name": "GAAP用户数"}))

    assert snapshot.metrics[0].name == "GAAP用户数"
    assert snapshot.metrics[0].analyzed is True


async def test_a_graph_key_is_stripped_to_the_surface_name() -> None:
    """Agents often pass CM keys as get_metric name; follow-up must not keep them."""

    entries = [
        *_tool_call(
            "get_metric",
            {"name": "met:postgresql-70569beb:ShopDemo:DAU"},
            call_id="c1",
        ),
        *_tool_call(
            "get_dimension",
            {"name": "dim:postgresql-70569beb:ShopDemo:渠道类型"},
            call_id="c2",
        ),
        *_tool_call(
            "get_dataset",
            {"name": "ds:postgresql-70569beb:ShopDemo:dau_daily"},
            call_id="c3",
        ),
    ]

    snapshot = await _collect(entries)

    assert snapshot.anchor_metric == "DAU"
    assert {entity.name for entity in snapshot.metrics} == {"DAU"}
    assert "渠道类型" in snapshot.business_entities
    assert "dau_daily" in {entity.name for entity in snapshot.datasets}
    assert not any(
        name.startswith(("met:", "dim:", "ds:"))
        for name in snapshot.business_entities
    )


async def test_a_graph_key_merges_with_the_surface_name_from_context() -> None:
    """search_context yields DAU; get_metric with the key must not fork a second metric."""

    context = json.dumps(
        {
            "schema_prompt": (
                "指标: met:postgresql-70569beb:ShopDemo:DAU\n"
                "  可下钻维度: 渠道类型, 页面"
            )
        },
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("search_context", {"query": "DAU"}, call_id="c1"),
        *_tool_result(context, call_id="c1"),
        *_tool_call(
            "get_metric",
            {"name": "met:postgresql-70569beb:ShopDemo:DAU"},
            call_id="c2",
        ),
    ]

    snapshot = await _collect(entries)

    assert snapshot.anchor_metric == "DAU"
    assert [entity.name for entity in snapshot.metrics] == ["DAU"]
    assert snapshot.metrics[0].analyzed is True
    assert set(snapshot.unused_dimensions) == {"渠道类型", "页面"}


async def test_a_skill_counts_as_used_via_the_skill_tool() -> None:
    entries = [
        *_tool_call("Skill", {"skill": "bi-funnel-analysis"}, call_id="c1"),
        *_tool_call("Skill", {"skill": "not-a-skill"}, call_id="c2"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.skills_used == ("bi-funnel-analysis",)


async def test_a_skill_also_counts_when_its_card_is_read() -> None:
    entries = [
        *_tool_call(
            "Read",
            {"file_path": "/ws/skills/atomic/bi-funnel-analysis/SKILL.md"},
            call_id="c1",
        ),
        *_tool_call(
            "Read", {"file_path": "/ws/skills/not-a-skill/SKILL.md"}, call_id="c2"
        ),
        *_tool_call("Read", {"file_path": "/ws/report.md"}, call_id="c3"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.skills_used == ("bi-funnel-analysis",)


async def test_skill_tool_and_read_both_contribute_to_skills_used() -> None:
    """Both Skill(skill=…) and Read of SKILL.md are checked for skill uses."""
    entries = [
        *_tool_call("Skill", {"skill": "bi-funnel-analysis"}, call_id="c1"),
        *_tool_call(
            "Read",
            {"file_path": "/ws/skills/atomic/bi-comparison-analysis/SKILL.md"},
            call_id="c2",
        ),
    ]

    snapshot = await _collect(entries)

    assert set(snapshot.skills_used) == {
        "bi-funnel-analysis",
        "bi-comparison-analysis",
    }


async def test_plan_nodes_are_reported_with_the_state_they_reached() -> None:
    entries = [
        *_tool_call(
            "PlanCreate",
            {
                "tasks": [
                    {"id": "n1", "subject": "取数", "description": "拉取指标"},
                    {"id": "n2", "subject": "下钻", "description": "按维度拆解"},
                ]
            },
            call_id="c1",
        ),
        *_tool_call(
            "TaskStateUpdate",
            {"task_id": "n1", "state": "completed"},
            call_id="c2",
        ),
        *_tool_call(
            "TaskStateUpdate",
            {"task_id": "n2", "state": "in_progress"},
            call_id="c3",
        ),
    ]

    snapshot = await _collect(entries)

    assert snapshot.completed_nodes == ("取数: completed", "下钻: in_progress")


async def test_plan_update_registers_revised_task_subjects() -> None:
    entries = [
        *_tool_call(
            "PlanCreate",
            {
                "tasks": [
                    {"id": "n1", "subject": "取数", "description": "拉取指标"},
                ]
            },
            call_id="c1",
        ),
        *_tool_call(
            "PlanUpdate",
            {
                "changes": [
                    {
                        "task_id": "n2",
                        "action": "add",
                        "task": {
                            "subject": "下钻",
                            "description": "按维度拆解",
                        },
                    }
                ]
            },
            call_id="c2",
        ),
        *_tool_call(
            "TaskStateUpdate",
            {"task_id": "n2", "state": "completed"},
            call_id="c3",
        ),
    ]

    snapshot = await _collect(entries)

    assert "下钻: completed" in snapshot.completed_nodes


async def test_a_lookup_names_entities_and_only_analysis_marks_them() -> None:
    context = json.dumps(
        {
            "schema_prompt": (
                "指标: met:holo_test:DemoBiz:GAAP用户数\n  可下钻维度: 渠道类型, 页面"
            )
        },
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("search_context", {"query": "GAAP用户数"}, call_id="c1"),
        *_tool_result(context, call_id="c1"),
        *_tool_call("get_dimension", {"name": "渠道类型"}, call_id="c2"),
    ]

    snapshot = await _collect(entries)

    assert {entity.name for entity in snapshot.metrics} == {"GAAP用户数"}
    assert {entity.name for entity in snapshot.dimensions} == {"渠道类型", "页面"}
    assert snapshot.unused_dimensions == ("页面",)


async def test_a_dimension_is_bound_to_the_metric_it_was_listed_under() -> None:
    """The lookup reports bindings per metric, and only the anchor's own
    dimensions are worth offering as a breakdown of it."""

    context = json.dumps(
        {
            "schema_prompt": (
                "指标: met:holo_test:DemoBiz:人均GAAP\n  可下钻维度: 渠道类型\n"
                "指标: met:holo_test:DemoBiz:漏斗有效付费用户数\n  可下钻维度: 落地页\n"
            )
        },
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("search_context", {"query": "人均GAAP"}, call_id="c1"),
        *_tool_result(context, call_id="c1"),
    ]

    snapshot = await _collect(entries)
    relevance = {entity.name: entity.relevance for entity in snapshot.dimensions}

    assert snapshot.anchor_metric == "人均GAAP"
    assert relevance["渠道类型"] > relevance["落地页"]


def _listed_dimension(name: str, alias: str) -> list[dict[str, Any]]:
    body = json.dumps(
        [{"dimension_name": name, "aliases": [alias]}], ensure_ascii=False
    )
    return [
        *_tool_call("list_dimensions", {"domain": "DemoBiz"}, call_id="dims"),
        *_tool_result(body, call_id="dims"),
    ]


async def test_a_dimension_grouped_by_in_sql_counts_as_analyzed() -> None:
    entries = [
        *_listed_dimension("渠道类型", "channel_type"),
        *_tool_call(
            "execute_sql",
            {
                "sql": (
                    "SELECT channel_type, sum(gaap) FROM dwd.gaap_daily "
                    "GROUP BY channel_type"
                )
            },
            call_id="c2",
        ),
    ]

    snapshot = await _collect(entries)

    assert snapshot.dimensions[0].analyzed is True
    assert snapshot.unused_dimensions == ()
    assert {entity.name for entity in snapshot.datasets} == {"gaap_daily"}


async def test_a_dimension_only_filtered_on_is_still_worth_drilling_into() -> None:
    """Restricting a query to one channel is not the same as decomposing by
    channel, so the dimension stays on offer."""

    entries = [
        *_listed_dimension("渠道类型", "channel_type"),
        *_tool_call(
            "execute_sql",
            {
                "sql": (
                    "SELECT sum(gaap) FROM dwd.gaap_daily "
                    "WHERE channel_type = 'app'"
                )
            },
            call_id="c2",
        ),
    ]

    snapshot = await _collect(entries)

    assert snapshot.dimensions[0].analyzed is False
    assert snapshot.unused_dimensions == ("渠道类型",)


async def test_a_query_local_alias_is_not_a_dataset() -> None:
    """"WITH daily_agg AS (...)" names a step of the query, not a table."""

    entries = _tool_call(
        "execute_sql",
        {
            "sql": (
                "WITH daily_agg AS (SELECT dt, sum(gaap) g FROM dwd.gaap_daily "
                "GROUP BY dt) SELECT * FROM daily_agg"
            )
        },
    )

    snapshot = await _collect(entries)

    assert {entity.name for entity in snapshot.datasets} == {"gaap_daily"}


async def test_a_dimension_from_a_domain_listing_is_pruned_but_still_real() -> None:
    """Nobody asked for it, so it must not be recommended; a question naming it
    is still grounded."""

    body = json.dumps(
        [{"dimension_name": "MCPServerID"}, {"dimension_name": "渠道类型"}],
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("get_metric", {"name": "人均GAAP"}, call_id="c1"),
        *_tool_call("list_dimensions", {"domain": "DemoBiz"}, call_id="c2"),
        *_tool_result(body, call_id="c2"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.anchor_metric == "人均GAAP"
    assert snapshot.dimensions == ()
    assert "MCPServerID" in snapshot.business_entities


async def test_the_pruning_budget_is_configurable() -> None:
    context = json.dumps(
        {
            "schema_prompt": (
                "指标: met:holo_test:DemoBiz:人均GAAP\n"
                "  可下钻维度: 渠道类型, 落地页, 地域, 产品"
            )
        },
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("search_context", {"query": "人均GAAP"}, call_id="c1"),
        *_tool_result(context, call_id="c1"),
    ]

    assert len((await _collect(entries)).dimensions) == 4

    snapshot = await _collect(entries, max_dimensions=1)

    assert len(snapshot.dimensions) == 1


async def test_a_failed_tool_result_contributes_nothing() -> None:
    body = json.dumps({"dimension_name": "页面"}, ensure_ascii=False)
    entries = [
        *_tool_call("list_dimensions", {}, call_id="c1"),
        *_tool_result(body, call_id="c1", state="error"),
        *_tool_call("list_dimensions", {}, call_id="c2"),
        *_tool_result("Error executing tool list_dimensions: 404", call_id="c2"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.dimensions == ()


async def test_a_sub_agent_transcript_is_not_this_run_s_tool_output() -> None:
    body = json.dumps({"dimension_name": "页面"}, ensure_ascii=False)
    entries = [
        *_tool_call("list_dimensions", {}, call_id="c1"),
        *_tool_result(body, call_id="c1", metadata={"subagent_event": True}),
    ]

    snapshot = await _collect(entries)

    assert snapshot.dimensions == ()


async def test_artifacts_are_counted_by_kind_and_never_twice() -> None:
    entries = [
        *_tool_call("Write", {"file_path": "out/board.html"}, call_id="c1"),
        *_tool_call("Write", {"file_path": "out/board.html"}, call_id="c2"),
        *_tool_call("Write", {"file_path": "out/notes.md"}, call_id="c3"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.artifacts_summary == "已产出 1 个看板/报告页面、1 个文档"


async def test_no_artifact_is_stated_as_such() -> None:
    snapshot = await _collect([])

    assert snapshot.artifacts_summary == "无产出物"


async def test_the_question_and_the_conclusion_are_truncated() -> None:
    entries = [
        _user_input("问" * (USER_INPUT_LIMIT + 50)),
        *_text_block("blk-1", "答" * (ANSWER_LIMIT + 50)),
    ]

    snapshot = await _collect(entries)

    assert len(snapshot.user_input) == USER_INPUT_LIMIT
    assert len(snapshot.final_answer_summary) == ANSWER_LIMIT


async def test_the_last_closed_block_is_the_conclusion() -> None:
    entries = [
        *_text_block("blk-1", "先看总量。"),
        *_text_block("blk-2", "  "),
        *_text_block("blk-3", "日活在月中下降。"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.final_answer_summary == "日活在月中下降。"


async def test_a_reply_cut_short_still_yields_its_answer() -> None:
    """An interrupted turn never sends TEXT_BLOCK_END, and its text is all
    the conclusion there is."""

    snapshot = await _collect(_text_block("blk-1", "只写到这里", closed=False))

    assert snapshot.final_answer_summary == "只写到这里"


async def test_submitting_nonsense_never_raises() -> None:
    snapshot = await _collect(
        [
            {"kind": "agent_event", "payload": None},
            {"kind": "agent_event", "payload": {"type": "TOOL_CALL_END"}},
            {"kind": "user_input", "payload": {}},
            {},
        ]
    )

    assert snapshot.user_input == ""


async def test_the_snapshot_cannot_be_edited_after_it_is_frozen() -> None:
    snapshot = await _collect(_user_input("看下日活"))

    with pytest.raises(ValidationError):
        snapshot.user_input = "改一下"


async def test_get_priority_metrics_lists_are_domain_dumps() -> None:
    body = json.dumps(
        [
            {"metric_name": "DAU", "aliases": ["日活"], "priority": "high"},
            {"metric_name": "MAU", "priority": "normal"},
        ],
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("get_priority_metrics", {"domain": "ShopDemo"}, call_id="c1"),
        *_tool_result(body, call_id="c1"),
    ]

    snapshot = await _collect(entries)

    assert {"DAU", "MAU"} <= set(snapshot.business_entities)
    assert snapshot.entity_aliases == (("日活", "DAU"),)
    assert all(not entity.analyzed for entity in snapshot.metrics)


async def test_get_north_star_metrics_is_the_same_listing() -> None:
    body = json.dumps([{"metric_name": "DAU"}], ensure_ascii=False)
    entries = [
        *_tool_call("get_north_star_metrics", {"domain": "ShopDemo"}, call_id="c1"),
        *_tool_result(body, call_id="c1"),
    ]

    snapshot = await _collect(entries)

    assert {entity.name for entity in snapshot.metrics} == {"DAU"}


async def test_search_context_records_intent_feedback_and_golden_query() -> None:
    context = json.dumps(
        {
            "schema_prompt": "指标: met:holo_test:DemoBiz:DAU\n  可下钻维度: 渠道",
            "intent_feedback": {
                "coverage": "insufficient",
                "gaps": ["无指标命中", "时间不可解析"],
                "next_steps": ["澄清时间范围", "换同义指标"],
            },
            "golden_query": {"verified_sql": "SELECT 1"},
        },
        ensure_ascii=False,
    )
    entries = [
        *_tool_call("search_context", {"query": "DAU"}, call_id="c1"),
        *_tool_result(context, call_id="c1"),
    ]

    snapshot = await _collect(entries)

    assert snapshot.intent_coverage == "insufficient"
    assert snapshot.intent_gaps == ("无指标命中", "时间不可解析")
    assert snapshot.intent_next_step == "澄清时间范围"
    assert snapshot.has_golden_query is True
    assert snapshot.anchor_metric == "DAU"

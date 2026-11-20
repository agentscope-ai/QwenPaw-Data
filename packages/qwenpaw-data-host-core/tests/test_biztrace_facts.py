# -*- coding: utf-8 -*-
"""Tests for mechanical window-fact mining."""

from __future__ import annotations

from qwenpaw_data.host.core.algo.biztrace.facts import (
    WindowFacts,
    classify_sql_error,
    collect_facts,
)
from qwenpaw_data.host.core.algo.biztrace.segmentation import SegmentChainNode


def _node(*content: dict) -> SegmentChainNode:
    return SegmentChainNode(
        index=0,
        kind="normal",
        role="assistant",
        parent_msg_id="r1",
        covered=True,
        content=list(content),
    )


def test_sql_window_yields_tables_date_join_and_metrics() -> None:
    sql = (
        "SELECT i.aliyun_uid, SUM(i.gaap_d) AS amt "
        "FROM analytics_data.ads_gaap i "
        "JOIN sales_cdm.dim_user u ON i.aliyun_uid = u.aliyun_uid "
        "WHERE i.thedate >= 20260501 AND i.ds = 20260531"
    )
    facts = collect_facts(
        [
            _node(
                {
                    "type": "tool_use",
                    "name": "execute_read",
                    "input": {"sql": sql},
                }
            )
        ]
    )
    assert "analytics_data.ads_gaap" in facts.tables
    assert "sales_cdm.dim_user" in facts.tables
    assert "thedate" in facts.date_columns
    assert "ds" in facts.date_columns
    assert "aliyun_uid" in facts.join_keys
    assert any("gaap_d" in item for item in facts.metric_columns)


def test_skill_and_hash_filenames_are_excluded() -> None:
    facts = collect_facts(
        [
            _node(
                {
                    "type": "tool_use",
                    "name": "read_file",
                    "input": {"path": "skills/query-odps/SKILL.md"},
                },
                {
                    "type": "tool_use",
                    "name": "read_file",
                    "input": {"path": "/tmp/95f204a5.csv"},
                },
                {
                    "type": "tool_use",
                    "name": "read_file",
                    "input": {"path": "/nas/knowhow.md"},
                },
                {
                    "type": "tool_use",
                    "name": "write_file",
                    "input": {"path": "out/june_gaap.csv"},
                },
            )
        ]
    )
    assert facts.files_read == ["knowhow.md"]
    assert facts.files_written == ["june_gaap.csv"]
    assert facts.has_input_evidence() is True


def test_failure_kinds_and_closing_reply() -> None:
    facts = collect_facts(
        [
            _node(
                {
                    "type": "tool_result",
                    "name": "execute_read",
                    "status": "error",
                    "output": "Table not found: ads_gaap",
                },
                {"type": "text", "text": "中途说明"},
                {"type": "text", "text": "最终交付：6 张卡。"},
            )
        ]
    )
    assert "no_table" in facts.failure_kinds
    assert facts.closing_reply == "最终交付：6 张卡。"


def test_answered_ask_counts_as_input_evidence() -> None:
    facts = collect_facts(
        [
            _node(
                {
                    "type": "tool_result",
                    "name": "ask_user_question",
                    "output": {
                        "status": "answered",
                        "answers": [
                            {
                                "question": "口径?",
                                "selected_options": ["自然日"],
                                "custom_text": None,
                            }
                        ],
                    },
                }
            )
        ]
    )
    assert facts.answered_ask is True
    assert facts.has_input_evidence() is True


def test_classify_sql_error_splits_the_spec_kinds() -> None:
    assert classify_sql_error("Table not found: x") == "no_table"
    assert classify_sql_error("odps:Select denied") == "no_permission"
    assert classify_sql_error("Unknown column gaap_d") == "no_column"
    assert classify_sql_error("query blocked by interceptor") == "blocked"


def test_empty_window_has_no_input_evidence() -> None:
    facts = WindowFacts()
    assert facts.has_input_evidence() is False
    assert facts.render() == ""


def test_plan_create_keeps_every_step_in_order() -> None:
    long_description = "定位异常来源。" + "明细" * 200
    facts = collect_facts(
        [
            _node(
                {
                    "type": "tool_use",
                    "name": "PlanCreate",
                    "input": {
                        "tasks": [
                            {
                                "id": "fetch_core_metrics",
                                "subject": "取数：最近一周核心运营指标日序列",
                                "description": "拉取 7 天日粒度数据，落盘 data/raw/core_metrics_daily.csv。",
                                "blocked_by": [],
                            },
                            {
                                "id": "observe_new_users",
                                "subject": "观测与异常检测：新增角色数趋势",
                                "description": "输出趋势描述与异常点，落盘 steps/step_01_new_users.yaml。",
                                "blocked_by": ["fetch_core_metrics"],
                            },
                            {
                                "id": "observe_active_users",
                                "subject": "观测与异常检测：活跃角色数趋势",
                                "description": "落盘 steps/step_02_active_users.yaml。",
                                "blocked_by": ["fetch_core_metrics"],
                            },
                            {
                                "id": "observe_revenue",
                                "subject": "观测与异常检测：角色付费金额趋势",
                                "description": "落盘 steps/step_03_revenue.yaml。",
                                "blocked_by": ["fetch_core_metrics"],
                            },
                            {
                                "id": "revenue_decomposition",
                                "subject": "收入变化拆解：付费率×ARPPU×付费人数",
                                "description": "识别付费人数与客单价的贡献。",
                                "blocked_by": ["observe_revenue"],
                            },
                            {
                                "id": "drilldown_anomalies",
                                "subject": "维度下钻：异常指标的关键维度定位",
                                "description": long_description,
                                "blocked_by": [
                                    "observe_new_users",
                                    "observe_active_users",
                                    "observe_revenue",
                                    "revenue_decomposition",
                                ],
                            },
                            "not-a-task",
                            {
                                "id": "generate_report",
                                "subject": "生成分析报告：游戏运营周报",
                                "description": "生成可视化 HTML 报告。",
                                "blocked_by": ["drilldown_anomalies"],
                            },
                        ]
                    },
                }
            )
        ]
    )
    assert len(facts.plan_steps) == 7
    assert facts.plan_steps[0].startswith("取数：最近一周核心运营指标日序列 —")
    assert "（依赖：" not in facts.plan_steps[0]
    assert "依赖：fetch_core_metrics" in facts.plan_steps[1]
    assert "依赖：observe_revenue" in facts.plan_steps[4]
    drilldown = facts.plan_steps[5]
    assert "依赖：observe_new_users、observe_active_users" in drilldown
    assert drilldown.endswith("…")
    assert facts.plan_steps[6].startswith("生成分析报告：游戏运营周报")
    assert "drilldown_anomalies" in facts.plan_steps[6]
    rendered = facts.render()
    assert "- 计划步骤:" in rendered
    assert rendered.index("1. 取数") < rendered.index("7. 生成分析报告")
    assert "plan steps" in facts.render(lang="en")


def test_plan_update_lists_add_revise_and_delete() -> None:
    facts = collect_facts(
        [
            _node(
                {
                    "type": "tool_use",
                    "name": "planupdate",
                    "input": {
                        "changes": [
                            {
                                "action": "add",
                                "task_id": "observe_pay_rate",
                                "task": {
                                    "subject": "观测付费率",
                                    "description": "按日看付费率波动。",
                                },
                            },
                            {
                                "action": "revise",
                                "task_id": "generate_report",
                                "task": {
                                    "subject": "生成周报",
                                    "description": "补上付费率章节。",
                                },
                            },
                            {"action": "delete", "task_id": "drilldown_anomalies"},
                            {"action": "add"},
                        ]
                    },
                }
            )
        ]
    )
    assert facts.plan_steps == [
        "add observe_pay_rate: 观测付费率 — 按日看付费率波动。",
        "revise generate_report: 生成周报 — 补上付费率章节。",
        "delete drilldown_anomalies",
    ]


def test_facts_render_names_mined_values() -> None:
    facts = WindowFacts(
        tables=["ads_gaap"],
        join_keys=["aliyun_uid"],
        answered_ask=True,
    )
    rendered = facts.render()
    assert "ads_gaap" in rendered
    assert "aliyun_uid" in rendered
    assert "已作答的澄清提问" in rendered

# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any

from qwenpaw_data.host.core.algo.biztrace.models import BizEvent, OrchestrationInfo
from qwenpaw_data.host.core.algo.biztrace.plan import (
    PlanLedger,
    PlanOp,
    replay_plan,
    select_priors,
)
from qwenpaw_data.host.core.algo.evaluation.settings import PRIOR_SEGMENTS


def _use(
    seq: int,
    event_id: str,
    category: str,
    arguments: dict[str, Any],
    *,
    subtask_state: str | None = None,
) -> BizEvent:
    return BizEvent(
        event_id=event_id,
        kind="tool_use",
        seq=seq,
        block_id=event_id,
        tool_name=category,
        input=arguments,
        orchestration=OrchestrationInfo(
            category=category,  # type: ignore[arg-type]
            subtask_state=subtask_state,  # type: ignore[arg-type]
            node_id=arguments.get("task_id"),
        ),
    )


def _result(seq: int, event_id: str, *, status: str = "done") -> BizEvent:
    return BizEvent(
        event_id=f"{event_id}:result",
        kind="tool_result",
        seq=seq,
        block_id=event_id,
        status=status,  # type: ignore[arg-type]
        tool_name="TaskStateUpdate",
    )


def test_ledger_commits_only_a_successful_result() -> None:
    ledger = PlanLedger()
    ledger.observe(
        _use(1, "call_ok", "TaskStateUpdate", {"task_id": "t1", "state": "in_progress"})
    )
    ledger.observe(_result(2, "call_ok"))
    ledger.observe(
        _use(3, "call_err", "TaskStateUpdate", {"task_id": "t1", "state": "completed"})
    )
    ledger.observe(_result(4, "call_err", status="error"))
    ledger.observe(
        _use(
            5, "call_lost", "TaskStateUpdate", {"task_id": "t2", "state": "in_progress"}
        )
    )

    assert [task["id"] for task in ledger.snapshot(at=9)] == []
    # Nothing to replay without a PlanCreate; the successful call still committed.
    assert ledger._ops == [
        PlanOp(
            seq=1,
            name="TaskStateUpdate",
            arguments={"task_id": "t1", "state": "in_progress"},
        )
    ]


def test_ledger_positions_the_op_at_the_call_card() -> None:
    ledger = PlanLedger()
    ledger.observe(
        _use(7, "call_1", "TaskStateUpdate", {"task_id": "t1", "state": "in_progress"})
    )
    ledger.observe(_result(8, "call_1"))

    assert [op.seq for op in ledger._ops] == [7]


def test_replay_plan_applies_updates_in_biz_seq_order() -> None:
    ops = [
        PlanOp(
            seq=1,
            name="PlanCreate",
            arguments={
                "tasks": [
                    {"id": "t1", "subject": "查询", "description": "拉明细"},
                    {"id": "t2", "subject": "分析", "description": "拆渠道"},
                ],
            },
        ),
        PlanOp(
            seq=2,
            name="TaskStateUpdate",
            arguments={"task_id": "t1", "state": "in_progress"},
        ),
        PlanOp(
            seq=3,
            name="TaskStateUpdate",
            arguments={"task_id": "t1", "state": "completed"},
        ),
        PlanOp(
            seq=4,
            name="PlanUpdate",
            arguments={
                "changes": [
                    {
                        "task_id": "t2",
                        "action": "revise",
                        "task": {"subject": "分析 v2", "description": "按周拆"},
                    },
                    {
                        "task_id": "t3",
                        "action": "add",
                        "task": {"subject": "报告", "description": "汇总"},
                    },
                ],
            },
        ),
        PlanOp(
            seq=5,
            name="TaskStateUpdate",
            arguments={"task_id": "t2", "state": "in_progress"},
        ),
        PlanOp(
            seq=6,
            name="PlanUpdate",
            arguments={
                "changes": [{"task_id": "t3", "action": "delete"}],
            },
        ),
    ]

    at4 = replay_plan(ops, at=4)
    assert list(at4) == ["t1", "t2", "t3"]
    assert at4["t1"]["state"] == "completed"
    assert at4["t2"]["state"] == "pending"
    assert at4["t2"]["subject"] == "分析 v2"
    assert at4["t3"]["state"] == "pending"

    at6 = replay_plan(ops, at=6)
    assert list(at6) == ["t1", "t2"]
    assert at6["t2"]["state"] == "in_progress"

    at3 = replay_plan(ops, at=3)
    assert at3["t2"]["subject"] == "分析"

    assert (
        replay_plan(
            [
                PlanOp(
                    seq=1,
                    name="TaskStateUpdate",
                    arguments={"task_id": "t1", "state": "in_progress"},
                )
            ],
            at=9,
        )
        == {}
    )


def test_select_priors_without_a_subtask_take_the_latest_window() -> None:
    rows = [(i, i, f"结论{i}") for i in range(1, 7)]
    rows.insert(0, (6, 0, ""))

    selected = select_priors(rows, before=7)

    assert selected == [
        (3, "结论3"),
        (4, "结论4"),
        (5, "结论5"),
        (6, "结论6"),
    ]
    assert len(selected) == PRIOR_SEGMENTS


def test_select_priors_filters_by_the_running_subtask() -> None:
    rows = [
        (2, 1, "t1 时期段"),
        (3, 3, "过渡段"),
        (6, 4, "t2 时期段"),
    ]

    def active_at(at: int) -> set[str]:
        if at <= 2:
            return {"t1"}
        if at >= 4:
            return {"t2"}
        return set()

    selected = select_priors(rows, before=7, subtask="t2", active_at=active_at)

    assert selected == [(3, "t2 时期段")]


def test_select_priors_caps_the_filtered_branch_at_the_limit() -> None:
    rows = [(i + 1, i, f"t1 段{i}") for i in range(1, 8)]

    selected = select_priors(rows, before=9, subtask="t1", active_at=lambda at: {"t1"})

    assert selected == [
        (4, "t1 段4"),
        (5, "t1 段5"),
        (6, "t1 段6"),
        (7, "t1 段7"),
    ]
    assert len(selected) == PRIOR_SEGMENTS

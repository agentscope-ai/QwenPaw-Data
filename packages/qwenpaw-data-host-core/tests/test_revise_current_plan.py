# -*- coding: utf-8 -*-
from __future__ import annotations

import pytest

from qwenpaw_data.host.core.orchestration.state import RuntimeStateManager
from qwenpaw_data.host.core.orchestration.task_graph import (
    PlanNodeChange,
    TaskNode,
    apply_dag_patch,
    apply_plan_changes,
    make_node,
)


GRAPH_ID = "graph-test"


def _node(
    node_id: str,
    name: str,
    deps: list[str] | None = None,
    state: str = "todo",
) -> TaskNode:
    return make_node(
        graph_id=GRAPH_ID,
        node_id=node_id,
        name=name,
        description=f"{name} description",
        expected_outcome=f"{name} outcome",
        deps=deps or [],
        state=state,  # type: ignore[arg-type]
    )


def _change_node(
    name: str,
    deps: list[str] | None = None,
    node_id: str | None = None,
) -> dict:
    payload = {
        "name": name,
        "description": f"{name} description",
        "expected_outcome": f"{name} outcome",
        "deps": deps or [],
    }
    if node_id is not None:
        payload["node_id"] = node_id
    return payload


def _linear_nodes() -> list[TaskNode]:
    return [
        _node("n1", "N1", state="done"),
        _node("n2", "N2", deps=["n1"], state="in_progress"),
        _node("n3", "N3", deps=["n2"], state="todo"),
    ]


def _chunk_text(response) -> str:
    block = response.content[0]
    return block.text if hasattr(block, "text") else block["text"]


def test_apply_plan_changes_single_revise_marks_todo_downstream() -> None:
    nodes = _linear_nodes()
    nodes[2].state = "in_progress"

    result = apply_plan_changes(
        nodes,
        GRAPH_ID,
        [
            PlanNodeChange(
                node_id="n2",
                action="revise",
                node=_change_node("N2 revised", deps=["n1"]),
            ),
        ],
    )

    node_map = {node.id: node for node in nodes}
    assert result.revised == ["n2"]
    assert result.downstream_reset == ["n3"]
    assert node_map["n2"].state == "todo"
    assert node_map["n2"].name == "N2 revised"
    assert node_map["n3"].state == "todo"
    assert node_map["n1"].state == "done"


def test_apply_plan_changes_batch_mixed_actions() -> None:
    nodes = _linear_nodes()

    result = apply_plan_changes(
        nodes,
        GRAPH_ID,
        [
            PlanNodeChange(
                node_id="n4",
                action="add",
                node=_change_node("N4", deps=["n2"]),
            ),
            PlanNodeChange(node_id="n3", action="delete"),
            PlanNodeChange(
                node_id="n2",
                action="revise",
                node=_change_node("N2 batch", deps=["n1"]),
            ),
        ],
    )

    node_map = {node.id: node for node in nodes}
    assert result.added == ["n4"]
    assert result.deleted == ["n3"]
    assert result.revised == ["n2"]
    assert "n3" not in node_map
    assert "n4" in node_map
    assert node_map["n2"].state == "todo"


def test_apply_plan_changes_rejects_invalid_atomically() -> None:
    nodes = _linear_nodes()
    before = [node.model_dump(mode="json") for node in nodes]

    with pytest.raises(ValueError, match="not found"):
        apply_plan_changes(
            nodes,
            GRAPH_ID,
            [PlanNodeChange(node_id="missing", action="delete")],
        )

    assert [node.model_dump(mode="json") for node in nodes] == before


def test_apply_plan_changes_todo_dedup_across_revised_nodes() -> None:
    nodes = [
        _node("a", "A", state="done"),
        _node("b", "B", deps=["a"], state="done"),
        _node("c", "C", deps=["a"], state="done"),
        _node("d", "D", deps=["b", "c"], state="in_progress"),
    ]

    result = apply_plan_changes(
        nodes,
        GRAPH_ID,
        [
            PlanNodeChange(
                node_id="b",
                action="revise",
                node=_change_node("B2", deps=["a"]),
            ),
            PlanNodeChange(
                node_id="c",
                action="revise",
                node=_change_node("C2", deps=["a"]),
            ),
        ],
    )

    node_map = {node.id: node for node in nodes}
    assert result.revised == ["b", "c"]
    assert result.downstream_reset == ["d"]
    assert node_map["d"].state == "todo"


def test_apply_plan_changes_rejects_unknown_dep_with_reason() -> None:
    nodes = _linear_nodes()
    before = [node.model_dump(mode="json") for node in nodes]

    with pytest.raises(ValueError) as exc_info:
        apply_plan_changes(
            nodes,
            GRAPH_ID,
            [
                PlanNodeChange(
                    node_id="n2",
                    action="revise",
                    node=_change_node("N2", deps=["n9"]),
                ),
            ],
        )

    msg = str(exc_info.value)
    assert "Invalid dependency" in msg
    assert "unknown deps" in msg
    assert "n9" in msg
    assert [node.model_dump(mode="json") for node in nodes] == before


def test_apply_plan_changes_rejects_self_dependency() -> None:
    nodes = _linear_nodes()

    with pytest.raises(ValueError, match="cannot list itself in deps"):
        apply_plan_changes(
            nodes,
            GRAPH_ID,
            [
                PlanNodeChange(
                    node_id="n2",
                    action="revise",
                    node=_change_node("N2", deps=["n2"]),
                ),
            ],
        )


def test_apply_plan_changes_rejects_cycle_with_reason() -> None:
    nodes = _linear_nodes()
    before = [node.model_dump(mode="json") for node in nodes]

    with pytest.raises(ValueError) as exc_info:
        apply_plan_changes(
            nodes,
            GRAPH_ID,
            [
                PlanNodeChange(
                    node_id="n1",
                    action="revise",
                    node=_change_node("N1", deps=["n3"]),
                ),
            ],
        )

    assert "Invalid topology" in str(exc_info.value)
    assert "cycle" in str(exc_info.value).lower()
    assert [node.model_dump(mode="json") for node in nodes] == before


def test_apply_plan_changes_rejects_mutual_add_cycle() -> None:
    nodes: list[TaskNode] = []

    with pytest.raises(ValueError, match="cycle"):
        apply_plan_changes(
            nodes,
            GRAPH_ID,
            [
                PlanNodeChange(
                    node_id="a",
                    action="add",
                    node=_change_node("A", deps=["b"]),
                ),
                PlanNodeChange(
                    node_id="b",
                    action="add",
                    node=_change_node("B", deps=["a"]),
                ),
            ],
        )


def test_apply_plan_changes_revise_deps_success() -> None:
    nodes = _linear_nodes()

    result = apply_plan_changes(
        nodes,
        GRAPH_ID,
        [
            PlanNodeChange(
                node_id="n3",
                action="revise",
                node=_change_node("N3", deps=["n1"]),
            ),
        ],
    )

    node_map = {node.id: node for node in nodes}
    assert result.revised == ["n3"]
    assert node_map["n3"].deps == ["n1"]
    assert node_map["n3"].state == "todo"


def _webapp_observation_nodes() -> list[TaskNode]:
    return [
        _node("n1_data_fetch", "数据获取"),
        _node(
            "n2_metric_observation",
            "指标基础观测",
            deps=["n1_data_fetch"],
        ),
        _node(
            "n3_anomaly_detection",
            "异常波动检测",
            deps=["n1_data_fetch"],
        ),
        _node(
            "n4_attribution",
            "维度下拆归因",
            deps=["n2_metric_observation", "n3_anomaly_detection"],
        ),
        _node(
            "n5_report",
            "报告生成",
            deps=[
                "n2_metric_observation",
                "n3_anomaly_detection",
                "n4_attribution",
            ],
        ),
    ]


def test_apply_plan_changes_add_without_inner_node_id_and_revise_dep() -> None:
    nodes = _webapp_observation_nodes()

    result = apply_plan_changes(
        nodes,
        GRAPH_ID,
        [
            PlanNodeChange(
                node_id="n1_data_fetch_active_user",
                action="add",
                node=_change_node("补充获取激活用户数", deps=[]),
            ),
            PlanNodeChange(
                node_id="n2_metric_observation",
                action="revise",
                node=_change_node(
                    "指标基础观测",
                    deps=["n1_data_fetch", "n1_data_fetch_active_user"],
                ),
            ),
        ],
    )

    node_map = {node.id: node for node in nodes}
    assert result.added == ["n1_data_fetch_active_user"]
    assert result.revised == ["n2_metric_observation"]
    assert "n1_data_fetch_active_user" in node_map
    assert node_map["n2_metric_observation"].deps == [
        "n1_data_fetch",
        "n1_data_fetch_active_user",
    ]


def test_apply_plan_changes_rejects_add_node_id_mismatch() -> None:
    nodes = _linear_nodes()

    with pytest.raises(ValueError, match="node_id mismatch"):
        apply_plan_changes(
            nodes,
            GRAPH_ID,
            [
                PlanNodeChange(
                    node_id="n4",
                    action="add",
                    node=_change_node("N4", node_id="n9"),
                ),
            ],
        )


@pytest.mark.asyncio
async def test_revise_current_plan_tool_notifies_once() -> None:
    state = RuntimeStateManager()
    await state.create_plan(
        name="Graph",
        description="desc",
        expected_outcome="outcome",
        nodes=[
            {
                "node_id": "n1",
                "name": "N1",
                "description": "d1",
                "expected_outcome": "o1",
            },
            {
                "node_id": "n2",
                "name": "N2",
                "description": "d2",
                "expected_outcome": "o2",
                "deps": ["n1"],
            },
            {
                "node_id": "n3",
                "name": "N3",
                "description": "d3",
                "expected_outcome": "o3",
                "deps": ["n2"],
            },
        ],
    )
    notify_count = 0

    async def _count_notify(_event_type):
        nonlocal notify_count
        notify_count += 1

    state._notify_graph_change = _count_notify  # type: ignore[method-assign]

    response = await state.revise_current_plan(
        [
            {
                "node_id": "n2",
                "action": "revise",
                "node": _change_node("N2 tool", deps=["n1"]),
            },
            {"node_id": "n3", "action": "delete"},
        ],
    )

    assert notify_count == 1
    assert "Applied 2 change(s)" in _chunk_text(response)
    assert "n3" not in {node.id for node in state._nodes}


@pytest.mark.asyncio
async def test_revise_current_plan_tool_returns_topology_error() -> None:
    state = RuntimeStateManager()
    await state.create_plan(
        name="Graph",
        description="desc",
        expected_outcome="outcome",
        nodes=[
            {
                "node_id": "n1",
                "name": "N1",
                "description": "d1",
                "expected_outcome": "o1",
            },
            {
                "node_id": "n2",
                "name": "N2",
                "description": "d2",
                "expected_outcome": "o2",
                "deps": ["n1"],
            },
        ],
    )

    response = await state.revise_current_plan(
        [
            {
                "node_id": "n2",
                "action": "revise",
                "node": _change_node("N2", deps=["missing"]),
            },
        ],
    )

    text = _chunk_text(response)
    assert "Invalid dependency" in text
    assert "unknown deps" in text


def test_apply_dag_patch_resets_downstream_for_modified_node() -> None:
    nodes = [
        _node("n1", "N1", state="done"),
        _node("n2", "N2", deps=["n1"], state="done"),
        _node("n3", "N3", deps=["n2"], state="in_progress"),
    ]

    diff = apply_dag_patch(
        nodes,
        GRAPH_ID,
        {
            "name": "Graph",
            "description": "desc",
            "expected_outcome": "outcome",
            "nodes": [
                {
                    "node_id": "n1",
                    "name": "N1",
                    "description": "N1 description",
                    "expected_outcome": "N1 outcome",
                    "deps": [],
                },
                {
                    "node_id": "n2",
                    "name": "N2",
                    "description": "changed",
                    "expected_outcome": "N2 outcome",
                    "deps": ["n1"],
                },
                {
                    "node_id": "n3",
                    "name": "N3",
                    "description": "N3 description",
                    "expected_outcome": "N3 outcome",
                    "deps": ["n2"],
                },
            ],
        },
    )

    node_map = {node.id: node for node in nodes}
    assert diff.modified == ["n2"]
    assert diff.downstream_reset == ["n3"]
    assert node_map["n2"].state == "todo"
    assert node_map["n3"].state == "todo"


def test_load_state_dict_migrates_stale_nodes_to_todo() -> None:
    state = RuntimeStateManager()
    node = _node("n1", "N1", state="todo").model_dump(mode="json")
    node["state"] = "stale"

    state.load_state_dict(
        {
            "current_graph_id": GRAPH_ID,
            "graph_registry": {},
            "nodes": [node],
        },
    )

    assert len(state._nodes) == 1
    assert state._nodes[0].state == "todo"

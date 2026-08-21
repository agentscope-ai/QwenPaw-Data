from __future__ import annotations

import pytest

from qwenpaw_data.host.core.orchestration import RuntimeStateManager
from qwenpaw_data.host.core.orchestration.task_graph import SOP, nodes_to_sop


@pytest.mark.asyncio
async def test_create_plan_normalizes_unique_node_name_deps() -> None:
    rs = RuntimeStateManager()

    result = await rs.create_plan(
        name="访问趋势分析",
        description="分析 webapp 访问趋势",
        expected_outcome="输出可执行分析 SOP",
        nodes=[
            {
                "node_id": "fetch_data",
                "name": "数据获取与探查",
                "description": "获取并探查数据",
                "expected_outcome": "可用的数据集",
            },
            {
                "node_id": "analyze_trend",
                "name": "趋势分析",
                "description": "分析趋势",
                "expected_outcome": "趋势结论",
                "deps": ["数据获取与探查"],
            },
        ],
    )

    assert result.state != "error"
    graph_id = rs.current_graph_id
    assert graph_id is not None
    sop = nodes_to_sop(rs._nodes, graph_id, rs._graph_registry)
    assert sop.nodes[1].deps == ["fetch_data"]
    assert SOP.from_dict(sop.to_dict()).nodes[1].deps == ["fetch_data"]


@pytest.mark.asyncio
async def test_create_plan_rejects_unknown_deps_without_mutating_state() -> None:
    rs = RuntimeStateManager()

    result = await rs.create_plan(
        name="访问趋势分析",
        description="分析 webapp 访问趋势",
        expected_outcome="输出可执行分析 SOP",
        nodes=[
            {
                "node_id": "analyze_trend",
                "name": "趋势分析",
                "description": "分析趋势",
                "expected_outcome": "趋势结论",
                "deps": ["不存在的节点"],
            },
        ],
    )

    assert result.state == "error"
    assert rs.current_graph_id is None
    assert rs._nodes == []


@pytest.mark.asyncio
async def test_revise_current_plan_normalizes_unique_node_name_deps() -> None:
    rs = RuntimeStateManager()
    await rs.create_plan(
        name="访问趋势分析",
        description="分析 webapp 访问趋势",
        expected_outcome="输出可执行分析 SOP",
        nodes=[
            {
                "node_id": "fetch_data",
                "name": "数据获取与探查",
                "description": "获取并探查数据",
                "expected_outcome": "可用的数据集",
            },
            {
                "node_id": "analyze_trend",
                "name": "趋势分析",
                "description": "分析趋势",
                "expected_outcome": "趋势结论",
            },
        ],
    )

    result = await rs.revise_current_plan(
        changes=[
            {
                "node_id": "analyze_trend",
                "action": "revise",
                "node": {
                    "name": "趋势分析",
                    "description": "分析趋势",
                    "expected_outcome": "趋势结论",
                    "deps": ["数据获取与探查"],
                },
            },
        ],
    )

    assert result.state != "error"
    graph_id = rs.current_graph_id
    assert graph_id is not None
    sop = nodes_to_sop(rs._nodes, graph_id, rs._graph_registry)
    assert sop.nodes[1].deps == ["fetch_data"]

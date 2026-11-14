"""Develop query contracts at the Store boundary, without a Cypher emulator."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, create_autospec

import pytest
from context_manager.auth import GraphScopeAuthority
from context_manager.compatibility.explorer import ExplorerService
from context_manager.compatibility.explorer.graph_tools import (
    find_anchors,
)
from context_manager.compatibility.explorer.journey import run_explore_sse
from context_manager.schema import GRAPH_SCHEMA, METRIC
from context_manager.store import GraphStore, NodeRecord, StoreError

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor


@pytest.fixture
def graph():
    store = create_autospec(GraphStore, instance=True)
    store.backend = "neo4j"
    store.schema = GRAPH_SCHEMA
    store.access = GraphScopeAuthority()
    scope = store.access.issue_read_scope(authorization("alice"), owner_id="alice")
    store.list_nodes.return_value = ()
    return ExplorerService(store, executor=executor), store, scope


def test_fulltext_keeps_per_label_top_k_and_global_score_order(graph):
    service, store, scope = graph
    store.execute_query.side_effect = [
        ({"node": {"key": "metric", "name": "Revenue"}, "score": 0.3},),
        ({"node": {"key": "dimension", "name": "Revenue segment"}, "score": 0.9},),
    ]
    result = find_anchors(
        service.access(scope), "revenue", labels=["Metric", "Dimension"], k=1
    )
    assert [n["key"] for n in result["anchors"]] == ["dimension", "metric"]
    assert [n["score"] for n in result["anchors"]] == [0.9, 0.3]
    assert result["searched_labels"] == ["Metric", "Dimension"]
    for call in store.execute_query.call_args_list:
        assert "ORDER BY score DESC LIMIT $k" in call.args[0]
        assert call.args[1]["scope_owner_ids"] == ["alice"]
        assert call.args[1]["scope_workspace_id"] == "workspace-1"
        assert call.args[1]["k"] == 1
        assert call.kwargs["scope"] is scope


@pytest.mark.asyncio
async def test_exploration_returns_tool_error_to_the_model_so_it_can_correct_the_next_call(
    graph,
):
    service, store, scope = graph
    tool = SimpleNamespace(
        id="expand",
        function=SimpleNamespace(
            name="expand_subgraph",
            arguments='{"seeds":["metric"],"relations":["NOT_A_RELATION"]}',
        ),
    )
    messages = [
        SimpleNamespace(content=None, tool_calls=[tool]),
        SimpleNamespace(content="Corrected report", tool_calls=[]),
    ]
    create = AsyncMock(
        side_effect=[
            SimpleNamespace(choices=[SimpleNamespace(message=message)])
            for message in messages
        ]
    )
    sdk = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    events = [
        event
        async for event in run_explore_sse(
            service.access(scope), "revenue", sdk, "test", executor=executor
        )
    ]
    failed = next(
        json.loads(event.split("data: ", 1)[1])
        for event in events
        if event.startswith("event: tool_result")
    )
    assert failed["ok"] is False
    error = json.loads(failed["result"])
    assert "illegal relation" in error["error"] and "NOT_A_RELATION" in error["error"]
    feedback = next(
        message
        for message in create.await_args_list[1].kwargs["messages"]
        if message["role"] == "tool"
    )
    assert json.loads(feedback["content"]) == error
    assert events[-2].startswith("event: final") and events[-1].startswith(
        "event: done"
    )
    store.execute_query.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [None, "index_unavailable", "invalid_query", "unavailable"]
)
async def test_exploration_falls_back_only_when_no_requested_index_is_available(
    graph, failure
):
    service, store, scope = graph

    def query(cypher, parameters, *, scope):
        if "db.index.fulltext.queryNodes" in cypher and failure:
            raise StoreError(
                failure,
                operation="search_fulltext",
                code=failure,
            )
        return ()

    store.execute_query.side_effect = query
    store.list_nodes.side_effect = lambda spec, **kwargs: (
        (
            NodeRecord(
                METRIC, {"key": "metric:revenue", "name": "Revenue", "zone": "metadata"}
            ),
        )
        if spec is METRIC
        else ()
    )
    tool = SimpleNamespace(
        id="anchor",
        function=SimpleNamespace(
            name="find_anchors", arguments='{"query":"revenue", "labels":["Metric"]}'
        ),
    )
    messages = [
        SimpleNamespace(content=None, tool_calls=[tool]),
        SimpleNamespace(content="Done", tool_calls=[]),
    ]
    sdk = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=AsyncMock(
                    side_effect=[
                        SimpleNamespace(choices=[SimpleNamespace(message=m)])
                        for m in messages
                    ]
                )
            )
        )
    )
    events = [
        e
        async for e in run_explore_sse(
            service.access(scope), "revenue", sdk, "test", executor=executor
        )
    ]
    event = next(
        json.loads(e.split("data: ", 1)[1])
        for e in events
        if e.startswith("event: tool_result")
    )
    result = json.loads(event["result"])
    if failure in {"invalid_query", "unavailable"}:
        assert event["ok"] is False
        assert failure in result["error"]
        store.list_nodes.assert_not_called()
        return
    assert event["ok"]
    assert (result.get("method") == "contains_scan_fallback") is bool(failure)
    assert [node["key"] for node in result["anchors"]] == (
        ["metric:revenue"] if failure else []
    )

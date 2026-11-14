"""Verify merging scored index candidates across selected node types."""

from unittest.mock import Mock

import pytest
from context_manager.auth import GraphScopeAuthority
from context_manager.schema import DATASET, GRAPH_SCHEMA, METRIC, TABLE
from context_manager.store.neo4j import GraphTarget, Neo4jGraphStore

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.schema_support import make_glossary


@pytest.mark.parametrize(
    "kind, candidate, tied_keys",
    [("fulltext", TABLE, ["m3", "c1"]), ("vector", DATASET, ["c1", "m3"])],
)
def test_search_merges_scores_without_type_quotas_and_restricts_candidates(
    kind, candidate, tied_keys
):
    runtime = Mock(target=GraphTarget("tenant-1", "workspace-1"))
    authority = GraphScopeAuthority()
    store = Neo4jGraphStore(
        runtime, GRAPH_SCHEMA, authority, make_glossary(GRAPH_SCHEMA)
    )
    scope = authority.issue_read_scope(authorization(), owner_id="user-1")
    other = [{"properties": {"key": "c1"}, "score": 0.2}]
    metrics = [
        {"properties": {"key": "m1"}, "score": 0.9},
        {"properties": {"key": "m2"}, "score": 0.8},
        {"properties": {"key": "m3"}, "score": 0.2},
    ]
    search = getattr(store, f"search_{kind}")
    query = "revenue" if kind == "fulltext" else (0.25,) * 1024
    runtime.read.side_effect = [other, metrics]
    result = search(query, specs=(candidate, METRIC, candidate), limit=2, scope=scope)
    assert [node.key for node in result] == ["m1", "m2"]
    runtime.read.side_effect = [other, metrics]
    result = search(query, specs=(candidate, METRIC), limit=4, scope=scope)
    assert [node.key for node in result] == ["m1", "m2", *tied_keys]
    runtime.read.side_effect = [other]
    result = search(query, specs=(candidate,), limit=2, scope=scope)
    assert [node.key for node in result] == ["c1"]

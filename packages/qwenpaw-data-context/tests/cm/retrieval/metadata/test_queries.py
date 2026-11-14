"""Verify retained Metadata composition directly against scoped graph facts."""

import pytest
from context_manager.compatibility import metadata as queries
from context_manager.compatibility.metadata import MetadataHttpReader
from context_manager.compatibility.metadata import queries as query_impl
from context_manager.schema import (
    CALIBER_ON,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DIMENSION_BINDING,
    DOMAIN,
    HAS_CALIBER,
    HAS_DATASET,
    HAS_METRIC,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
)

from cm.infrastructure.access.support import authorization
from cm.knowledge.support import MemoryGraphStore


def _node(store, spec, key, **properties):
    store.nodes[spec, key] = {
        "key": key,
        "name": key,
        "domain": "sales",
        "workspace_id": "workspace-1",
        "owner_id": "__global__",
        "zone": "metadata",
        "datasource_id": "source-1",
        "embedding": [0.1],
        "embedding_hash": "private-hash",
        **properties,
    }
    if spec is METRIC:
        _edge(store, HAS_METRIC, "dom:sales", key)


def _edge(store, spec, source, target, **properties):
    store.edges[spec, source, target] = {
        "workspace_id": "workspace-1",
        "owner_id": "__global__",
        "zone": "metadata",
        "datasource_id": "source-1",
        **properties,
    }


@pytest.fixture
def graph():
    store = MemoryGraphStore()
    scope = store.access.issue_read_scope(
        authorization(),
        zone="metadata",
        owner_id="__global__",
        datasource_id="source-1",
    )
    _node(store, DOMAIN, "dom:sales", domain_id="sales", name="Sales display name")
    for key, children in (
        ("Geography", ["Country"]),
        ("Country", ["City", "Unregistered"]),
        ("City", []),
        ("Channel", []),
    ):
        _node(store, DIMENSION, key, child_dimensions=children)
    for key in ("Orders", "Returns"):
        _node(store, DATASET, key)
        _edge(store, HAS_DATASET, "dom:sales", key)
    for key, dataset in (
        ("field-country-1", "Orders"),
        ("field-country-2", "Returns"),
        ("field-city", "Orders"),
    ):
        _node(store, DATASET_FIELD, key, dataset=dataset)
        _edge(store, DATASET_HAS_COLUMN_DATASET_FIELD, dataset, key)
    _edge(
        store,
        MAPS_TO_FIELD,
        "Country",
        "field-country-1",
        expr="country",
        value_example=["CN", "US", "ALL"],
        total_value_marker="ALL",
    )
    _edge(
        store,
        MAPS_TO_FIELD,
        "Country",
        "field-country-2",
        expr="country",
        value_example=["US", "CA"],
    )
    _edge(
        store,
        MAPS_TO_FIELD,
        "City",
        "field-city",
        expr="city",
        value_example=["Beijing"],
    )
    _edge(
        store,
        DIMENSION_BINDING,
        "Country",
        "Orders",
        expr="country",
        value_example=["ALL", "GB"],
    )
    _edge(
        store,
        DIMENSION_BINDING,
        "Channel",
        "Orders",
        expr="channel",
        value_example=["web"],
    )
    for key, priority in (
        ("Revenue", "high"),
        ("Volume", "normal"),
        ("Orphan", "normal"),
    ):
        _node(store, METRIC, key, priority=priority)
    for key, metric, dataset in (
        ("caliber-1", "Revenue", "Orders"),
        ("caliber-2", "Revenue", "Orders"),
        ("caliber-3", "Volume", "Returns"),
    ):
        _node(store, METRIC_CALIBER, key, metric_name=metric, dataset=dataset)
        _edge(store, HAS_CALIBER, metric, key)
        _edge(store, CALIBER_ON, key, dataset)
    return store, scope


def test_hierarchy_resolves_only_visible_same_domain_names_and_continues(
    graph, monkeypatch
):
    store, scope = graph
    monkeypatch.setattr(query_impl, "_PAGE_SIZE", 2)
    _node(store, DIMENSION, "Area", child_dimensions=["Country"])
    _node(store, DIMENSION, "OtherParent", domain="other", child_dimensions=["Country"])
    _node(
        store,
        DIMENSION,
        "PrivateParent",
        owner_id="user-1",
        child_dimensions=["Country"],
    )
    _node(store, DIMENSION, "OtherCity", name="City", domain="other")
    result = queries.get_dimension_hierarchy(
        MetadataHttpReader(store, scope), key="Country", limit=1, offset=0
    )
    assert [node["key"] for node in result["parents"]] == ["Area"]
    assert [node["key"] for node in result["children"]] == ["City"]
    assert (result["parent_count"], result["child_count"], result["next_offset"]) == (
        2,
        1,
        1,
    )
    result = queries.get_dimension_hierarchy(
        MetadataHttpReader(store, scope), key="Country", limit=1, offset=1
    )
    assert [node["key"] for node in result["parents"]] == ["Geography"]
    assert result["children"] == []
    assert result["next_offset"] is None


def test_values_merge_real_field_and_direct_bindings_preserving_order_and_markers(
    graph, monkeypatch
):
    store, scope = graph
    monkeypatch.setattr(query_impl, "_PAGE_SIZE", 2)
    first = queries.get_dimension_values(
        MetadataHttpReader(store, scope), key="Country", limit=3, offset=0
    )
    assert first["values"] == [
        {"value": "CN", "is_rollup_sentinel": False},
        {"value": "US", "is_rollup_sentinel": False},
        {"value": "ALL", "is_rollup_sentinel": True},
    ]
    assert (first["total_count"], first["next_offset"]) == (5, 3)
    second = queries.get_dimension_values(
        MetadataHttpReader(store, scope), key="Country", limit=3, offset=3
    )
    assert [row["value"] for row in second["values"]] == ["CA", "GB"]
    assert second["next_offset"] is None
    empty = queries.get_dimension_values(
        MetadataHttpReader(store, scope), key="Country", limit=3, offset=99
    )
    assert empty["values"] == [] and empty["next_offset"] is None


def test_associations_follow_both_current_paths_and_paginate_after_deduplication(
    graph, monkeypatch
):
    store, scope = graph
    monkeypatch.setattr(query_impl, "_PAGE_SIZE", 1)
    first = queries.list_dimensions_of_metric(
        MetadataHttpReader(store, scope), key="Revenue", limit=2, offset=0
    )
    assert [row["key"] for row in first["dimensions"]] == ["Channel", "City"]
    assert (first["total_count"], first["next_offset"]) == (3, 2)
    second = queries.list_dimensions_of_metric(
        MetadataHttpReader(store, scope), key="Revenue", limit=2, offset=2
    )
    assert [row["key"] for row in second["dimensions"]] == ["Country"]
    assert second["next_offset"] is None
    reverse = queries.list_metrics_of_dimension(
        MetadataHttpReader(store, scope), key="Country", limit=1, offset=0
    )
    assert [row["key"] for row in reverse["metrics"]] == ["Revenue"]
    assert (reverse["total_count"], reverse["next_offset"]) == (2, 1)
    reverse = queries.list_metrics_of_dimension(
        MetadataHttpReader(store, scope), key="Country", limit=1, offset=1
    )
    assert [row["key"] for row in reverse["metrics"]] == ["Volume"]
    assert reverse["next_offset"] is None


@pytest.mark.parametrize(
    "properties",
    [
        {"owner_id": "user-2"},
        {"workspace_id": "workspace-2"},
        {"datasource_id": "source-2"},
    ],
)
@pytest.mark.parametrize(
    "hidden",
    [
        "caliber",
        "dataset",
        "field",
        "caliber_edge",
        "dataset_edge",
        "field_edge",
        "binding",
    ],
)
def test_each_intermediate_node_and_edge_remains_scoped(graph, properties, hidden):
    store, scope = graph
    store.nodes = {
        item: value for item, value in store.nodes.items() if item[1] != "caliber-2"
    }
    targets = {
        "caliber": store.nodes[METRIC_CALIBER, "caliber-1"],
        "dataset": store.nodes[DATASET, "Orders"],
        "field": store.nodes[DATASET_FIELD, "field-city"],
        "caliber_edge": store.edges[HAS_CALIBER, "Revenue", "caliber-1"],
        "dataset_edge": store.edges[CALIBER_ON, "caliber-1", "Orders"],
        "field_edge": store.edges[
            DATASET_HAS_COLUMN_DATASET_FIELD, "Orders", "field-city"
        ],
        "binding": store.edges[MAPS_TO_FIELD, "City", "field-city"],
    }
    targets[hidden].update(properties)
    forward = queries.list_dimensions_of_metric(
        MetadataHttpReader(store, scope), key="Revenue", limit=10, offset=0
    )
    assert "City" not in {node["key"] for node in forward["dimensions"]}
    reverse = queries.list_metrics_of_dimension(
        MetadataHttpReader(store, scope), key="City", limit=10, offset=0
    )
    assert reverse["metrics"] == []

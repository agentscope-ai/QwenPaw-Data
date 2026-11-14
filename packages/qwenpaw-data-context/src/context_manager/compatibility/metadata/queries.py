"""Assemble legacy semantic queries from scoped, current-schema Store reads."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING

from ...schema import (
    CALIBER_ON,
    DATASET,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DIMENSION_BINDING,
    DOMAIN,
    HAS_CALIBER,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
    DisplayPolicy,
    EdgeSpec,
    NodeSpec,
)
from ...auth import ScopeBinding
from ...store import EdgeRecord, GraphStore, NeighborDirection, NodeRecord

if TYPE_CHECKING:
    from .reader import MetadataHttpReader

_PAGE_SIZE = 1000


def public_properties(record: NodeRecord | EdgeRecord) -> dict[str, object]:
    """Project only declared, displayable properties."""
    visible = {
        prop.name
        for prop in record.spec.properties
        if prop.display_policy is not DisplayPolicy.NEVER
    }
    return {name: value for name, value in record.properties.items() if name in visible}


def _nodes(
    store: GraphStore, spec: NodeSpec, filters: dict, scope: ScopeBinding
) -> Iterator[NodeRecord]:
    """Read every scoped page, including the probe after a full page."""
    offset = 0
    while True:
        page = store.list_nodes(
            spec, filters=filters, limit=_PAGE_SIZE, offset=offset, scope=scope
        )
        yield from page
        if len(page) < _PAGE_SIZE:
            return
        offset += len(page)


def _require_node(reader: MetadataHttpReader, spec: NodeSpec, key: str) -> NodeRecord:
    node = reader.store.get_nodes(spec, (key,), scope=reader.scope)[0]
    if node is None or not reader.contains(node, orphan_metric=spec is METRIC):
        raise LookupError("Metadata node not found")
    return node


def _neighbors(
    store: GraphStore,
    node: NodeRecord,
    direction: NeighborDirection,
    scope: ScopeBinding,
) -> Iterator[tuple[NodeRecord, str]]:
    """Page raw neighbors before selecting a relationship."""
    offset = 0
    while True:
        page = store.expand_neighbors(
            node.spec,
            node.key,
            direction=direction,
            limit=_PAGE_SIZE,
            offset=offset,
            scope=scope,
        )
        yield from ((neighbor, edge.spec.relationship) for neighbor, edge in page)
        if len(page) < _PAGE_SIZE:
            return
        offset += len(page)


def _follow(
    store: GraphStore,
    nodes: Iterable[NodeRecord],
    edge: EdgeSpec,
    direction: NeighborDirection,
    domain: str,
    scope: ScopeBinding,
) -> list[NodeRecord]:
    """Follow one semantic relation within the seed's domain, deduplicating keys."""
    target = edge.target if direction is NeighborDirection.OUTGOING else edge.source
    found = {}
    for node in {node.key: node for node in nodes}.values():
        for neighbor, relationship in _neighbors(store, node, direction, scope):
            if (
                neighbor.spec is target
                and relationship == edge.relationship
                and neighbor.properties.get("domain") == domain
            ):
                found[neighbor.key] = neighbor
    return list(found.values())


def _node_page(
    nodes: Iterable[NodeRecord], collection: str, limit: int, offset: int
) -> dict:
    """Sort and paginate unique accepted results rather than intermediate hops."""
    ordered = sorted(
        {node.key: node for node in nodes}.values(),
        key=lambda node: (node.properties.get("name") or "", node.key),
    )
    return {
        collection: [
            public_properties(node) for node in ordered[offset : offset + limit]
        ],
        "total_count": len(ordered),
        "next_offset": offset + limit if offset + limit < len(ordered) else None,
    }


def list_metrics(
    reader: MetadataHttpReader,
    *,
    domain: str,
    limit: int,
    offset: int,
    priority: bool = False,
) -> list[dict]:
    """Keep develop's domain-level datasource gate and high-priority ordering."""
    if (
        priority
        and reader.datasource_id
        and not any(
            node.properties["domain_id"] == domain and reader.contains(node)
            for node in reader._all(DOMAIN)
        )
    ):
        raise LookupError("Metadata node not found")
    nodes = reader.metric_nodes(domain)
    if priority:
        nodes = [node for node in nodes if node.properties.get("priority") == "high"]
    return [public_properties(node) for node in nodes[offset : offset + limit]]


def get_metric(
    reader: MetadataHttpReader, *, key: str, limit: int, offset: int
) -> dict:
    """Select the metric by relations, then retain all authorized caliber rows like develop."""
    record = _require_node(reader, METRIC, key)
    if reader.datasource_id and not any(
        node.properties["domain_id"] == record.properties["domain"]
        and reader.contains(node)
        for node in reader._all(DOMAIN)
    ):
        raise LookupError("Metadata node not found")
    page = reader.store.expand_neighbors(
        METRIC,
        key,
        direction=NeighborDirection.OUTGOING,
        limit=limit,
        offset=offset,
        scope=reader.scope,
    )
    return {
        **public_properties(record),
        "metric_calibers": [
            public_properties(node)
            for node, edge in page
            if node.spec is METRIC_CALIBER
            and edge.spec.relationship == HAS_CALIBER.relationship
        ],
        "next_offset": offset + len(page) if len(page) == limit else None,
    }


def get_domain_overview(reader: MetadataHttpReader, *, domain: str, limit: int) -> dict:
    """Require a selected domain; count each collection using develop's selection rule."""
    store, scope = reader.store, reader.scope
    roots = store.list_nodes(
        DOMAIN, filters={"domain_id": domain}, limit=1, scope=scope
    )
    if not roots or not reader.contains(roots[0]):
        raise LookupError("Metadata node not found")
    result = {"domain": public_properties(roots[0]), "truncated": False}
    for spec, count_name, collection in (
        (METRIC, "metric_count", "priority_metrics"),
        (DIMENSION, "dimension_count", "top_dimensions"),
        (DATASET, "dataset_count", "datasets"),
    ):
        count = 0
        summaries = []
        nodes = (
            reader.metric_nodes(domain)
            if spec is METRIC
            else (
                node
                for node in _nodes(store, spec, {"domain": domain}, scope)
                if reader.contains(node)
            )
        )
        for node in nodes:
            count += 1
            if spec is METRIC and node.properties.get("priority") != "high":
                continue
            if len(summaries) < limit:
                summaries.append(public_properties(node))
            else:
                result["truncated"] = True
        result[count_name] = count
        result[collection] = summaries
    return result


def get_dimension_hierarchy(
    reader: MetadataHttpReader,
    *,
    key: str,
    limit: int,
    offset: int,
) -> dict:
    """Resolve declared children and inverse parents among visible domain nodes."""
    store, scope = reader.store, reader.scope
    seed = _require_node(reader, DIMENSION, key)
    child_names = set(seed.properties.get("child_dimensions") or ())
    parents, children = [], []
    for node in _nodes(store, DIMENSION, {"domain": seed.properties["domain"]}, scope):
        if node.properties["name"] in child_names:
            children.append(node)
        # develop filters parents, but returns declared children without datasource narrowing.
        if reader.contains(node) and seed.properties["name"] in (
            node.properties.get("child_dimensions") or ()
        ):
            parents.append(node)
    parent_page = _node_page(parents, "parents", limit, offset)
    child_page = _node_page(children, "children", limit, offset)
    return {
        "key": seed.key,
        "dimension_name": seed.properties["name"],
        "parents": parent_page["parents"],
        "children": child_page["children"],
        "parent_count": parent_page["total_count"],
        "child_count": child_page["total_count"],
        "next_offset": parent_page["next_offset"] or child_page["next_offset"],
    }


def get_dimension_values(
    reader: MetadataHttpReader,
    *,
    key: str,
    limit: int,
    offset: int,
) -> dict:
    """Read binding examples in first-seen order and retain stored rollup markers."""
    store, scope = reader.store, reader.scope
    seed = _require_node(reader, DIMENSION, key)
    neighbors = list(_neighbors(store, seed, NeighborDirection.OUTGOING, scope))
    values, markers = {}, set()
    for edge in (MAPS_TO_FIELD, DIMENSION_BINDING):
        endpoints = tuple(
            dict.fromkeys(
                (key, node.key)
                for node, relationship in neighbors
                if node.spec is edge.target
                and relationship == edge.relationship
                and node.properties.get("domain") == seed.properties["domain"]
            )
        )
        for start in range(0, len(endpoints), _PAGE_SIZE):
            bindings = store.get_edges(
                edge, endpoints[start : start + _PAGE_SIZE], scope=scope
            )
            for binding in bindings:
                if binding is None:
                    continue
                marker = (binding.properties.get("total_value_marker") or "").strip()
                if marker:
                    markers.add(marker)
                for value in binding.properties.get("value_example") or ():
                    text = value.strip()
                    if text:
                        values.setdefault(text, None)
    ordered = list(values)
    return {
        "key": seed.key,
        "dimension_name": seed.properties["name"],
        "values": [
            {"value": value, "is_rollup_sentinel": value in markers}
            for value in ordered[offset : offset + limit]
        ],
        "total_count": len(ordered),
        "next_offset": offset + limit if offset + limit < len(ordered) else None,
    }


def list_dimensions_of_metric(
    reader: MetadataHttpReader,
    *,
    key: str,
    limit: int,
    offset: int,
) -> dict:
    """Resolve dimensions through the metric's dataset-bound calibers."""
    store, scope = reader.store, reader.scope
    seed = _require_node(reader, METRIC, key)
    domain = seed.properties["domain"]
    out, incoming = NeighborDirection.OUTGOING, NeighborDirection.INCOMING
    calibers = _follow(store, (seed,), HAS_CALIBER, out, domain, scope)
    datasets = _follow(store, calibers, CALIBER_ON, out, domain, scope)
    fields = _follow(
        store, datasets, DATASET_HAS_COLUMN_DATASET_FIELD, out, domain, scope
    )
    dimensions = _follow(store, fields, MAPS_TO_FIELD, incoming, domain, scope)
    dimensions += _follow(store, datasets, DIMENSION_BINDING, incoming, domain, scope)
    return {
        "key": key,
        **_node_page(
            (node for node in dimensions if reader.contains(node)),
            "dimensions",
            limit,
            offset,
        ),
    }


def list_metrics_of_dimension(
    reader: MetadataHttpReader,
    *,
    key: str,
    limit: int,
    offset: int,
) -> dict:
    """Reverse the same dataset/field paths to find visible related metrics."""
    store, scope = reader.store, reader.scope
    seed = _require_node(reader, DIMENSION, key)
    domain = seed.properties["domain"]
    out, incoming = NeighborDirection.OUTGOING, NeighborDirection.INCOMING
    fields = _follow(store, (seed,), MAPS_TO_FIELD, out, domain, scope)
    datasets = _follow(
        store, fields, DATASET_HAS_COLUMN_DATASET_FIELD, incoming, domain, scope
    )
    datasets += _follow(store, (seed,), DIMENSION_BINDING, out, domain, scope)
    calibers = _follow(store, datasets, CALIBER_ON, incoming, domain, scope)
    metrics = _follow(store, calibers, HAS_CALIBER, incoming, domain, scope)
    return {
        "key": key,
        **_node_page(
            (node for node in metrics if reader.contains(node)),
            "metrics",
            limit,
            offset,
        ),
    }

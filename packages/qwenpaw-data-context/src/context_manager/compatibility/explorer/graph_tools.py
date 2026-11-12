"""Develop's fulltext anchor search and paged graph expansion."""

from __future__ import annotations

import logging
from typing import Any

from ...store import StoreError, legacy_reads
from .access import ExplorerAccess
from .display import HIDDEN_FIELDS

log = logging.getLogger(__name__)
# Limit traversal to the relationships supported by Explorer.
_ALLOWED_REL_TYPES = frozenset(
    {
        "ANCHORED_TO",
        "ASSOCIATION",
        "BELONGS_TO",
        "CALIBER_ON",
        "COMPOSITION",
        "CONSTRAINS",
        "CONTAINS",
        "HAS",
        "HIERARCHY",
        "INFLUENCE",
        "JOINS",
        "MAPS_TO_COLUMN",
        "MAPS_TO_FIELD",
        "PARTICIPATION",
        "USES_COLUMN",
    }
)


def find_anchors(
    access: ExplorerAccess, query: str, *, labels: list[str] | None = None, k: int = 8
) -> dict[str, Any]:
    """Preserve per-label top-k and native score ordering for the legacy tool."""
    query = (query or "").strip()
    result = {"anchors": [], "searched_labels": [], "unsearchable_labels": []}
    if not query:
        return result
    available = {
        spec.name: spec
        for spec in access.store.schema.nodes
        if not spec.internal
        and spec.fulltext_fields
        and spec.name
        in {"Metric", "Dimension", "DatasetField", "Dataset", "Event", "Entity"}
    }
    requested = list(labels) if labels else list(available)
    seen = set()
    for label in requested:
        spec = available.get(label)
        if spec is None:
            result["unsearchable_labels"].append(label)
            continue
        try:
            rows = legacy_reads.search_fulltext_scores(
                access.store, spec, query, limit=k, scope=access.scope
            )
        except StoreError as exc:
            if exc.code != "index_unavailable":
                raise
            log.debug("anchor search for %s failed: %s", label, exc)
            result["unsearchable_labels"].append(label)
            continue
        result["searched_labels"].append(label)
        for row in rows:
            node = row["node"]
            key = node.get("key") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            result["anchors"].append(
                {
                    "label": label,
                    "key": key,
                    "name": str(node.get("name") or node.get("description") or key),
                    "score": float(row["score"]),
                }
            )
    result["anchors"].sort(key=lambda row: row["score"], reverse=True)
    return result


#: properties() 返回中需要排除的系统 / 大体积字段（所有 label 通用）
_EXCLUDE_NODE_PROPS: frozenset[str] = frozenset(
    {
        "key",  # 已作为 nodes dict 的 key
        "embedding",
        "embedding_model",
        "embedding_dim",
        "embedding_hash",
        "zone",  # 图层枚举标识（metadata/knowledge/user），对取数决策无业务意义
        "domain_orig",  # 与 domain 字段 100% 重复（写入端双写，见 semantic_incremental_write）
        "anomaly_rules_json",  # Metric 上的预留字段，全量为 '[]'
        "filter_summary",  # Dataset 上的预留字段，全量为 ''
    }
)

#: 某些字段只在特定 label 上冗余（恒为常量 / 与另一字段 100% 相等）。
#: mgw6-w4ready：旧 Formula 的 expression/expression_lang 双写条目随 label
#: 退役清零（MetricCaliber 写入面只落 formula，无双写）。
_EXCLUDE_NODE_PROPS_BY_LABEL: dict[str, frozenset[str]] = {}


def _clean_node_props(raw: dict[str, Any], label: str = "") -> dict[str, Any]:
    """保留业务属性，剔除系统字段和 None 值。

    label 用于按 label 过滤冗余字段（见 _EXCLUDE_NODE_PROPS_BY_LABEL）。
    """
    extra = _EXCLUDE_NODE_PROPS_BY_LABEL.get(label, frozenset())
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k in HIDDEN_FIELDS or k in _EXCLUDE_NODE_PROPS or k in extra or v is None:
            continue
        if hasattr(v, "isoformat"):
            v = v.isoformat()
        elif isinstance(v, (list, tuple)):
            v = [x.isoformat() if hasattr(x, "isoformat") else x for x in v]
        out[k] = v
    return out


def expand_subgraph(
    access: ExplorerAccess,
    seeds: list[str],
    *,
    relations: list[str] | None = None,
    depth: int = 1,
    limit: int = 200,
    page: int = 0,
) -> dict[str, Any]:
    """Page unique relation triples reachable through scoped one/two-hop paths."""
    if not seeds:
        return {
            "nodes": {},
            "edges": [],
            "count": 0,
            "page": {"index": 0, "size": 0, "total": 0, "has_more": False},
        }
    if relations:
        bad = [name for name in relations if name not in _ALLOWED_REL_TYPES]
        if bad:
            raise ValueError(
                f"illegal relation type(s): {bad}. Allowed: {sorted(_ALLOWED_REL_TYPES)}"
            )
    allowed = set(relations or _ALLOWED_REL_TYPES)
    depth = max(1, min(int(depth), 2))
    size, index = max(1, min(int(limit), 1000)), max(0, int(page))
    roots = [node for key in seeds if (node := access.node(key)) is not None]
    selected = {}
    for root in roots:
        frontier = [(root, ())]
        for hop in range(depth):
            following = []
            for current, path in frontier:
                for node, edge in access.neighbors(current):
                    identity = (
                        edge.source_key,
                        edge.spec.relationship,
                        edge.target_key,
                    )
                    if edge.spec.relationship not in allowed or any(
                        identity
                        == (item.source_key, item.spec.relationship, item.target_key)
                        for item in path
                    ):
                        continue
                    extended = (*path, edge)
                    if (
                        node.key != root.key
                        and node.properties.get("status", "active") != "retired"
                    ):
                        for item in extended:
                            selected[
                                (
                                    item.source_key,
                                    item.spec.relationship,
                                    item.target_key,
                                )
                            ] = item
                    if hop + 1 < depth:
                        following.append((node, extended))
            frontier = following
    nodes = {
        node.key: {
            "label": node.spec.name,
            **_clean_node_props(node.properties, node.spec.name),
        }
        for node in roots
    }
    edges = []
    for identity in sorted(selected)[index * size : (index + 1) * size]:
        edge = selected[identity]
        for key, spec in (
            (edge.source_key, edge.spec.source),
            (edge.target_key, edge.spec.target),
        ):
            node = access.node(key, (spec.name,))
            if node is not None:
                nodes.setdefault(
                    key,
                    {
                        "label": spec.name,
                        **_clean_node_props(node.properties, spec.name),
                    },
                )
        edges.append(
            {
                "from": edge.source_key,
                "rel": edge.spec.relationship,
                "to": edge.target_key,
            }
        )
    pages = max(1, (len(selected) + size - 1) // size)
    return {
        "nodes": nodes,
        "edges": edges,
        "count": len(edges),
        "page": {
            "index": index,
            "size": size,
            "total": pages,
            "has_more": index + 1 < pages,
        },
    }


def graph_health(access):
    """Describe visible type counts without retaining node properties."""
    counts = {}
    for spec in access.specs():
        count = sum(1 for _ in access.nodes((spec.name,), fields=("key",)))
        if count:
            counts[spec.name] = count
    return {
        "labels": dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    }

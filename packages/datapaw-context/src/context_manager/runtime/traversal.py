"""Step 4: Legacy typed traversal helpers + weighted BFS fallback.

``instantiate_path_plan`` matches an explicit label/relation path pattern from anchors.

The topology pipeline builds candidates then calls :func:`context_manager.runtime.decision_llm.decide_with_path`
(rule-shaped hops + merged JSON for one or more concrete edge paths). Fallback: weighted BFS when
that yields no nodes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from neo4j import Driver

from ..config import TraversalEdgeDirection, normalize_traversal_edge_direction
from ..utils import get_logger, neo4j_session
from .anchors import AnchorSet

log = get_logger("runtime.traversal")


# ---------------------------------------------------------------------- #
# Data structures
# ---------------------------------------------------------------------- #

@dataclass
class TraversalSubgraph:
    """Result of typed traversal: collected node keys by label."""
    node_keys: list[str] = field(default_factory=list)
    nodes_by_label: dict[str, list[str]] = field(default_factory=dict)
    edges: list[dict[str, str]] = field(default_factory=list)
    method: str = "typed"  # "typed" | "bfs" | "card_cache" | "abstain"

    def has_results(self) -> bool:
        return bool(self.node_keys)

    def to_key_set(self) -> set[str]:
        return set(self.node_keys)


# ---------------------------------------------------------------------- #
# Path plan parsing
# ---------------------------------------------------------------------- #

# "Metric -[HAS_FORMULA]-> Formula" → ("Metric", "HAS_FORMULA", "Formula")
_PATH_STEP_RE = re.compile(
    r"^(\w+)\s+-\[(\w+)\]->\s+(\w+)$"
)


def _parse_path_plan(path_plan: list[str]) -> list[tuple[str, str, str]]:
    """Parse path plan steps into (src_label, rel_type, dst_label) tuples."""
    steps: list[tuple[str, str, str]] = []
    for step in path_plan:
        m = _PATH_STEP_RE.match(step.strip())
        if m:
            steps.append((m.group(1), m.group(2), m.group(3)))
        else:
            log.debug("Unparseable path plan step: %r", step)
    return steps


# ---------------------------------------------------------------------- #
# Typed traversal
# ---------------------------------------------------------------------- #

def _typed_traversal_cypher(
    session,
    anchor_key: str,
    anchor_label: str,
    steps: list[tuple[str, str, str]],
    max_nodes: int = 30,
) -> list[dict[str, Any]]:
    """Execute one path plan from a single anchor; return collected nodes."""
    if not steps:
        return []

    # Build Cypher pattern: MATCH (n0:L0 {key: $key}) -[:R1]-> (n1:L1) -[:R2]-> ...
    pattern_parts = [f"(n0:{anchor_label} {{key: $anchor_key}})"]
    with_parts = ["n0"]
    for i, (src, rel, dst) in enumerate(steps):
        idx = i + 1
        pattern_parts.append(f"-[:{rel}]-> (n{idx}:{dst})")
        with_parts.append(f"n{idx}")

    pattern = "".join(pattern_parts)
    return_expr = ", ".join(
        f"n{i}.key AS key_{i}, labels(n{i})[0] AS label_{i}"
        for i in range(len(steps) + 1)
    )
    where_clause = " AND ".join(
        f"(n{i}.valid_to IS NULL OR n{i}.valid_to > datetime())"
        for i in range(1, len(steps) + 1)
    )

    cypher = f"""
    MATCH {pattern}
    {'WHERE ' + where_clause if where_clause else ''}
    RETURN {return_expr}
    LIMIT {max_nodes}
    """
    try:
        rows = session.run(cypher, anchor_key=anchor_key).data()
    except Exception as exc:
        log.debug("typed traversal failed for anchor=%s steps=%s: %s", anchor_key, steps, exc)
        return []

    # Flatten rows to node list
    nodes: list[dict[str, Any]] = []
    for row in rows:
        for i in range(len(steps) + 1):
            key = row.get(f"key_{i}")
            label = row.get(f"label_{i}")
            if key and label:
                nodes.append({"key": key, "label": label})
    return nodes


def instantiate_path_plan(
    driver: Driver,
    anchors: AnchorSet,
    path_plan: list[str],
    *,
    max_nodes: int = 50,
) -> TraversalSubgraph:
    """Execute path_plan from all anchor nodes, merge results.

    Args:
        driver:     Neo4j driver.
        anchors:    AnchorSet from Step 1.
        path_plan:  LLM-generated relation sequence list.
        max_nodes:  Hard cap on collected node keys.

    Returns:
        TraversalSubgraph, possibly empty if traversal yields nothing.
    """
    steps = _parse_path_plan(path_plan)
    if not steps:
        log.warning("instantiate_path_plan: no parseable steps in %s", path_plan)
        return TraversalSubgraph(method="typed")

    all_nodes: dict[str, str] = {}  # key → label

    with neo4j_session(driver) as s:
        for anchor in anchors.anchors:
            # Only start traversal if anchor label matches path_plan first src_label
            expected_src = steps[0][0]
            if anchor.label != expected_src:
                continue
            nodes = _typed_traversal_cypher(
                s, anchor.key, anchor.label, steps, max_nodes=max_nodes
            )
            for n in nodes:
                if n["key"] not in all_nodes:
                    all_nodes[n["key"]] = n["label"]
            if len(all_nodes) >= max_nodes:
                break

    nodes_by_label: dict[str, list[str]] = {}
    for key, label in all_nodes.items():
        nodes_by_label.setdefault(label, []).append(key)

    return TraversalSubgraph(
        node_keys=list(all_nodes.keys()),
        nodes_by_label=nodes_by_label,
        method="typed",
    )


# ---------------------------------------------------------------------- #
# Weighted BFS fallback
# ---------------------------------------------------------------------- #

# Default relation weights (derived from domain knowledge; overridden by trace stats)
_DEFAULT_REL_WEIGHTS: dict[str, float] = {
    "HAS_FORMULA": 1.0,
    "OF_VIEW": 0.9,
    "CONTAINS_TABLE": 0.9,
    "HAS_COLUMN": 0.8,
    "ANALYZED_BY": 0.7,
    "MAPS_TO_COLUMN": 0.7,
    "JOINS_ON": 0.6,
    "USES_COLUMN": 0.6,
    "DERIVED_FROM": 0.5,
    "ABOUT": 0.3,
}

# Public alias for neighbor-edge ordering in candidate BFS.
DEFAULT_REL_WEIGHTS: dict[str, float] = _DEFAULT_REL_WEIGHTS


def relation_priority_score(rel_type: str | None) -> float:
    """Higher score = prefer keeping this relation when per-node caps apply."""
    if not rel_type:
        return 0.0
    return _DEFAULT_REL_WEIGHTS.get(str(rel_type), 0.1)


def cypher_relation_priority_case(rel_expr: str = "type(r)") -> str:
    """Cypher numeric expression for ``ORDER BY`` when capping neighbor edges."""
    whens = " ".join(
        f"WHEN {rel_expr} = '{rel}' THEN {w}"
        for rel, w in sorted(_DEFAULT_REL_WEIGHTS.items(), key=lambda x: -x[1])
    )
    return f"CASE {whens} ELSE 0.1 END"


def _fetch_relation_weights_from_traces(session) -> dict[str, float]:
    """Compute relation type frequencies from successful traces."""
    try:
        rows = session.run(
            """
        MATCH (t:Task {status: 'success'})-[:DECOMPOSES_INTO]->(p:Step)
            -[:EXECUTED_BY]->(tc:ToolCall)-[:PRODUCES]->(cl:Claim)
            -[:RESOLVED_TO]->(m)
            RETURN type(last(relationships(shortestPath((t)-[*]-(m))))) AS rel, count(*) AS cnt
            LIMIT 50
            """
        ).data()
        if rows:
            total = sum(r["cnt"] for r in rows)
            return {r["rel"]: r["cnt"] / total for r in rows if r["rel"]}
    except Exception:
        pass
    return {}


def weighted_bfs_fallback(
    driver: Driver,
    anchors: AnchorSet,
    *,
    max_depth: int = 10,
    max_nodes: int = 500,
    required_labels: Optional[list[str]] = None,
    edge_direction: TraversalEdgeDirection = "out",
) -> TraversalSubgraph:
    """Weighted BFS from anchor nodes when typed traversal fails.

    Args:
        driver:         Neo4j driver.
        anchors:        AnchorSet from Step 1.
        max_depth:      Maximum BFS depth.
        max_nodes:      Hard cap on explored nodes.
        required_labels: If set, only include nodes with these labels.
        edge_direction: ``out`` | ``in`` | ``both`` — 与 ``recall.traversal_edge_direction`` 一致。

    Returns:
        TraversalSubgraph (may be empty → caller should abstain).
    """
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    if not anchors.anchors:
        return TraversalSubgraph(method="abstain")

    anchor_keys = anchors.top_keys(5)
    if not anchor_keys:
        return TraversalSubgraph(method="abstain")

    # Label filter
    label_filter = ""
    if required_labels:
        label_filter = f"AND any(l IN labels(neighbor) WHERE l IN {required_labels!r})"

    cypher = f"""
    MATCH (start)
    WHERE start.key IN $anchor_keys
    CALL apoc.path.subgraphNodes(start, {{
        maxLevel: $depth,
        limit: $limit
    }}) YIELD node AS neighbor
    WHERE (neighbor.valid_to IS NULL OR neighbor.valid_to > datetime())
      {label_filter}
    RETURN neighbor.key AS key, labels(neighbor)[0] AS label
    LIMIT $limit
    """

    all_nodes: dict[str, str] = {}

    try:
        with neo4j_session(driver) as s:
            rows = s.run(
                cypher,
                anchor_keys=anchor_keys,
                depth=max_depth,
                limit=max_nodes,
            ).data()
        for row in rows:
            if row.get("key"):
                all_nodes[row["key"]] = row.get("label") or "Unknown"
    except Exception as exc:
        log.warning("weighted_bfs APOC failed (%s); using simple BFS", exc)
        all_nodes = _simple_bfs(
            driver, anchor_keys, max_depth, max_nodes, edge_direction=edge_direction
        )

    nodes_by_label: dict[str, list[str]] = {}
    for key, label in all_nodes.items():
        nodes_by_label.setdefault(label, []).append(key)

    method = "abstain" if not all_nodes else "bfs"
    return TraversalSubgraph(
        node_keys=list(all_nodes.keys()),
        nodes_by_label=nodes_by_label,
        method=method,
    )


def _simple_bfs(
    driver: Driver,
    start_keys: list[str],
    max_depth: int,
    max_nodes: int,
    *,
    edge_direction: TraversalEdgeDirection = "out",
) -> dict[str, str]:
    """Simple Cypher-based BFS without APOC."""
    edge_direction = normalize_traversal_edge_direction(edge_direction)
    all_nodes: dict[str, str] = {}
    frontier = list(start_keys)

    with neo4j_session(driver) as s:
        for _ in range(max_depth):
            if len(all_nodes) >= max_nodes or not frontier:
                break
            room = max_nodes - len(all_nodes)
            next_frontier: list[str] = []
            if edge_direction in ("out", "both"):
                rows = s.run(
                    """
                    MATCH (a)-[r]->(b)
                    WHERE a.key IN $keys
                      AND (b.valid_to IS NULL OR b.valid_to > datetime())
                    RETURN b.key AS key, labels(b)[0] AS label
                    LIMIT $lim
                    """,
                    keys=frontier,
                    lim=room,
                ).data()
                for row in rows:
                    k = row.get("key")
                    if k and k not in all_nodes:
                        all_nodes[k] = row.get("label") or "Unknown"
                        next_frontier.append(k)
            if edge_direction in ("in", "both"):
                rows = s.run(
                    """
                    MATCH (b)-[r]->(a)
                    WHERE a.key IN $keys
                      AND (b.valid_to IS NULL OR b.valid_to > datetime())
                    RETURN b.key AS key, labels(b)[0] AS label
                    LIMIT $lim
                    """,
                    keys=frontier,
                    lim=max(0, max_nodes - len(all_nodes)),
                ).data()
                for row in rows:
                    k = row.get("key")
                    if k and k not in all_nodes:
                        all_nodes[k] = row.get("label") or "Unknown"
                        next_frontier.append(k)
            frontier = next_frontier

    return all_nodes


def path_plan_to_steps(path_plan: Optional[list[str]]) -> list[dict[str, str]]:
    """Structured relation steps for observability UIs (parsed ``path_plan`` strings)."""
    if not path_plan:
        return []
    steps = _parse_path_plan(path_plan)
    return [
        {"from_label": a, "relation": b, "to_label": c}
        for a, b, c in steps
    ]


def describe_typed_pattern(path_plan: Optional[list[str]]) -> str:
    """Human-readable MATCH pattern sketch from abstract ``path_plan``."""
    steps = _parse_path_plan(path_plan or [])
    if not steps:
        return ""
    parts: list[str] = []
    for i, (src, rel, dst) in enumerate(steps):
        if i == 0:
            parts.append(f"({src})-[:{rel}]->({dst})")
        else:
            parts.append(f"-[:{rel}]->({dst})")
    return "".join(parts)


def subgraph_from_card(card: dict) -> TraversalSubgraph:
    """Build TraversalSubgraph directly from Strategy.path_subgraph_keys."""
    keys = list(card.get("path_subgraph_keys") or [])
    return TraversalSubgraph(
        node_keys=keys,
        nodes_by_label={},  # labels not stored per card; caller uses keys directly
        method="card_cache",
    )


__all__ = [
    "TraversalSubgraph",
    "DEFAULT_REL_WEIGHTS",
    "instantiate_path_plan",
    "weighted_bfs_fallback",
    "subgraph_from_card",
    "path_plan_to_steps",
    "describe_typed_pattern",
    "relation_priority_score",
    "cypher_relation_priority_case",
]

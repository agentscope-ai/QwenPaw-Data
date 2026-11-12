"""Graph Center query algorithms copied from develop c4e072a7 api/retrieval.py.

Database access is bound to the current Store and Scope.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from ...schema import GRAPH_SCHEMA, Zone
from ...store import NeighborDirection
from .access import ExplorerAccess
from .display import HIDDEN_FIELDS, json_value
from .ds_closure import closure_key_sets
from .queries import ExplorerQueries

_PROP_MAX_LEN = 400
_METADATA_LAYER_LABELS = tuple(
    s.name for s in GRAPH_SCHEMA.nodes if s.zone is Zone.METADATA and not s.internal
)
_KNOWLEDGE_LAYER_LABELS = ("Entity", "Event")
_NON_METADATA_ZONES = ("knowledge", "user")


def _trim_props(props: dict) -> dict:
    """节点属性传给前端时把超长 ddl/formula_evidence 截一下，避免 payload 过大。

    mgw4-viewsql（§2.1-C 泄漏点 3）：画布 payload 出口整体裁剪 ``sql`` ——
    治理面只给视图名+描述，看 SQL 走 dataset 明细受控下钻（view_sql）。
    """
    out: dict = {}
    for k, v in props.items():
        if k in HIDDEN_FIELDS:
            continue
        if isinstance(v, str) and len(v) > _PROP_MAX_LEN:
            out[k] = v[:_PROP_MAX_LEN] + "…"
        elif hasattr(v, "iso_format"):  # neo4j.time.DateTime / Date
            out[k] = v.iso_format()
        else:
            out[k] = json_value(v)
    return out


def _node_label(props: dict, group: str) -> str:
    """节点在图里显示的短文本。优先 name → key 末段。"""
    if group in (
        "Metric",
        "Dimension",
        "Domain",
        "Entity",
        "Event",
        "Dataset",
    ):
        return str(props.get("name") or props.get("key") or "")
    if group == "MetricCaliber":
        ds = str(props.get("dataset") or "")
        return ds.rsplit(".", 1)[-1] if ds else str(props.get("key", ""))
    if group == "Table":
        return str(props.get("name") or props.get("key") or "")
    if group == "DatasetField":
        return str(props.get("name") or "?")
    if group == "Column":
        return f"{props.get('table') or '?'}.{props.get('name') or '?'}"
    if group == "DataSource":
        return str(
            props.get("datasource_name")
            or props.get("name")
            or props.get("datasource_id")
            or props.get("key")
            or group
        )
    return str(props.get("key") or props.get("name") or group)


def _all_layer_labels_union() -> tuple[str, ...]:
    """``zone_mode=all`` 时合并元数据与知识图层的结点标签（去重保序）。"""
    merged: dict[str, None] = {}
    for lab in _METADATA_LAYER_LABELS + _KNOWLEDGE_LAYER_LABELS:
        merged.setdefault(lab, None)
    return tuple(merged.keys())


def _explorer_collect_haystacks(
    node_id: str,
    label: str,
    group: str,
    props: dict[str, Any],
) -> list[str]:
    """与 ``app.js`` 找节点打分一致：可检索字段并集。"""
    parts: list[str] = []
    if node_id:
        parts.append(str(node_id))
    if label:
        parts.append(str(label))
    p = props if isinstance(props, dict) else {}
    for k in (
        "key",
        "name",
        "title",
        "table",
        "schema",
        "column_key",
        "domain",
        "zone",
        "chunk_id",
    ):
        v = p.get(k)
        if v is not None and str(v).strip():
            parts.append(str(v))
    return parts


def _tokenize_query(q_lower: str) -> list[str]:
    """与前端 graphDb.tokenizeQuery 同规则：ASCII 词 + CJK 二元组（跳过纯虚词
    二元组）。自然语言长句（「留存有什么相关指标」）整句子串必然 0 命中，
    靠 token 兜底召回。"""
    tokens: list[str] = list(re.findall(r"[a-z0-9_]{2,}", q_lower))
    particles = set(
        "的了吗呢吧啊哦嘛么是有都在和与跟就还也很不没这那些个为对以到说要看会能什"
    )
    cjk = re.sub(r"[a-z0-9_\s]", "", q_lower)
    for i in range(len(cjk) - 1):
        bg = cjk[i : i + 2]
        if all(ch in particles for ch in bg):
            continue
        tokens.append(bg)
    return list(dict.fromkeys(tokens))


def _explorer_match_score(
    q_lower: str,
    node_id: str,
    label: str,
    group: str,
    props: dict[str, Any],
) -> int:
    best = 0
    hays: list[str] = []
    seen: set[str] = set()
    for raw in _explorer_collect_haystacks(node_id, label, group, props):
        h = str(raw).lower()
        if not h or h in seen:
            continue
        seen.add(h)
        hays.append(h)
        if h == q_lower:
            best = max(best, 100)
        elif h.startswith(q_lower):
            best = max(best, 85)
        elif q_lower in h:
            best = max(best, 55)
    if best == 0:
        # 整句无子串命中 → token 级召回（弱于子串分，避免压过精确搜索）
        tokens = _tokenize_query(q_lower)
        if tokens:
            matched = sum(1 for t in tokens if any(t in h for h in hays))
            if matched:
                best = min(52, 12 + 10 * matched)
    return best


def search_explorer_nodes(access: ExplorerAccess, query, *, limit=25, fetch_cap=500):
    """Preserve substring/token matching and display ranking within the issued Scope."""
    query = (query or "").strip().lower()
    if not query:
        return []
    limit = max(1, min(int(limit), 100))
    cap = max(limit, min(int(fetch_cap), 2000))
    tokens = [query, *_tokenize_query(query)[:6]]
    rows = []
    for node in access.nodes():
        props = node.properties
        values = [
            props.get(field)
            for field in (
                "key",
                "name",
                "title",
                "domain",
                "table",
                "schema",
                "column_key",
                "chunk_id",
            )
        ]
        if not any(
            token in str(value).lower()
            for token in tokens
            for value in values
            if value is not None
        ):
            continue
        canvas = _canvas(node)
        score = _explorer_match_score(
            query, node.key, canvas["label"], node.spec.name, canvas["props"]
        )
        if score > 0:
            rows.append({**canvas, "score": score})
        if len(rows) >= cap:
            break
    best = {}
    for row in rows:
        if row["id"] not in best or row["score"] > best[row["id"]]["score"]:
            best[row["id"]] = row
    return sorted(best.values(), key=lambda row: (-row["score"], row["id"]))[:limit]


def search_explorer_subgraph(
    access: ExplorerAccess,
    query,
    *,
    allowed_labels=(),
    exact=False,
    hops=1,
    limit=50,
):
    """Find visible hits and traverse only authorized nodes and edges."""
    query = (query or "").strip()
    hits = []
    for node in access.nodes(allowed_labels or None):
        props = node.properties
        matches = (
            query in (node.key, props.get("name"))
            if exact
            else any(
                query.lower() in str(value or "").lower()
                for value in (
                    node.key,
                    props.get("name"),
                    props.get("canonical_name"),
                    props.get("description"),
                    *(props.get("aliases") or ()),
                )
            )
        )
        if matches:
            hits.append(node)
            if len(hits) >= max(1, min(int(limit), 200)):
                break
    nodes = {node.key: _search_node(node) for node in hits}
    edges = {}
    for hit in hits:
        frontier = [hit]
        visited = {hit.key}
        accepted = 0
        for _ in range(max(1, min(int(hops), 3))):
            following = []
            for source in frontier:
                for node, edge in access.neighbors(source):
                    if node.key == hit.key:
                        continue
                    signature = (
                        edge.source_key,
                        edge.target_key,
                        edge.spec.relationship,
                    )
                    edges[signature] = {
                        "source_key": signature[0],
                        "target_key": signature[1],
                        "rel_type": signature[2],
                        "properties": {},
                    }
                    nodes.setdefault(node.key, _search_node(node))
                    if node.key not in visited:
                        visited.add(node.key)
                        following.append(node)
                    accepted += 1
                    if accepted >= 200:
                        break
                if accepted >= 200:
                    break
            frontier = following
            if not frontier or accepted >= 200:
                break
    return {
        "hit_nodes": [_search_node(node) for node in hits],
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
    }


def global_graph_snapshot(
    access: ExplorerAccess,
    *,
    max_edges=800,
    max_nodes=600,
    domain_roots_only=True,
    zone_mode="all",
    datasource_id="",
):
    """Build a bounded canvas from canonical records, retaining root/endpoint rules."""
    mode = (zone_mode or "all").strip().lower() or "all"
    if mode not in {"all", "metadata", "knowledge"}:
        raise ValueError(
            f"zone_mode must be one of: all, metadata, knowledge (got {zone_mode!r})"
        )
    nodes, edges, counts = {}, [], {}
    layered = mode in {"knowledge", "metadata"} or not domain_roots_only
    if layered:
        labels = (
            _KNOWLEDGE_LAYER_LABELS
            if mode == "knowledge"
            else _METADATA_LAYER_LABELS
            if mode == "metadata"
            else _all_layer_labels_union()
        )
        queries = ExplorerQueries(access)
        closure = closure_key_sets(queries, ds=datasource_id)
        for row in queries.nodes(labels, datasource_id, closure, max_nodes):
            nodes[row["node_key"]] = _canvas_properties(row["label"], row["props"])
            counts[row["label"]] = counts.get(row["label"], 0) + 1
        if max_edges:
            for row in queries.edges(labels, datasource_id, closure, max_edges):
                source = _canvas_properties(row["source_label"], row["source_props"])
                target = _canvas_properties(row["target_label"], row["target_props"])
                # Edge endpoints remain visible beyond the initial node budget.
                nodes.setdefault(source["id"], source)
                nodes.setdefault(target["id"], target)
                edges.append(
                    {
                        "from": source["id"],
                        "to": target["id"],
                        "type": row["relationship"],
                        "props": _trim_props(row["props"]),
                    }
                )
        return {
            "center": None,
            "nodes": list(nodes.values()),
            "edges": edges,
            "raw": {
                "zone_mode": mode,
                "mode": "neo4j_labels_" + mode,
                "graph": "",
                **({"datasource_id": datasource_id} if mode == "metadata" else {}),
                "neo4j_labels": list(labels),
                "stats": {"node_count": len(nodes), "edge_count": len(edges), **counts},
            },
        }
    queries = ExplorerQueries(access)
    for row in queries.roots("Domain", max_nodes):
        canvas = _canvas_properties("Domain", row["props"])
        canvas["props"]["metric_count"] = row["count"]
        nodes[canvas["id"]] = canvas
    counts["domain"] = len(nodes)
    if not nodes:
        for row in queries.roots("DataSource", min(200, max_nodes)):
            canvas = _canvas_properties("DataSource", row["props"])
            canvas["props"]["table_count"] = row["count"]
            nodes[canvas["id"]] = canvas
        counts["datasource"] = len(nodes)
    return {
        "center": None,
        "nodes": list(nodes.values()),
        "edges": edges,
        "raw": {
            "zone_mode": mode,
            "mode": "domain_roots_only",
            "stats": {"node_count": len(nodes), "edge_count": 0, **counts},
        },
    }


def domain_graph_snapshot(access: ExplorerAccess, domain, *, max_nodes=200):
    """Return a named Domain and its active Metric skeleton."""
    if not (domain or "").strip():
        raise ValueError("domain is required")
    root = next(iter(access.nodes(("Domain",), filters={"name": domain})), None)
    if root is None:
        raise ValueError(f"Domain {domain!r} not found")
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    metrics = []
    for node, edge in access.neighbors(root, direction=NeighborDirection.OUTGOING):
        end = node.properties.get("valid_to")
        end = end.isoformat() if hasattr(end, "isoformat") else end
        if (
            edge.spec.relationship == "HAS"
            and node.spec.name == "Metric"
            and (end is None or end > now)
        ):
            metrics.append(node)
    metrics.sort(key=lambda node: node.properties.get("name") or "")
    metrics = metrics[:max_nodes]
    shown = metrics[: max(0, max_nodes - 1)]
    nodes = [_canvas(root), *(_canvas(node) for node in shown)]
    edges = [
        {"from": root.key, "to": node.key, "type": "HAS", "props": {}} for node in shown
    ]
    return {
        "center": root.key,
        "nodes": nodes,
        "edges": edges,
        "raw": {
            "domain": domain,
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                "metric_count": len(metrics),
            },
        },
    }


def expand_node_snapshot(
    access: ExplorerAccess, node_key, *, max_edges=80, metadata_only=False
):
    return _expand_snapshot(
        access, node_key, NeighborDirection.BOTH, max_edges, metadata_only
    )


def expand_node_layer(
    access: ExplorerAccess,
    node_key,
    *,
    direction="down",
    max_edges=80,
    metadata_only=False,
):
    direction = (direction or "down").lower()
    if direction not in ("down", "up"):
        raise ValueError(f"direction must be 'down' or 'up', got {direction!r}")
    result = _expand_snapshot(
        access,
        node_key,
        NeighborDirection.OUTGOING
        if direction == "down"
        else NeighborDirection.INCOMING,
        max_edges,
        metadata_only,
    )
    # The previous OPTIONAL MATCH retains its center row at a leaf; it does
    # not switch to the opposite direction when no directional edge exists.
    result["raw"]["direction"] = direction
    return result


def _canvas(node):
    return {
        "id": node.key,
        "label": _node_label(node.properties, node.spec.name),
        "group": node.spec.name,
        "props": _trim_props(node.properties),
    }


def _canvas_properties(label, properties):
    return {
        "id": properties["key"],
        "label": _node_label(properties, label),
        "group": label,
        "props": _trim_props(properties),
    }


def _canvas_edge(edge):
    return {
        "from": edge.source_key,
        "to": edge.target_key,
        "type": edge.spec.relationship,
        "props": _trim_props(edge.properties),
    }


def _search_node(node):
    props = node.properties
    return {
        "key": node.key,
        "label": node.spec.name,
        "zone": props.get("zone") or "",
        "display_name": props.get("name")
        or props.get("canonical_name")
        or props.get("goal")
        or node.key,
    }


def _expand_snapshot(access, key, direction, limit, metadata_only):
    if not (key or "").strip():
        raise ValueError("node_key is required")
    center = access.node(key)
    if center is None:
        raise ValueError(f"Node {key!r} not found")
    nodes, edges = {key: _canvas(center)}, {}
    for node, edge in access.neighbors(center, direction=direction):
        if metadata_only and node.spec.zone.value in _NON_METADATA_ZONES:
            continue
        if len(edges) >= limit:
            break
        nodes.setdefault(node.key, _canvas(node))
        signature = (edge.source_key, edge.target_key, edge.spec.relationship)
        edges.setdefault(signature, _canvas_edge(edge))
    return {
        "center": key,
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "raw": {"stats": {"node_count": len(nodes), "edge_count": len(edges)}},
    }

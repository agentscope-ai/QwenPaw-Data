"""Cypher proxy with Access Control List(ACL).

Prefix: ``/api/v1``

Allows full CRUD on writable labels (KG: Entity/Event, TG: Claim/Strategy)
while protecting all other node types as read-only.  DDL (DROP/LOAD) and
stored-procedure calls (CALL) are always blocked.
"""
from __future__ import annotations

import os
import re
import time
from itertools import islice
from typing import Any, Literal

from fastapi import APIRouter, Request
from neo4j import Query
from pydantic import BaseModel, Field

from qwenpaw_data.context.resource_budget import current_request_budget

from ..utils import get_logger, graph_session
from .response_envelope import fail, success

log = get_logger("api.cypher")

router = APIRouter(prefix="/api/v1/admin", tags=["cypher"])

# ---------------------------------------------------------------------------
# ACL configuration
# ---------------------------------------------------------------------------

_BLOCKED_KEYWORDS = frozenset({"drop", "load", "call"})

_DML_KEYWORDS = frozenset({"create", "merge", "delete", "detach", "set", "remove"})

_WRITABLE_LABELS = frozenset({"Entity", "Event", "Claim", "Strategy", "StrategyCard"})

_ALL_KNOWN_LABELS = frozenset({
    # KG
    "Entity", "Event",
    # TG
    "Task", "Step", "ToolCall", "Claim", "Turn", "Experience",
    "Strategy", "StrategyCard", "Session", "Tag",
    # MG / Physical
    "Domain", "Metric", "Formula", "Dimension", "DimensionValue",
    "Operator", "Dataset", "DatasetColumn", "Caliber",
    "DataSource", "Database", "Schema", "Table", "Column",
})

_PROTECTED_LABELS = _ALL_KNOWN_LABELS - _WRITABLE_LABELS

# ---------------------------------------------------------------------------
# Cypher static-analysis helpers
# ---------------------------------------------------------------------------

_STRING_LITERAL_RE = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\"""")

_NODE_PATTERN_RE = re.compile(
    r"\(\s*([A-Za-z_]\w*)\s*(?::\s*`?([A-Za-z_]\w*)`?(?:\s*:\s*`?([A-Za-z_]\w*)`?)*)",
)

_ANON_LABEL_RE = re.compile(
    r"\(\s*:\s*`?([A-Za-z_]\w*)`?(?:\s*:\s*`?([A-Za-z_]\w*)`?)*",
)

_REL_VAR_RE = re.compile(r"\[\s*([A-Za-z_]\w*)\s*(?:[:}\]\s])")

_CLAUSE_BOUNDARY_RE = re.compile(
    r"\b(?:MATCH|OPTIONAL\s+MATCH|WHERE|WITH|RETURN|ORDER|SKIP|LIMIT|UNION"
    r"|UNWIND|FOREACH|CREATE|MERGE|SET|DELETE|DETACH|REMOVE|CALL)\b",
    re.IGNORECASE,
)

_SET_REMOVE_BODY_RE = re.compile(
    r"\b(?:SET|REMOVE)\b\s+(.*?)(?=\b(?:SET|REMOVE|RETURN|WITH|WHERE|ORDER"
    r"|LIMIT|UNWIND|CALL|CREATE|MERGE|DELETE|DETACH|MATCH|FOREACH)\b|$)",
    re.IGNORECASE | re.DOTALL,
)

_DELETE_BODY_RE = re.compile(
    r"\b(?:DETACH\s+)?DELETE\b\s+([\w\s,]+?)(?=\b(?:SET|REMOVE|RETURN|WITH"
    r"|WHERE|ORDER|LIMIT|UNWIND|CALL|CREATE|MERGE|MATCH|FOREACH|DELETE)\b|$)",
    re.IGNORECASE,
)

_VAR_DOT_PROP_RE = re.compile(r"([A-Za-z_]\w*)\.(\w+)")
_VAR_LABEL_ASSIGN_RE = re.compile(r"([A-Za-z_]\w*)\s*:\s*`?([A-Za-z_]\w*)`?")
_VAR_PLUS_EQ_RE = re.compile(r"([A-Za-z_]\w*)\s*\+\s*=")


def _strip_string_literals(cypher: str) -> str:
    """Remove single/double-quoted string values. Backticks are kept
    because they are identifier escapes, not string values."""
    return _STRING_LITERAL_RE.sub("''", cypher)


def _has_keyword(lowered: str, keywords: frozenset[str]) -> bool:
    for kw in keywords:
        if kw + " " in lowered or kw + "(" in lowered or lowered.endswith(kw):
            return True
    if "detach" in lowered and "detach" in keywords:
        return True
    return False


def _build_var_label_map(cleaned: str) -> dict[str, set[str]]:
    """Scan all ``(var:Label)`` patterns and return ``{var: {labels}}``."""
    var_map: dict[str, set[str]] = {}
    for m in _NODE_PATTERN_RE.finditer(cleaned):
        var_name = m.group(1)
        labels = {g for g in m.groups()[1:] if g}
        var_map.setdefault(var_name, set()).update(labels)
    return var_map


def _find_relationship_vars(cleaned: str) -> set[str]:
    """Find variable names bound in relationship patterns ``-[r:TYPE]->``."""
    return {m.group(1) for m in _REL_VAR_RE.finditer(cleaned)}


def _extract_labels_from_pattern(pattern_text: str) -> set[str]:
    """Extract all node labels from a Cypher pattern fragment
    (handles both named and anonymous nodes, with optional backticks)."""
    labels: set[str] = set()
    for m in _NODE_PATTERN_RE.finditer(pattern_text):
        labels.update(g for g in m.groups()[1:] if g)
    for m in _ANON_LABEL_RE.finditer(pattern_text):
        labels.update(g for g in m.groups() if g)
    return labels


def _find_clause_body(cleaned: str, keyword: str) -> list[str]:
    """Find all clause bodies for a given keyword (e.g. CREATE, MERGE).

    Returns the text between the keyword and the next clause boundary."""
    bodies: list[str] = []
    lowered = cleaned.lower()
    start = 0
    kw_len = len(keyword)
    while True:
        pos = lowered.find(keyword, start)
        if pos == -1:
            break
        # Ensure word boundary
        if pos > 0 and lowered[pos - 1].isalnum():
            start = pos + kw_len
            continue
        end_pos = pos + kw_len
        if end_pos < len(lowered) and lowered[end_pos].isalnum():
            start = pos + kw_len
            continue
        body_start = pos + kw_len
        # Find next clause boundary
        next_boundary = _CLAUSE_BOUNDARY_RE.search(cleaned, body_start + 1)
        body_end = next_boundary.start() if next_boundary else len(cleaned)
        bodies.append(cleaned[body_start:body_end])
        start = body_end
    return bodies


def _extract_write_targets(
    cleaned: str, var_map: dict[str, set[str]]
) -> tuple[set[str], set[str]]:
    """Identify labels targeted by write operations.

    Returns ``(targeted_labels, unresolved_vars)``.
    ``unresolved_vars`` contains variable names used in write context
    but with no ``(var:Label)`` binding — these will be rejected unless
    they are relationship variables.
    """
    targeted: set[str] = set()
    unresolved: set[str] = set()

    # --- CREATE / MERGE: labels in inline node patterns ---
    for kw in ("create", "merge"):
        for body in _find_clause_body(cleaned, kw):
            targeted |= _extract_labels_from_pattern(body)

    # --- DELETE / DETACH DELETE: variables being deleted ---
    for m in _DELETE_BODY_RE.finditer(cleaned):
        var_list_str = m.group(1)
        for token in re.split(r"[,\s]+", var_list_str.strip()):
            token = token.strip()
            if not token or not token[0].isalpha():
                continue
            if token.lower() in _DML_KEYWORDS | _BLOCKED_KEYWORDS | {
                "return", "with", "match", "where", "detach",
            }:
                break
            if token in var_map:
                targeted |= var_map[token]
            else:
                unresolved.add(token)

    # --- SET / REMOVE ---
    for m in _SET_REMOVE_BODY_RE.finditer(cleaned):
        body = m.group(1)
        # var.prop = ... or var.prop += ...
        for pm in _VAR_DOT_PROP_RE.finditer(body):
            vn = pm.group(1)
            if vn.lower() in ("on",):
                continue
            if vn in var_map:
                targeted |= var_map[vn]
            else:
                unresolved.add(vn)
        # var += {...}
        for pm in _VAR_PLUS_EQ_RE.finditer(body):
            vn = pm.group(1)
            if vn in var_map:
                targeted |= var_map[vn]
            else:
                unresolved.add(vn)
        # SET var:Label / REMOVE var:Label
        for pm in _VAR_LABEL_ASSIGN_RE.finditer(body):
            vn = pm.group(1)
            label = pm.group(2)
            if vn.lower() in ("on",):
                continue
            targeted.add(label)
            if vn in var_map:
                targeted |= var_map[vn]
            else:
                unresolved.add(vn)

    return targeted, unresolved


def _check_write_acl(cypher: str) -> None:
    """Validate Cypher write ACL.  Raises via ``fail()`` on violation."""
    cleaned = _strip_string_literals(cypher)
    lowered = cleaned.lower()

    # 1. Multi-statement guard
    if ";" in cleaned:
        fail("MULTI_STATEMENT", "不允许多条语句（分号分隔）", status_code=403)

    # 2. Blocked keywords (DDL + CALL)
    if _has_keyword(lowered, _BLOCKED_KEYWORDS):
        fail("DDL_BLOCKED", "DDL / CALL 操作不允许（DROP / LOAD / CALL）", status_code=403)

    # 3. Check for DML keywords — if none, it's a pure read query
    if not _has_keyword(lowered, _DML_KEYWORDS):
        return

    # 4. Build variable → label map
    var_map = _build_var_label_map(cleaned)

    # 5. Extract write targets
    targeted, unresolved = _extract_write_targets(cleaned, var_map)

    # 6. Relationship variables are allowed in DELETE — remove from unresolved
    rel_vars = _find_relationship_vars(cleaned)
    unresolved -= rel_vars

    # 7. Unresolved variables → reject (can't verify safety)
    if unresolved:
        fail(
            "WRITE_NO_LABEL",
            f"写操作目标变量无法确定类型，已拒绝执行：{', '.join(sorted(unresolved))}。"
            "请在 MATCH 中为变量绑定标签，如 (n:Entity)",
            status_code=403,
            detail={"unresolved_vars": sorted(unresolved)},
        )

    # 8. No targets at all — DML keyword may be a false positive (e.g. property
    #    value that survived stripping), or a relationship-only operation. Allow.
    if not targeted:
        return

    # 9. Protected labels → reject
    protected_hit = targeted & _PROTECTED_LABELS
    if protected_hit:
        fail(
            "WRITE_PROTECTED",
            f"以下节点类型为只读，不允许写操作：{', '.join(sorted(protected_hit))}。"
            f"可写类型：{', '.join(sorted(_WRITABLE_LABELS))}",
            status_code=403,
            detail={"protected_labels": sorted(protected_hit)},
        )

    # 10. Unknown labels → reject (deny-by-default)
    unknown = targeted - _ALL_KNOWN_LABELS
    if unknown:
        fail(
            "WRITE_PROTECTED",
            f"未知的节点类型，不允许写操作：{', '.join(sorted(unknown))}",
            status_code=403,
            detail={"unknown_labels": sorted(unknown)},
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class CypherRequest(BaseModel):
    cypher: str
    params: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(200, ge=1, le=1000)
    response_format: Literal["auto", "graph", "table"] = "auto"
    datasource_id: str = ""

    @property
    def resolved_datasource_id(self) -> str:
        return (self.datasource_id or "").strip()


# ---------------------------------------------------------------------------
# Graph extraction
# ---------------------------------------------------------------------------


def _extract_graph(rows: list[dict], columns: list[str]) -> dict[str, Any] | None:
    """Extract graph nodes/edges from Cypher result rows.

    Walks every cell looking for Neo4j Node, Relationship, and Path objects.
    Returns ``None`` if no graph elements are found.
    """
    from neo4j.graph import Node, Relationship, Path

    nodes_map: dict[str, dict] = {}
    edges_list: list[dict] = []
    edges_seen: set[tuple] = set()

    def _node_to_dict(n: Node) -> dict:
        labels = list(n.labels)
        props = {k: v for k, v in dict(n).items() if k not in ("embedding", "embedding_hash", "signature_emb")}
        key = props.get("key", str(n.element_id))
        zone = props.get("zone", "")
        display = props.get("name", props.get("canonical_name", props.get("goal", key)))
        label = labels[0] if labels else "Unknown"
        return {"key": key, "label": label, "zone": zone, "display_name": str(display), "properties": props}

    def _rel_to_dict(r: Relationship) -> dict:
        start_props = dict(r.start_node) if r.start_node else {}
        end_props = dict(r.end_node) if r.end_node else {}
        source_key = start_props.get("key", str(r.start_node.element_id) if r.start_node else "")
        target_key = end_props.get("key", str(r.end_node.element_id) if r.end_node else "")
        rel_props = {k: v for k, v in dict(r).items() if k not in ("embedding", "embedding_hash")}
        return {
            "source_key": source_key,
            "target_key": target_key,
            "rel_type": r.type,
            "properties": rel_props,
        }

    def _process_value(v: Any) -> None:
        if isinstance(v, Node):
            nd = _node_to_dict(v)
            nodes_map[nd["key"]] = nd
        elif isinstance(v, Relationship):
            if v.start_node:
                sn = _node_to_dict(v.start_node)
                nodes_map[sn["key"]] = sn
            if v.end_node:
                en = _node_to_dict(v.end_node)
                nodes_map[en["key"]] = en
            rd = _rel_to_dict(v)
            edge_id = (rd["source_key"], rd["target_key"], rd["rel_type"])
            if edge_id not in edges_seen:
                edges_seen.add(edge_id)
                edges_list.append(rd)
        elif isinstance(v, Path):
            for node in v.nodes:
                _process_value(node)
            for rel in v.relationships:
                _process_value(rel)
        elif isinstance(v, (list, tuple)):
            for item in v:
                _process_value(item)

    for row in rows:
        for col in columns:
            _process_value(row.get(col))

    if not nodes_map and not edges_list:
        return None
    return {"nodes": list(nodes_map.values()), "edges": edges_list}


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/cypher")
def execute_cypher(body: CypherRequest, request: Request):
    """Execute a Cypher query with write ACL."""
    _check_write_acl(body.cypher)

    driver = request.app.state.driver
    t0 = time.monotonic()

    try:
        query_timeout = max(
            0.1,
            float(os.getenv("QWENPAW_DATA_CYPHER_TIMEOUT_SECONDS", "30")),
        )
    except ValueError:
        query_timeout = 30.0

    with graph_session(driver) as s:
        result = s.run(Query(body.cypher, timeout=query_timeout), **body.params)
        keys = list(result.keys())
        row_limit = current_request_budget().cap_cypher_rows(body.limit)
        fetch = getattr(result, "fetch", None)
        if callable(fetch):
            records_with_lookahead = list(fetch(row_limit + 1))
        else:
            records_with_lookahead = list(islice(result, row_limit + 1))
        truncated = len(records_with_lookahead) > row_limit
        records = records_with_lookahead[:row_limit]
        raw_rows_native = [dict(zip(keys, r.values())) for r in records]
        raw_rows_serial = [dict(zip(keys, (r.data()[k] for k in keys))) for r in records]

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    rows_native = raw_rows_native
    columns = keys if keys else (list(rows_native[0].keys()) if rows_native else [])

    graph = None
    if body.response_format != "table":
        graph = _extract_graph(raw_rows_native, columns)
        if body.response_format == "auto" and graph is None:
            pass

    ds_id = body.resolved_datasource_id
    if graph and ds_id:
        from .datasource_filter import filter_graph_by_datasource
        graph = filter_graph_by_datasource(graph, ds_id)

    from .kg_admin import _to_jsonable
    serialized_rows = _to_jsonable(raw_rows_serial)

    labels_hit = list({n["label"] for n in graph["nodes"]} if graph else [])
    summary = {
        "result_type": "graph" if graph else "table",
        "node_count": len(graph["nodes"]) if graph else 0,
        "edge_count": len(graph["edges"]) if graph else 0,
        "labels_hit": labels_hit,
        "elapsed_ms": elapsed_ms,
        "truncated": truncated,
    }

    data = {
        "rows": serialized_rows,
        "count": len(raw_rows_native),
        "truncated": truncated,
        "columns": columns,
        "graph": graph,
        "summary": summary,
    }
    current_request_budget().ensure_response_payload(data)
    return success(data)

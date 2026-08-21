"""Knowledge graph manual maintenance helpers (Neo4j :Entity / :Event).

Used by ``/knowledge-admin`` HTTP API. Writes are intentionally narrow:
Entity / Event 维护、RELATED_TO / ABOUT 等；全库按类型删边的类型列表由当前库扫描得到。
"""
from __future__ import annotations

import re
from typing import Any, Literal, Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from ..graph.knowledge import _write_entity_links
from ..graph.knowledge_writer import KnowledgeWriter

log = get_logger("api.kg_admin")

_STRIP_KEYS = frozenset({"embedding", "signature_emb", "query_emb", "query_embedding", "strategy_vec"})

_LIFECYCLE = frozenset(
    {
        "active",
        "archived",
        "frozen",
        "invalidated",
        "superseded",
        "needs_revalidation",
    }
)

_REL_SUBTYPES = frozenset(
    {"synonym", "antonym", "competitor", "complement", "correlates", "see_also", "cross_domain"}
)


def _to_jsonable(obj: Any) -> Any:
    """Neo4j 驱动会返回 DateTime / Duration 等，FastAPI JSON 无法直接序列化。"""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(x) for x in obj]
    iso = getattr(obj, "isoformat", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    iso2 = getattr(obj, "iso_format", None)
    if callable(iso2):
        try:
            return iso2()
        except Exception:
            pass
    mod = getattr(type(obj), "__module__", "") or ""
    if mod.startswith("neo4j"):
        return str(obj)
    return str(obj)


def _json_safe_props(props: dict[str, Any]) -> dict[str, Any]:
    raw = {k: v for k, v in (props or {}).items() if k not in _STRIP_KEYS}
    out = _to_jsonable(raw)
    return out if isinstance(out, dict) else {}


def list_knowledge_nodes(
    driver: Driver,
    *,
    q: str = "",
    kind: Literal["both", "entity", "event"] = "both",
    limit: int = 80,
) -> list[dict[str, Any]]:
    ql = (q or "").strip().lower()
    lim = max(1, min(int(limit), 500))
    kind_s = kind if kind in ("both", "entity", "event") else "both"
    cypher = """
    MATCH (n)
    WHERE (n:Entity OR n:Event)
      AND ($kind = 'both'
           OR ($kind = 'entity' AND n:Entity)
           OR ($kind = 'event' AND n:Event))
      AND ($q = '' OR
           toLower(coalesce(n.key, '')) CONTAINS $q OR
           toLower(coalesce(n.name, '')) CONTAINS $q OR
           toLower(coalesce(n.canonical_name, '')) CONTAINS $q OR
           toLower(coalesce(n.description, '')) CONTAINS $q)
    WITH n, coalesce(n.key, '') AS k
    ORDER BY k
    LIMIT $lim
    RETURN CASE WHEN n:Entity THEN 'Entity' ELSE 'Event' END AS label,
           n.key AS key,
           coalesce(n.name, n.canonical_name, '') AS display_name,
           coalesce(n.type, '') AS type,
           coalesce(n.lifecycle_state, '') AS lifecycle_state,
           coalesce(n.zone, '') AS zone
    """
    with neo4j_session(driver) as s:
        return s.run(cypher, q=ql, kind=kind_s, lim=lim).data()


def get_knowledge_node(driver: Driver, key: str) -> Optional[dict[str, Any]]:
    k = (key or "").strip()
    if not k:
        return None
    cypher = """
    MATCH (n {key: $k})
    WHERE n:Entity OR n:Event
    RETURN CASE WHEN n:Entity THEN 'Entity' ELSE 'Event' END AS label,
           n.key AS key,
           properties(n) AS props
    LIMIT 1
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, k=k).single()
    if not rec:
        return None
    props = _json_safe_props(dict(rec["props"] or {}))
    props.pop("embedding", None)
    return {"label": rec["label"], "key": rec["key"], "properties": props}


def list_neighbors(driver: Driver, key: str, *, limit: int = 120) -> list[dict[str, Any]]:
    k = (key or "").strip()
    if not k:
        return []
    lim = max(1, min(int(limit), 400))
    cypher = """
    MATCH (n {key: $k})
    WHERE n:Entity OR n:Event
    OPTIONAL MATCH (n)-[r]-(m)
    WHERE m IS NULL OR m <> n
    WITH n, r, m
    LIMIT $lim
    RETURN type(r) AS rel_type,
           CASE
             WHEN r IS NULL THEN null
             WHEN startNode(r) = n THEN 'out'
             ELSE 'in'
           END AS direction,
           n.key AS anchor_key,
           coalesce(m.key, '') AS other_key,
           [lb IN labels(m) WHERE lb IN ['Entity','Event','Metric','Dimension','Domain','Task','Step']][0] AS other_label,
           coalesce(m.name, m.canonical_name, '') AS other_name,
           properties(r) AS rel_props
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, k=k, lim=lim).data()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("rel_type") is None:
            continue
        rp_raw = dict(row.get("rel_props") or {})
        for sk in _STRIP_KEYS:
            rp_raw.pop(sk, None)
        rp = _to_jsonable(rp_raw)
        if not isinstance(rp, dict):
            rp = {}
        out.append(
            {
                "rel_type": row.get("rel_type"),
                "direction": row.get("direction"),
                "anchor_key": row.get("anchor_key"),
                "other_key": row.get("other_key"),
                "other_label": row.get("other_label") or "",
                "other_name": row.get("other_name") or "",
                "rel_props": rp,
            }
        )
    return out


def _merge_entity_tx(tx, *, row: dict[str, Any]) -> None:
    tx.run(
        """
        MERGE (e:Entity {key: $key})
        ON CREATE SET
          e.type = $type,
          e.name = $name,
          e.canonical_name = $name,
          e.aliases = $aliases,
          e.description = $desc,
          e.zone = '_shared',
          e.lifecycle_state = $ls
        ON MATCH SET
          e.type = $type,
          e.name = $name,
          e.canonical_name = $name,
          e.aliases = $aliases,
          e.description = $desc,
          e.zone = coalesce(e.zone, '_shared'),
          e.lifecycle_state = $ls
        """,
        key=row["key"],
        type=row["type"],
        name=row["name"],
        aliases=row["aliases"],
        desc=row["desc"],
        ls=row["ls"],
    )


def upsert_entity(
    driver: Driver,
    *,
    key: str,
    canonical_name: str,
    type_: str = "",
    aliases: Optional[list[str]] = None,
    description: str = "",
    lifecycle_state: str = "",
) -> dict[str, Any]:
    k = (key or "").strip()
    if not k:
        raise ValueError("key is required")
    if not k.startswith("ent:"):
        raise ValueError("Entity key 须以 ent: 开头")
    name = (canonical_name or "").strip() or k
    als = [str(a).strip() for a in (aliases or []) if str(a).strip()][:64]
    if not als:
        als = [name]
    ls = (lifecycle_state or "active").strip() or "active"
    if ls not in _LIFECYCLE:
        ls = "active"
    row = {
        "key": k,
        "type": (type_ or "concept")[:120],
        "name": name[:2000],
        "aliases": als,
        "desc": (description or "")[:8000],
        "ls": ls,
    }
    with neo4j_session(driver) as s:
        s.execute_write(_merge_entity_tx, row=row)
    log.info("kg_admin: upsert Entity %s", k)
    return {"ok": True, "key": k}


def upsert_event(
    driver: Driver,
    *,
    key: str,
    name: str,
    type_: str = "",
    description: str = "",
    date_from: str = "",
    date_to: str = "",
    scope: str = "_global",
    zone: str = "knowledge",
    source_id: str = "kg_admin:ui",
    source_trust: float = 0.95,
    extractor: str = "manual",
) -> dict[str, Any]:
    k = (key or "").strip()
    if not k:
        raise ValueError("key is required")
    if not k.startswith("ev:"):
        raise ValueError("Event key 须以 ev: 开头")
    nm = (name or "").strip()
    if not nm:
        raise ValueError("Event name 不能为空")
    df = (date_from or "").strip()
    dt = (date_to or df or "").strip()
    fact = {
        "key": k,
        "label": "Event",
        "graph_zone": zone or "knowledge",
        "properties": {
            "type": (type_ or "other")[:120],
            "scope": (scope or "_global")[:200],
            "name": nm[:2000],
            "description": (description or "")[:8000],
            "date_from": df[:64],
            "date_to": dt[:64],
        },
        "source_id": source_id or "kg_admin:ui",
        "source_trust": source_trust if source_trust is not None else 0.95,
        "extractor": extractor or "manual",
        "extractor_confidence": 1.0,
        "ingest_method": "user_correction",
    }
    kw = KnowledgeWriter(driver, embedder=None)
    dec = kw.write(fact)
    log.info("kg_admin: upsert Event %s → %s", k, dec.value)
    return {"ok": True, "key": k, "decision": dec.value}


def delete_knowledge_node(driver: Driver, key: str) -> dict[str, Any]:
    k = (key or "").strip()
    if not k:
        raise ValueError("key is required")
    cypher = """
    MATCH (n {key: $k})
    WHERE n:Entity OR n:Event
    DETACH DELETE n
    RETURN 1 AS ok
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, k=k).single()
    ok = bool(rec)
    if ok:
        log.info("kg_admin: deleted node %s", k)
    return {"ok": ok, "deleted": ok}


def delete_knowledge_nodes_batch(driver: Driver, keys: list[str]) -> dict[str, Any]:
    """按 key 批量 ``DETACH DELETE``，仅 ``Entity`` / ``Event``（与单条删除一致）。"""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in keys or []:
        k = str(raw or "").strip()
        if not k or k in seen:
            continue
        seen.add(k)
        cleaned.append(k)
    if len(cleaned) > 200:
        raise ValueError("单次最多删除 200 个结点")
    if not cleaned:
        raise ValueError("keys 不能为空")
    deleted = 0
    cypher = """
    MATCH (n {key: $k})
    WHERE n:Entity OR n:Event
    DETACH DELETE n
    RETURN 1 AS ok
    """
    with neo4j_session(driver) as s:
        for k in cleaned:
            rec = s.run(cypher, k=k).single()
            if rec:
                deleted += 1
    log.info("kg_admin: batch delete requested=%d deleted=%d", len(cleaned), deleted)
    return {"ok": True, "requested": len(cleaned), "deleted": deleted, "not_found": len(cleaned) - deleted}


# Neo4j 关系类型名：仅允许大写字母/数字/下划线（不拼进 Cypher 模式串，用 type(r)=$rt 参数比较，防注入）
_REL_TYPE_PARAM_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")


def list_global_edge_purge_types(driver: Driver) -> list[str]:
    """从当前库中枚举：至少一端为 Entity/Event 的有向边上出现的全部 ``type(r)``（去重、排序）。

    供「全库按类型删边」下拉框使用；与 :func:`delete_all_edges_touching_knowledge_nodes_by_type` 可删范围一致。
    """
    cypher = """
    MATCH (a)-[r]->(b)
    WHERE (a:Entity OR a:Event) OR (b:Entity OR b:Event)
    RETURN DISTINCT type(r) AS t
    ORDER BY t
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher).data()
    return [str(row["t"]).strip() for row in rows if str(row.get("t") or "").strip()]


def delete_all_edges_touching_knowledge_nodes_by_type(driver: Driver, *, rel_type: str) -> dict[str, Any]:
    """删除库中所有 ``type(r)=rel_type`` 且至少一端为 ``Entity`` 或 ``Event`` 的有向边。

    与「单锚点按类型删边」不同：不限制某一结点，一次清空该类型在知识结点上的全部附着边。
    ``rel_type`` 须与 Neo4j 中 ``type(r)`` 完全一致（含大小写）。使用 ``$rt`` 参数绑定，不做 Cypher 拼接。
    """
    rt = (rel_type or "").strip()
    if not rt or len(rt) > 200:
        raise ValueError("关系类型不能为空且长度须 ≤ 200")
    if re.search(r"[\s\x00]", rt):
        raise ValueError("关系类型不能含空白或空字符")
    cypher = """
    MATCH (a)-[r]->(b)
    WHERE type(r) = $rt
      AND ((a:Entity OR a:Event) OR (b:Entity OR b:Event))
    DELETE r
    """
    with neo4j_session(driver) as s:
        res = s.run(cypher, rt=rt)
        n_del = int(res.consume().counters.relationships_deleted)
    log.warning("kg_admin: global purge rel_type=%s deleted=%s", rt, n_del)
    return {"ok": True, "rel_type": rt, "deleted": n_del}


def delete_adjacent_edge(
    driver: Driver,
    *,
    anchor_key: str,
    other_key: str,
    rel_type: str,
    direction: str,
) -> dict[str, Any]:
    """删除锚点（须为 ``Entity`` / ``Event``）与对端之间、指定 ``type(r)`` 的关系。

    凡与知识库结点（Entity/Event）为端点的边均可删：关系类型用参数与 ``type(r)`` 比较，
    不再维护易漏的枚举白名单。
    """
    ak = (anchor_key or "").strip()
    bk = (other_key or "").strip()
    if not ak or not bk:
        raise ValueError("anchor_key / other_key 不能为空")
    rt = (rel_type or "").strip().upper()
    if not _REL_TYPE_PARAM_RE.match(rt):
        raise ValueError(
            "关系类型须为 Neo4j 合法形式：大写字母开头，仅含 A–Z、0–9、下划线，长度 1–63"
        )
    dir_ = (direction or "out").strip().lower()
    if dir_ == "out":
        cypher = """
        MATCH (anchor {key: $ak})-[r]->(other {key: $bk})
        WHERE type(r) = $rt AND (anchor:Entity OR anchor:Event)
        DELETE r
        """
    elif dir_ == "in":
        cypher = """
        MATCH (other {key: $bk})-[r]->(anchor {key: $ak})
        WHERE type(r) = $rt AND (anchor:Entity OR anchor:Event)
        DELETE r
        """
    else:
        raise ValueError("direction 须为 in 或 out")
    with neo4j_session(driver) as s:
        result = s.run(cypher, ak=ak, bk=bk, rt=rt)
        summary = result.consume()
        n_del = int(summary.counters.relationships_deleted)
    if n_del:
        log.info("kg_admin: delete edge %s %s %s → other=%s (n=%s)", ak, dir_, rt, bk, n_del)
    return {"ok": True, "deleted": n_del}


def delete_edges_from_anchor_by_type(
    driver: Driver,
    *,
    anchor_key: str,
    rel_type: str,
    direction_scope: Literal["both", "out", "in"] = "both",
) -> dict[str, Any]:
    """删除锚点（Entity/Event）上某一 ``type(r)`` 的全部边，可按出/入/双向范围。"""
    ak = (anchor_key or "").strip()
    if not ak:
        raise ValueError("anchor_key 不能为空")
    rt = (rel_type or "").strip().upper()
    if not _REL_TYPE_PARAM_RE.match(rt):
        raise ValueError(
            "关系类型须为 Neo4j 合法形式：大写字母开头，仅含 A–Z、0–9、下划线，长度 1–63"
        )
    scope = (direction_scope or "both").strip().lower()
    if scope not in ("both", "out", "in"):
        raise ValueError("direction_scope 须为 both / out / in")

    cy_out = """
    MATCH (anchor {key: $ak})-[r]->()
    WHERE type(r) = $rt AND (anchor:Entity OR anchor:Event)
    DELETE r
    """
    cy_in = """
    MATCH ()-[r]->(anchor {key: $ak})
    WHERE type(r) = $rt AND (anchor:Entity OR anchor:Event)
    DELETE r
    """
    cy_both = """
    MATCH (anchor {key: $ak})-[r]-()
    WHERE type(r) = $rt AND (anchor:Entity OR anchor:Event)
    DELETE r
    """
    n_total = 0
    with neo4j_session(driver) as s:
        if scope == "out":
            res = s.run(cy_out, ak=ak, rt=rt)
            n_total += int(res.consume().counters.relationships_deleted)
        elif scope == "in":
            res = s.run(cy_in, ak=ak, rt=rt)
            n_total += int(res.consume().counters.relationships_deleted)
        else:
            res = s.run(cy_both, ak=ak, rt=rt)
            n_total += int(res.consume().counters.relationships_deleted)
    log.info("kg_admin: delete by type anchor=%s rt=%s scope=%s n=%s", ak, rt, scope, n_total)
    return {"ok": True, "deleted": n_total}


def merge_related_to(
    driver: Driver,
    *,
    from_key: str,
    to_key: str,
    relation_subtype: str = "see_also",
    description: str = "",
) -> dict[str, Any]:
    fk = (from_key or "").strip()
    tk = (to_key or "").strip()
    if not fk or not tk or fk == tk:
        raise ValueError("from_key / to_key 无效")
    st = (relation_subtype or "see_also").strip()[:64]
    if st not in _REL_SUBTYPES:
        st = "see_also"
    link = {
        "from_key": fk,
        "to_key": tk,
        "description": (description or "")[:500],
        "relation_subtype": st,
        "scope": "",
        "sim_score": None,
    }
    with neo4j_session(driver) as s:
        s.execute_write(_write_entity_links, links=[link])
    log.info("kg_admin: RELATED_TO %s ↔ %s (bidirectional)", fk, tk)
    return {"ok": True}


def delete_related_to(driver: Driver, *, from_key: str, to_key: str) -> dict[str, Any]:
    fk = (from_key or "").strip()
    tk = (to_key or "").strip()
    if not fk or not tk:
        raise ValueError("from_key / to_key 无效")
    cy_fwd = """
    MATCH (a:Entity {key: $fk})-[r:RELATED_TO]->(b:Entity {key: $tk})
    DELETE r
    """
    cy_rev = """
    MATCH (a:Entity {key: $tk})-[r:RELATED_TO]->(b:Entity {key: $fk})
    DELETE r
    """
    deleted = 0
    with neo4j_session(driver) as s:
        deleted += int(s.run(cy_fwd, fk=fk, tk=tk).consume().counters.relationships_deleted)
        deleted += int(s.run(cy_rev, fk=fk, tk=tk).consume().counters.relationships_deleted)
    return {"ok": True, "deleted": deleted}


def set_event_about(driver: Driver, *, event_key: str, entity_key: str, connect: bool) -> dict[str, Any]:
    ek = (event_key or "").strip()
    entk = (entity_key or "").strip()
    if not ek or not entk:
        raise ValueError("event_key / entity_key 无效")
    if connect:
        cypher = """
        MATCH (ev:Event {key: $ek})
        MATCH (ent:Entity {key: $entk})
        MERGE (ev)-[:ABOUT]->(ent)
        RETURN 1 AS ok
        """
    else:
        cypher = """
        MATCH (ev:Event {key: $ek})-[r:ABOUT]->(ent:Entity {key: $entk})
        DELETE r
        RETURN 1 AS ok
        """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, ek=ek, entk=entk).single()
    ok = bool(rec)
    log.info("kg_admin: Event ABOUT %s %s %s", ek, "connect" if connect else "disconnect", entk)
    return {"ok": ok}


# ═══════════════════════════════════════════════════════════════════════ #
#  Cross-graph edge CRUD
# ═══════════════════════════════════════════════════════════════════════ #

_CROSS_GRAPH_EDGE_WHITELIST = frozenset(
    {"SURFACE_METRIC", "SURFACE_DIMENSION", "SURFACE_DOMAIN", "HAS_INSTANCE"}
)


def merge_cross_graph_edge(
    driver: Driver,
    *,
    from_key: str,
    to_key: str,
    rel_type: str,
    properties: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Create or update a whitelisted cross-graph edge between two nodes."""
    fk = (from_key or "").strip()
    tk = (to_key or "").strip()
    rt = (rel_type or "").strip()
    if not fk or not tk:
        raise ValueError("from_key / to_key 不能为空")
    if rt not in _CROSS_GRAPH_EDGE_WHITELIST:
        raise ValueError(
            f"不支持的跨图边类型: {rt}。允许: {', '.join(sorted(_CROSS_GRAPH_EDGE_WHITELIST))}"
        )
    props = {k: v for k, v in (properties or {}).items() if v is not None}
    props["zone"] = "knowledge"

    cypher = f"""
    MATCH (a {{key: $fk}})
    MATCH (b {{key: $tk}})
    MERGE (a)-[r:`{rt}`]->(b)
    SET r += $props
    RETURN type(r) AS rel_type
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, fk=fk, tk=tk, props=props).single()
    ok = bool(rec)
    log.info("kg_admin: cross-graph edge %s %s → %s", rt, fk, tk)
    return {"ok": ok, "from_key": fk, "to_key": tk, "rel_type": rt}


def delete_cross_graph_edge(
    driver: Driver,
    *,
    from_key: str,
    to_key: str,
    rel_type: str,
) -> dict[str, Any]:
    """Remove a whitelisted cross-graph edge by endpoints and type."""
    fk = (from_key or "").strip()
    tk = (to_key or "").strip()
    rt = (rel_type or "").strip()
    if not fk or not tk:
        raise ValueError("from_key / to_key 不能为空")
    if rt not in _CROSS_GRAPH_EDGE_WHITELIST:
        raise ValueError(
            f"不支持的跨图边类型: {rt}。允许: {', '.join(sorted(_CROSS_GRAPH_EDGE_WHITELIST))}"
        )
    cypher = f"""
    MATCH (a {{key: $fk}})-[r:`{rt}`]->(b {{key: $tk}})
    DELETE r
    RETURN 1 AS ok
    """
    with neo4j_session(driver) as s:
        result = s.run(cypher, fk=fk, tk=tk)
        n_del = int(result.consume().counters.relationships_deleted)
    log.info("kg_admin: delete cross-graph edge %s %s → %s (n=%s)", rt, fk, tk, n_del)
    return {"ok": True, "deleted": n_del}


# ═══════════════════════════════════════════════════════════════════════ #
#  Edge property update (whitelist-controlled)
# ═══════════════════════════════════════════════════════════════════════ #

_EDGE_EDITABLE_FIELDS: dict[str, frozenset[str]] = {
    "RELATED_TO": frozenset({"relation_subtype", "description", "scope"}),
    "ABOUT": frozenset({"notes"}),
    "SURFACE_METRIC": frozenset({"role", "notes"}),
    "SURFACE_DIMENSION": frozenset({"role"}),
}


def update_edge_properties(
    driver: Driver,
    *,
    from_key: str,
    to_key: str,
    rel_type: str,
    properties: dict[str, Any],
) -> dict[str, Any]:
    """Update whitelisted properties on an existing edge."""
    fk = (from_key or "").strip()
    tk = (to_key or "").strip()
    rt = (rel_type or "").strip()
    if not fk or not tk or not rt:
        raise ValueError("from_key / to_key / rel_type 不能为空")
    allowed = _EDGE_EDITABLE_FIELDS.get(rt)
    if allowed is None:
        raise ValueError(f"边类型 {rt} 不支持属性编辑")
    bad_keys = set(properties.keys()) - allowed
    if bad_keys:
        raise ValueError(
            f"边类型 {rt} 不允许编辑字段: {', '.join(sorted(bad_keys))}。"
            f"允许: {', '.join(sorted(allowed))}"
        )
    if not properties:
        raise ValueError("properties 不能为空")
    cypher = f"""
    MATCH (a {{key: $fk}})-[r:`{rt}`]->(b {{key: $tk}})
    SET r += $props
    RETURN properties(r) AS updated
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, fk=fk, tk=tk, props=properties).single()
    if not rec:
        raise ValueError(f"未找到边: {fk} -[{rt}]-> {tk}")
    updated = _json_safe_props(dict(rec["updated"] or {}))
    log.info("kg_admin: update edge props %s %s → %s: %s", rt, fk, tk, list(properties.keys()))
    return {
        "ok": True,
        "from_key": fk,
        "to_key": tk,
        "rel_type": rt,
        "updated_properties": updated,
    }

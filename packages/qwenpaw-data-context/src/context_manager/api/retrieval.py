"""NL → 图谱检索（Cypher 查询内联于本模块）。

提供两个核心公共函数：

- :func:`resolve_metric`  全文检索 (``metric_text``) → 候选 metric (key/name/score)
- :func:`expand_subgraph` 给定 metric_key，展开全部上下文：
     formulas / tables / columns / drill dims / calibers / derived metrics

返回结构都是 plain ``dict``，前端 vis-network 直接消费。
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any, Optional

from neo4j import Driver

from ..embedder import embed_one
from ..config import CFG
from ..rrf import rrf_merge as _rrf_merge
from ..utils import get_logger, neo4j_database_ctx, neo4j_session

log = get_logger("api.retrieval")

# ---------------------------------------------------------------------- #
# Relevance gate — soft text match + vector cosine (see runtime/relevance.py)
# ---------------------------------------------------------------------- #
_RRF_SCALE = 20.0  # legacy: scale RRF (~0-0.05) → [0,1] when no cosine is present


def relevance_gate(
    rows: list[dict[str, Any]],
    query: str,
    *,
    threshold: Optional[float] = None,
    name_field: str = "name",
    aliases_field: str = "aliases",
    score_field: str = "score",
    vec_score_field: str = "vec_score",
    floor: Optional[float] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Relevance gate: CJK-soft text match + vector cosine → threshold filter.

    Dense signal preference: when a row carries ``vec_score`` (raw Neo4j cosine),
    it is rescaled and used as the dense signal. Rows without a cosine fall back
    to the legacy RRF-scaled proxy (``score`` field), so callers that don't yet
    propagate cosine keep working (just with a weaker dense signal).

    Returns:
        (filtered_rows, gate_info) where gate_info has keys:
        - status: "relevant" | "low_confidence" | "no_match"
        - score: best blended score (0-1)
        - matched_name: name of best matching row (or "")
    """
    from ..runtime.relevance import (
        blend_relevance,
        classify,
        normalize_cosine,
        soft_text_match,
    )
    from ..config import CFG

    eff_threshold = (
        threshold
        if (threshold is not None and threshold > 0)
        else CFG.relevance_threshold
    )
    eff_floor = floor if floor is not None else CFG.relevance_floor

    if not rows:
        return [], {"status": "no_match", "score": 0.0, "matched_name": ""}

    best_score = 0.0
    best_name = ""
    scored_rows: list[tuple[float, dict[str, Any]]] = []

    for row in rows:
        name = str(row.get(name_field, "") or "")
        aliases = row.get(aliases_field, []) or []
        description = str(row.get("description", "") or "")
        text = soft_text_match(query, name, aliases, description)

        if vec_score_field in row and row.get(vec_score_field) is not None:
            vecn = normalize_cosine(float(row.get(vec_score_field) or 0.0))
        else:
            # legacy fallback: treat RRF-scaled score as a (weak) dense proxy
            vecn = min(1.0, float(row.get(score_field, 0) or 0) * _RRF_SCALE)

        combined = blend_relevance(text=text, vec_cosine_normalized=vecn)
        scored_rows.append((combined, row))
        if combined > best_score:
            best_score = combined
            best_name = name

    status = classify(best_score, threshold=eff_threshold, floor=eff_floor)

    if status == "no_match":
        return [], {"status": status, "score": round(best_score, 3), "matched_name": ""}

    filtered = [row for score, row in scored_rows if score >= eff_threshold]
    return filtered, {
        "status": status,
        "score": round(best_score, 3),
        "matched_name": best_name,
    }


# ---------------------------------------------------------------------- #
# Hybrid retrieval helpers (fulltext + vector + RRF)
# ---------------------------------------------------------------------- #
def _vector_search(
    driver: Driver,
    *,
    index_name: str,
    query_vec: list[float],
    k: int,
    domain: Optional[str] = None,
) -> list[dict[str, Any]]:
    """对给定向量索引做近似最近邻检索；返回 ``[{key, score, ...node fields}]``。

    走 Neo4j 5 的 ``db.index.vector.queryNodes``。
    ``domain`` 不为空时在 WHERE 子句中按 domain 过滤。
    """
    if not query_vec:
        return []
    domain_clause = "AND node.domain = $domain" if domain else ""
    cypher = f"""
    CALL db.index.vector.queryNodes('{index_name}', $k, $vec) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
      {domain_clause}
    RETURN node.key AS key, node.name AS name, node.domain AS domain,
           coalesce(node.aliases, []) AS aliases,
           coalesce(node.description, '') AS description,
           coalesce(node.unit, '') AS unit, node.type AS type,
           node.comment AS comment, node.table AS table,
           coalesce(node.source_trust, 1.0) AS source_trust,
           node.datasource_id AS datasource_id,
           score
    ORDER BY score DESC, source_trust DESC
    """
    params: dict[str, Any] = {"k": k, "vec": query_vec}
    if domain:
        params["domain"] = domain
    with neo4j_session(driver) as s:
        try:
            return s.run(cypher, **params).data()
        except Exception as exc:
            log.warning("vector index %r query failed: %s", index_name, exc)
            return []


# ---------------------------------------------------------------------- #
# NL 解析（轻量级：仅子串匹配 + 全文检索；LLM 留作 v0.2）
# ---------------------------------------------------------------------- #


def detect_dimensions(
    driver: Driver, domain: Optional[str], query: str, datasource_id: str = ""
) -> list[dict[str, Any]]:
    """在 ``Dimension.name + aliases`` 里做子串匹配；可选限定 domain 与 datasource_id。"""
    if not query:
        return []
    ds = (datasource_id or "").strip()
    cypher = """
    MATCH (d:Dimension)
    WHERE ($domain IS NULL OR d.domain = $domain)
      AND ($ds = '' OR d.datasource_id = $ds)
      AND (
        toLower($q) CONTAINS toLower(d.name) OR
        any(syn IN coalesce(d.aliases, []) WHERE toLower($q) CONTAINS toLower(syn))
      )
    RETURN d.key AS key, d.name AS name, d.domain AS domain
    ORDER BY size(d.name) DESC LIMIT 5
    """
    with neo4j_session(driver) as s:
        return s.run(cypher, q=query, domain=domain, ds=ds).data()


def list_domains(driver: Driver) -> list[str]:
    """所有 ``Domain.name`` 列表，缓存用。"""
    with neo4j_session(driver) as s:
        rows = s.run("MATCH (d:Domain) RETURN d.name AS name ORDER BY name").data()
    return [r["name"] for r in rows if r.get("name")]


def detect_domain(driver: Driver, query: str) -> Optional[str]:
    """如果 query 里含某个 ``Domain.name`` 子串（不区分大小写），返回它。否则 ``None``。"""
    q = (query or "").lower()
    if not q:
        return None
    for name in list_domains(driver):
        if name.lower() in q:
            return name
    return None


# ---------------------------------------------------------------------- #
# Q-1: 自然语言 → metric (Hybrid: fulltext + vector via RRF)
# ---------------------------------------------------------------------- #
def _fulltext_search_metrics(
    driver: Driver, query: str, k: int, domain: Optional[str] = None
) -> list[dict[str, Any]]:
    domain_clause = "AND node.domain = $domain" if domain else ""
    cypher = f"""
    CALL db.index.fulltext.queryNodes('metric_text', $q) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
      {domain_clause}
    RETURN node.key AS key, node.name AS name, node.domain AS domain,
           coalesce(node.aliases, []) AS aliases,
           coalesce(node.description, '') AS description,
           coalesce(node.unit, '') AS unit, node.type AS type,
           coalesce(node.source_trust, 1.0) AS source_trust,
           node.datasource_id AS datasource_id,
           score
    ORDER BY score DESC, source_trust DESC LIMIT $k
    """
    params: dict[str, Any] = {"q": query, "k": k}
    if domain:
        params["domain"] = domain
    with neo4j_session(driver) as s:
        try:
            return s.run(cypher, **params).data()
        except Exception as exc:
            log.warning("fulltext 'metric_text' failed (%s); fallback to LIKE", exc)
    domain_clause_fb = "AND m.domain = $domain" if domain else ""
    fallback = f"""
    MATCH (m:Metric)
    WHERE (m.valid_to IS NULL OR m.valid_to > datetime()) AND (
        toLower(m.name) CONTAINS toLower($q) OR
        any(syn IN coalesce(m.aliases, []) WHERE toLower(syn) CONTAINS toLower($q))
    )
      {domain_clause_fb}
    RETURN m.key AS key, m.name AS name, m.domain AS domain,
           coalesce(m.aliases, []) AS aliases,
           coalesce(m.description, '') AS description,
           coalesce(m.unit, '') AS unit, m.type AS type,
           coalesce(m.source_trust, 1.0) AS source_trust, 0.5 AS score,
           m.datasource_id AS datasource_id
    ORDER BY source_trust DESC LIMIT $k
    """
    with neo4j_session(driver) as s:
        return s.run(fallback, **params).data()


def resolve_metric(
    driver: Driver,
    query: str,
    *,
    k: int = 5,
    domain: Optional[str] = None,
    use_vector: bool = True,
    datasource_id: str = "",
) -> list[dict[str, Any]]:
    """Hybrid retrieval：fulltext (Lucene) + vector (cosine on ``met_vec``) + RRF。

    - ``use_vector=True`` 且 query 不为空时，先 ``embed(query)`` 走向量近邻；
    - 同时跑 Lucene；
    - RRF 融合。``score`` 字段返回的是 RRF 分数（0–0.05 量级，正比于相关性）。
    - 没有任何向量索引可用时自动 fallback 到原 fulltext-only 行为。
    - ``domain`` 不为空时，在检索阶段即按 domain 过滤（fulltext WHERE / vector WHERE），
      避免跨域候选进入 RRF 融合。
    - ``datasource_id`` 不为空时，在 domain 过滤后进一步按 ``node.datasource_id``
      收窄；过滤后为空才回退到（同 domain 的）全集，避免过严丢候选。
    """
    if not (query or "").strip():
        return []
    pool_k = max(k * 4, 20) if domain else max(k * 2, 10)

    rankings: list[list[dict[str, Any]]] = []
    rankings.append(_fulltext_search_metrics(driver, query, pool_k, domain=domain))

    vec_score_by_key: dict[str, float] = {}
    if use_vector:
        try:
            qvec = embed_one(query)
        except Exception as exc:
            log.warning("embed_one failed (%s); skipping vector path", exc)
            qvec = None
        if qvec:
            vec_rows = _vector_search(
                driver, index_name="met_vec", query_vec=qvec, k=pool_k, domain=domain
            )
            if vec_rows:
                rankings.append(vec_rows)
                for r in vec_rows:
                    row_key = r.get("key")
                    if row_key is not None:
                        sc = float(r.get("score") or 0.0)
                        if sc > vec_score_by_key.get(row_key, 0.0):
                            vec_score_by_key[row_key] = sc

    if len(rankings) == 1:
        rows = rankings[0]
    else:
        rows = _rrf_merge(rankings, key_field="key")

    # Preserve raw cosine alongside RRF so the relevance gate has a dense signal.
    for r in rows:
        r["vec_score"] = vec_score_by_key.get(r.get("key"), 0.0)

    ds = (datasource_id or "").strip()
    if ds:
        filtered = [r for r in rows if (r.get("datasource_id") or "") == ds]
        if filtered:
            rows = filtered
    return rows[:k]


def _event_search_row_cypher_return() -> str:
    return """
    RETURN node.key AS key, coalesce(node.name, '') AS name, coalesce(node.type, '') AS type,
           coalesce(node.scope, '') AS scope, coalesce(node.description, '') AS description,
           CASE WHEN node.date_from IS NULL THEN '' ELSE toString(node.date_from) END AS date_from,
           CASE WHEN node.date_to IS NULL THEN '' ELSE toString(node.date_to) END AS date_to,
           coalesce(ent.key, '') AS about_entity_key,
           coalesce(ent.name, ent.canonical_name, '') AS about_entity_name,
           score
    """


def _fulltext_search_events(driver: Driver, query: str, k: int) -> list[dict[str, Any]]:
    cypher = f"""
    CALL db.index.fulltext.queryNodes('event_text', $q) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
    OPTIONAL MATCH (node)-[:ABOUT]->(ent:Entity)
    {_event_search_row_cypher_return()}
    ORDER BY score DESC LIMIT $k
    """
    with neo4j_session(driver) as s:
        try:
            return s.run(cypher, q=query, k=k).data()
        except Exception as exc:
            log.warning("fulltext 'event_text' failed (%s); fallback to LIKE", exc)
    fallback = """
    MATCH (ev:Event)
    WHERE (ev.valid_to IS NULL OR ev.valid_to > datetime())
      AND (
        toLower(coalesce(ev.name, '')) CONTAINS toLower($q) OR
        toLower(coalesce(ev.description, '')) CONTAINS toLower($q) OR
        toLower(coalesce(ev.type, '')) CONTAINS toLower($q)
      )
    OPTIONAL MATCH (ev)-[:ABOUT]->(ent:Entity)
    RETURN ev.key AS key, coalesce(ev.name, '') AS name, coalesce(ev.type, '') AS type,
           coalesce(ev.scope, '') AS scope, coalesce(ev.description, '') AS description,
           CASE WHEN ev.date_from IS NULL THEN '' ELSE toString(ev.date_from) END AS date_from,
           CASE WHEN ev.date_to IS NULL THEN '' ELSE toString(ev.date_to) END AS date_to,
           coalesce(ent.key, '') AS about_entity_key,
           coalesce(ent.name, ent.canonical_name, '') AS about_entity_name,
           0.5 AS score
    ORDER BY score DESC, ev.date_from DESC LIMIT $k
    """
    with neo4j_session(driver) as s:
        return s.run(fallback, q=query, k=k).data()


def _vector_search_events(
    driver: Driver, query_vec: list[float], k: int
) -> list[dict[str, Any]]:
    if not query_vec:
        return []
    cypher = f"""
    CALL db.index.vector.queryNodes('ev_vec', $k, $vec) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
    OPTIONAL MATCH (node)-[:ABOUT]->(ent:Entity)
    {_event_search_row_cypher_return()}
    ORDER BY score DESC LIMIT $k
    """
    with neo4j_session(driver) as s:
        try:
            return s.run(cypher, k=k, vec=query_vec).data()
        except Exception as exc:
            log.warning("vector index 'ev_vec' query failed: %s", exc)
            return []


def search_events(
    driver: Driver,
    query: str,
    *,
    limit: int = 10,
    use_vector: bool = True,
) -> list[dict[str, Any]]:
    """自然语言 → Event 召回（仅 ``:Event``；全文 ``event_text`` + 向量 ``ev_vec`` RRF）。

    ``limit`` 默认 10，最大建议 50（由 API 层校验）。
    ``score`` 为 RRF 融合分（与 ``resolve_metric`` 同量级）。
    """
    q = (query or "").strip()
    if not q:
        return []
    k = max(1, min(int(limit), 50))
    pool_k = max(k * 3, 20)

    rankings: list[list[dict[str, Any]]] = []
    rankings.append(_fulltext_search_events(driver, q, pool_k))

    vec_score_by_key: dict[str, float] = {}
    if use_vector:
        try:
            qvec = embed_one(q)
        except Exception as exc:
            log.warning("embed_one failed for events (%s); skipping vector path", exc)
            qvec = None
        if qvec:
            vec_rows = _vector_search_events(driver, qvec, pool_k)
            if vec_rows:
                rankings.append(vec_rows)
                for r in vec_rows:
                    kk = r.get("key")
                    if kk is not None:
                        sc = float(r.get("score") or 0.0)
                        if sc > vec_score_by_key.get(kk, 0.0):
                            vec_score_by_key[kk] = sc

    if len(rankings) == 1:
        rows = rankings[0]
    else:
        rows = _rrf_merge(rankings, key_field="key")
    for r in rows:
        r["vec_score"] = vec_score_by_key.get(r.get("key"), 0.0)
    return rows[:k]


def resolve_column(
    driver: Driver,
    query: str,
    *,
    k: int = 8,
    domain_table_keys: Optional[list[str]] = None,
) -> list[dict[str, Any]]:
    """语义级别的 column 召回；对 metric 没覆盖的口径（"新增用户" 但没建 metric）兜底。

    检索路径：
    1. ``col_vec`` 向量近邻
    2. ``col_text`` 全文（name + comment）
    3. RRF 融合

    ``domain_table_keys`` 不为空时把候选限制在这些表的列。
    """
    if not (query or "").strip():
        return []
    pool_k = max(k * 3, 15)

    rankings: list[list[dict[str, Any]]] = []

    # vector
    try:
        qvec = embed_one(query)
    except Exception as exc:
        log.warning("embed_one failed for column: %s", exc)
        qvec = None
    if qvec:
        rankings.append(
            _vector_search(driver, index_name="col_vec", query_vec=qvec, k=pool_k)
        )

    # fulltext
    cypher_ft = """
    CALL db.index.fulltext.queryNodes('col_text', $q) YIELD node, score
    RETURN node.key AS key, node.name AS name, node.table AS table,
           coalesce(node.comment, '') AS comment, node.db AS db,
           node.schema AS schema, score
    ORDER BY score DESC LIMIT $k
    """
    with neo4j_session(driver) as s:
        try:
            rankings.append(s.run(cypher_ft, q=query, k=pool_k).data())
        except Exception as exc:
            log.warning("fulltext 'col_text' failed: %s", exc)

    rows = _rrf_merge([r for r in rankings if r], key_field="key") if rankings else []

    if domain_table_keys:
        # column.key = col:db.schema.table.col → 用前缀匹配
        prefixes = [tk.replace("tbl:", "col:") + "." for tk in domain_table_keys]
        rows = [
            r
            for r in rows
            if any(str(r.get("key", "")).startswith(p) for p in prefixes)
        ]
    return rows[:k]


# ---------------------------------------------------------------------- #
# 子图扩展（一次性把 metric 周边的 §6.2 全套结构拉回来）
# ---------------------------------------------------------------------- #
def expand_subgraph(
    driver: Driver,
    metric_key: str,
    *,
    include_anomaly: bool = True,
    include_drill: bool = True,
    include_calibers: bool = True,
    include_derived: bool = True,
    include_cross_graph: bool = True,
) -> dict[str, Any]:
    """给定 metric_key，把它周边的检索/取数所需的全部节点 + 边返回。

    形如：::

        {
          "center": {...metric},
          "nodes": [{id, label, group, props}, ...],
          "edges": [{from, to, type, props}, ...],
          "raw": {"formulas": [...], "drill_dims": [...], ...}
        }
    """
    cypher = """
    MATCH (m:Metric {key: $met_key})
    OPTIONAL MATCH (m)<-[:HAS_METRIC]-(dom:Domain)

    // 公式 → 表 → 列（USES_COLUMN 同时匹配 Column 和 DatasetColumn）
    OPTIONAL MATCH (m)-[hf:HAS_FORMULA]->(f:Formula)
    OPTIONAL MATCH (f)-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t:Table)
    OPTIONAL MATCH (f)-[uc:USES_COLUMN]->(col)
      WHERE col:Column OR col:DatasetColumn

    // drill 维度（MAPS_TO_COLUMN 或 MAPS_TO_DATASET_COLUMN）
    OPTIONAL MATCH (m)-[cd:ANALYZED_BY]->(dim:Dimension)
    OPTIONAL MATCH (dim)-[mc:MAPS_TO_COLUMN|MAPS_TO_DATASET_COLUMN]->(dim_col)
      WHERE dim_col:Column OR dim_col:DatasetColumn

    // 派生
    OPTIONAL MATCH (m)-[df:DERIVED_FROM]->(parent:Metric)

    // 口径
    OPTIONAL MATCH (m)-[hcal:HAS_CALIBER]->(cal:Caliber)
    OPTIONAL MATCH (cal)-[fo:FILTER_ON]->(cal_col:Column)

    RETURN
      m  AS metric, dom AS domain,
      collect(DISTINCT {f: f, t: t, col: col, role: uc.role}) AS formula_rows,
      collect(DISTINCT {dim: dim, col: dim_col, expr: mc.expr, filter: mc.filter,
                         binding_type: mc.binding_type}) AS drill_rows,
      collect(DISTINCT {parent: parent, role: df.role, relation_type: df.relation_type}) AS derived_rows,
      collect(DISTINCT {cal: cal, col: cal_col}) AS caliber_rows
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, met_key=metric_key).single()
    if not rec or rec["metric"] is None:
        return {"center": None, "nodes": [], "edges": [], "raw": {}}

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(node, group: str) -> Optional[str]:
        if node is None:
            return None
        # neo4j Node 对象 → dict
        props = dict(node)
        key = props.get("key")
        if not key:
            return None
        if key not in nodes:
            nodes[key] = {
                "id": key,
                "label": _node_label(props, group),
                "group": group,
                "props": _trim_props(props),
            }
        return key

    def add_edge(
        src: Optional[str], dst: Optional[str], rel: str, props: Optional[dict] = None
    ) -> None:
        if not (src and dst):
            return
        edges.append({"from": src, "to": dst, "type": rel, "props": props or {}})

    metric_node = rec["metric"]
    metric_key_actual = add_node(metric_node, "Metric")
    domain_key_actual = add_node(rec.get("domain"), "Domain")
    add_edge(domain_key_actual, metric_key_actual, "HAS_METRIC")

    raw: dict[str, Any] = {
        "metric": _trim_props(dict(metric_node)),
        "formulas": [],
        "drill_dims": [],
        "anomaly_rules": [],
        "derived": [],
        "calibers": [],
        "policies": [],
    }

    # formulas (含表/列)
    fml_seen: dict[str, dict] = {}
    for r in rec["formula_rows"] or []:
        if not r or not r.get("f"):
            continue
        f = r["f"]
        f_props = dict(f)
        f_key = add_node(f, "Formula")
        add_edge(metric_key_actual, f_key, "HAS_FORMULA")
        if r.get("t"):
            t_key = add_node(r["t"], "Table")
            add_edge(f_key, t_key, "OF_VIEW")
        if r.get("col"):
            _col_props = dict(r["col"])
            _col_group = (
                "DatasetColumn"
                if str(_col_props.get("key", "")).startswith("dscol:")
                else "Column"
            )
            c_key = add_node(r["col"], _col_group)
            add_edge(f_key, c_key, "USES_COLUMN", {"role": r.get("role") or ""})
        # 整理 raw.formulas
        fk = f_props.get("key")
        if fk and fk not in fml_seen:
            fml_seen[fk] = {
                "key": fk,
                "dataset": f_props.get("dataset"),
                "formula": f_props.get("formula"),
                "formula_evidence": f_props.get("formula_evidence") or "",
                "partition_predicate": f_props.get("partition_predicate") or "",
                "table_key": (dict(r["t"]).get("key") if r.get("t") else None),
                "uses_columns": [],
            }
        if fk and r.get("col"):
            fml_seen[fk]["uses_columns"].append(
                {"key": dict(r["col"]).get("key"), "role": r.get("role") or ""}
            )
    raw["formulas"] = list(fml_seen.values())

    # drill dims
    if include_drill:
        seen_dim: set[str] = set()
        for r in rec["drill_rows"] or []:
            if not r or not r.get("dim"):
                continue
            dim_props = dict(r["dim"])
            dim_key = add_node(r["dim"], "Dimension")
            add_edge(metric_key_actual, dim_key, "ANALYZED_BY")
            if r.get("col"):
                _dc_props = dict(r["col"])
                _dc_group = (
                    "DatasetColumn"
                    if str(_dc_props.get("key", "")).startswith("dscol:")
                    else "Column"
                )
                col_key = add_node(r["col"], _dc_group)
                _dc_rel = (
                    "MAPS_TO_DATASET_COLUMN"
                    if _dc_group == "DatasetColumn"
                    else "MAPS_TO_COLUMN"
                )
                add_edge(
                    dim_key,
                    col_key,
                    _dc_rel,
                    {
                        "expr": r.get("expr") or "",
                        "filter": r.get("filter") or "",
                        "binding_type": r.get("binding_type") or "",
                    },
                )
            if dim_props.get("key") not in seen_dim:
                seen_dim.add(dim_props.get("key"))
                raw["drill_dims"].append(
                    {
                        "key": dim_props.get("key"),
                        "name": dim_props.get("name"),
                        "aliases": dim_props.get("aliases") or [],
                        "dimension_type": dim_props.get("dimension_type") or "OLAP维度",
                    }
                )

    # derived
    if include_derived:
        for r in rec["derived_rows"] or []:
            if not r or not r.get("parent"):
                continue
            pk = add_node(r["parent"], "Metric")
            add_edge(
                metric_key_actual,
                pk,
                "DERIVED_FROM",
                {"role": r.get("role"), "relation_type": r.get("relation_type")},
            )
            raw["derived"].append(
                {
                    "key": dict(r["parent"]).get("key"),
                    "name": dict(r["parent"]).get("name"),
                    "role": r.get("role"),
                    "relation_type": r.get("relation_type"),
                }
            )

    # calibers (同表的)
    if include_calibers:
        cal_seen: set[str] = set()
        for r in rec["caliber_rows"] or []:
            if not r or not r.get("cal"):
                continue
            cal_props = dict(r["cal"])
            ck = add_node(r["cal"], "Caliber")
            add_edge(metric_key_actual, ck, "HAS_CALIBER")
            if r.get("col"):
                col_key = add_node(r["col"], "Column")
                add_edge(ck, col_key, "FILTER_ON")
            if cal_props.get("key") not in cal_seen:
                cal_seen.add(cal_props.get("key"))
                raw["calibers"].append(
                    {
                        "key": cal_props.get("key"),
                        "value": cal_props.get("value"),
                        "column_key": cal_props.get("column_key"),
                    }
                )

    # Partition / rollup columns hanging off the formula's tables (or datasets).
    # These aren't referenced by USES_COLUMN (the formula doesn't compute on
    # them) but the agent needs them to write correct SQL.
    table_keys = [k for k, n in nodes.items() if n["group"] == "Table"]
    if table_keys:
        with neo4j_session(driver) as s:
            pc_rows = s.run(
                """
                MATCH (t:Table) WHERE t.key IN $tks
                MATCH (t)-[:HAS_COLUMN]->(pc:Column)
                WHERE coalesce(pc.granularity_role, '') <> ''
                   OR coalesce(pc.topline_value, '') <> ''
                RETURN t.key AS tk, pc AS col
                """,
                tks=table_keys,
            ).data()
        for r in pc_rows:
            c_key = add_node(r["col"], "Column")
            add_edge(r["tk"], c_key, "HAS_COLUMN")

    # DatasetColumn partition info (synced from Column)
    dc_keys = [k for k, n in nodes.items() if n["group"] == "DatasetColumn"]
    if dc_keys and not table_keys:
        with neo4j_session(driver) as s:
            dc_pc_rows = s.run(
                """
                MATCH (ds:Dataset)-[:HAS_COLUMN]->(dc:DatasetColumn)
                WHERE dc.key IN $dks
                  AND (coalesce(dc.granularity_role, '') <> ''
                       OR coalesce(dc.topline_value, '') <> '')
                RETURN ds.key AS dk, dc AS col
                """,
                dks=dc_keys,
            ).data()
        for r in dc_pc_rows:
            c_key = add_node(r["col"], "DatasetColumn")
            add_edge(r["dk"], c_key, "HAS_COLUMN")

    # DatasetColumn composite: 把 COMPOSED_OF 边和目标 Column 带入子图
    composite_dc_keys = [
        k
        for k, n in nodes.items()
        if n["group"] == "DatasetColumn" and n["props"].get("composite")
    ]
    if composite_dc_keys:
        with neo4j_session(driver) as s:
            comp_rows = s.run(
                """
                MATCH (dc:DatasetColumn)-[r:COMPOSED_OF]->(col:Column)
                WHERE dc.key IN $dks
                RETURN dc.key AS dk, r.role AS role, col
                """,
                dks=composite_dc_keys,
            ).data()
        for r in comp_rows:
            col_key = add_node(r["col"], "Column")
            add_edge(r["dk"], col_key, "COMPOSED_OF", {"role": r["role"] or ""})

    return {
        "center": metric_key_actual,
        "nodes": list(nodes.values()),
        "edges": edges,
        "raw": raw,
    }


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #
_PROP_MAX_LEN = 400


def _trim_props(props: dict) -> dict:
    """节点属性传给前端时把超长 ddl/formula_evidence 截一下，避免 payload 过大。"""
    out: dict = {}
    for k, v in props.items():
        if isinstance(v, str) and len(v) > _PROP_MAX_LEN:
            out[k] = v[:_PROP_MAX_LEN] + "…"
        elif hasattr(v, "iso_format"):  # neo4j.time.DateTime / Date
            out[k] = v.iso_format()
        else:
            out[k] = v
    return out


def _node_label(props: dict, group: str) -> str:
    """节点在图里显示的短文本。优先 name → key 末段。"""
    if group in (
        "Metric",
        "Dimension",
        "Domain",
        "Entity",
        "Event",
    ):
        return str(props.get("name") or props.get("key") or "")
    if group == "Formula":
        ds = str(props.get("dataset") or "")
        return ds.rsplit(".", 1)[-1] if ds else str(props.get("key", ""))
    if group == "Table":
        return str(props.get("name") or props.get("key") or "")
    if group == "DatasetColumn":
        return str(props.get("display_name") or props.get("name") or "?")
    if group == "Column":
        return f"{props.get('table') or '?'}.{props.get('name') or '?'}"
    if group == "Caliber":
        return str(props.get("value") or props.get("key") or "")
    return str(props.get("key") or props.get("name") or group)


_GRAPH_KEY_PREFIXES = (
    "met:",
    "tbl:",
    "col:",
    "fml:",
    "dim:",
    "dom:",
    "db:",
    "schema:",
    "task:",
    "plan:",
)


def _trim_key_token(s: str) -> str:
    return str(s).strip().rstrip(".,;)}]'\"")


def _extract_embedded_graph_keys(text: str) -> list[str]:
    """从长文本里抽出 ``met:`` / ``tbl:`` 等 token（trigger_conditions.strategy_semantics 等）。"""
    if not text or not isinstance(text, str):
        return []
    keys: list[str] = []
    for chunk in re.split(r"[\s,\[\]()]+", text):
        t = _trim_key_token(chunk)
        if t and any(t.startswith(p) for p in _GRAPH_KEY_PREFIXES):
            keys.append(t)
    return keys


def _parse_path_subgraph_keys(raw: Any) -> list[str]:
    """Neo4j 可能存 list、JSON 字符串或罕见标量；统一成有序 key 列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x is not None and str(x).strip()]
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        if s.startswith("["):
            try:
                j = json.loads(s)
                if isinstance(j, list):
                    return [str(x) for x in j if x is not None and str(x).strip()]
            except Exception:
                pass
        return [s]
    return [str(raw)] if raw else []


def _collect_keys_from_trigger_conditions(raw: Any) -> list[str]:
    """``trigger_conditions`` JSON：graph_db_id、可选 anchor_* key 列表、嵌套字符串里的图 key。"""
    keys: list[str] = []
    tc = raw
    if isinstance(tc, str):
        tc = tc.strip()
        if not tc:
            return []
        try:
            tc = json.loads(tc)
        except Exception:
            return _extract_embedded_graph_keys(tc)
    if not isinstance(tc, dict):
        return []

    gdb = tc.get("graph_db_id") or tc.get("gdb")
    if gdb:
        g = str(gdb).strip()
        if g:
            keys.append(f"db:{g}")

    for ak in (
        "anchor_keys",
        "entry_anchor_keys",
        "metric_keys",
        "seed_keys",
        "dimension_keys",
    ):
        v = tc.get(ak)
        if isinstance(v, list):
            for x in v:
                if x is not None and str(x).strip():
                    keys.append(str(x).strip())
        elif isinstance(v, str) and v.strip():
            keys.append(v.strip())

    def walk_obj(o: Any) -> None:
        if isinstance(o, str):
            keys.extend(_extract_embedded_graph_keys(o))
        elif isinstance(o, dict):
            for vv in o.values():
                walk_obj(vv)
        elif isinstance(o, (list, tuple)):
            for vv in o:
                walk_obj(vv)

    walk_obj(tc)

    out: list[str] = []
    seen: set[str] = set()
    for k in keys:
        kk = _trim_key_token(str(k))
        if kk and kk not in seen:
            seen.add(kk)
            out.append(kk)
    return out


def _strategy_card_extra_fetch_keys(
    nodes: dict[str, dict[str, Any]],
    *,
    max_total_nodes: int,
) -> list[str]:
    """为可视化补拉：路径上缺失的节点 + trigger_conditions 里解析到的 metadata key。"""
    want: list[str] = []
    for nv in nodes.values():
        if nv.get("group") != "Strategy":
            continue
        props = nv.get("props") or {}
        for pk in _parse_path_subgraph_keys(props.get("path_subgraph_keys")):
            pks = str(pk).strip()
            if pks and pks not in nodes:
                want.append(pks)
        want.extend(
            _collect_keys_from_trigger_conditions(props.get("trigger_conditions"))
        )

    budget = max(0, max_total_nodes - len(nodes))
    out: list[str] = []
    seen = set(nodes.keys())
    for k in want:
        kk = _trim_key_token(k)
        if not kk or kk in seen:
            continue
        seen.add(kk)
        out.append(kk)
        if len(out) >= budget:
            break
    return out


def _ensure_synthetic_path_step_node(
    nodes: dict[str, dict[str, Any]],
    step_id: str,
    *,
    step_index: int,
    card_key: str,
    bound_key: str,
) -> None:
    """路径可视化：在 trace 层插入合成 ``PathStep``（与元数据表/指标解耦，顺序只在 PathStep 链上）。"""
    if step_id in nodes:
        return
    nodes[step_id] = {
        "id": step_id,
        "label": f"path step {step_index + 1}",
        "group": "PathStep",
        "props": {
            "zone": "trace",
            "synthetic": True,
            "step": step_index,
            "strategy_card_key": card_key,
            "bound_key": bound_key,
        },
    }


def _append_strategy_card_synthetic_edges(
    nodes: dict[str, dict[str, Any]],
    edges_out: list[dict[str, Any]],
    *,
    max_synthetic: int = 500,
) -> None:
    """策略卡可视化专用合成结点/边（不写库）。

    - 对每个在快照内的 ``path_subgraph_keys`` 下标 ``i``：合成 ``PathStep``，
      ``PATH_BIND``：PathStep → 对应表/指标。
    - ``PATH_ORDER``：相邻 PathStep 之间（仅当 ``path_keys[i]`` 与 ``path_keys[i+1]`` 均在快照内）。
    - ``CARD_ENTRY``：卡 → 路径上第一个在快照内的 PathStep（若存在）；否则仍指向首 key 结点。
    - ``CARD_META``：卡 → ``trigger_conditions`` / ``graph_db_id`` 解析出的 metadata 结点。
    """
    seen_dir: set[tuple[str, str, str]] = set()
    n_added = 0

    def add_edge(fr: str, to: str, typ: str, **props_extra: Any) -> None:
        nonlocal n_added
        if n_added >= max_synthetic:
            return
        if fr not in nodes or to not in nodes or fr == to:
            return
        sig = (fr, typ, to)
        if sig in seen_dir:
            return
        seen_dir.add(sig)
        pr = {"synthetic": True, **props_extra}
        edges_out.append({"from": fr, "to": to, "type": typ, "props": pr})
        n_added += 1

    for nv in nodes.values():
        if nv.get("group") != "Strategy":
            continue
        ck = str(nv.get("id") or "")
        if not ck:
            continue
        props = nv.get("props") or {}
        path_keys = [
            _trim_key_token(x)
            for x in _parse_path_subgraph_keys(props.get("path_subgraph_keys"))
        ]
        path_keys = [x for x in path_keys if x]
        meta_keys = _collect_keys_from_trigger_conditions(
            props.get("trigger_conditions")
        )

        first_path_idx: Optional[int] = None
        for i, pk in enumerate(path_keys):
            if pk not in nodes:
                continue
            if first_path_idx is None:
                first_path_idx = i
            step_id = f"syn:path_step:{ck}:{i}"
            _ensure_synthetic_path_step_node(
                nodes, step_id, step_index=i, card_key=ck, bound_key=pk
            )
            add_edge(step_id, pk, "PATH_BIND", path_index=i)

        for i in range(len(path_keys) - 1):
            a, b = path_keys[i], path_keys[i + 1]
            if a not in nodes or b not in nodes:
                continue
            sa = f"syn:path_step:{ck}:{i}"
            sb = f"syn:path_step:{ck}:{i + 1}"
            _ensure_synthetic_path_step_node(
                nodes, sa, step_index=i, card_key=ck, bound_key=a
            )
            _ensure_synthetic_path_step_node(
                nodes, sb, step_index=i + 1, card_key=ck, bound_key=b
            )
            add_edge(sa, sb, "PATH_ORDER", leg="chain", step=i)

        if path_keys and first_path_idx is not None:
            add_edge(
                ck,
                f"syn:path_step:{ck}:{first_path_idx}",
                "CARD_ENTRY",
                role="path_head",
            )
        elif path_keys:
            p0 = path_keys[0]
            if p0 in nodes:
                add_edge(ck, p0, "CARD_ENTRY", role="path_head")

        for mk in meta_keys:
            if mk == ck:
                continue
            if mk in nodes:
                add_edge(ck, mk, "CARD_META", role="trigger_metadata")


def guess_physical_db_id(driver: Driver) -> Optional[str]:
    """从当前 Neo4j 逻辑库取一个 ``Table.db``，作为 Explorer 未显式指定时的默认物理库 id。"""
    with neo4j_session(driver) as s:
        row = s.run(
            """
            MATCH (t:Table)
            WHERE coalesce(t.db, '') <> ''
            RETURN DISTINCT t.db AS db
            ORDER BY db ASC
            LIMIT 1
            """
        ).single()
    if not row or not row.get("db"):
        return None
    return str(row["db"])


def subgraph_snapshot_from_keys(
    driver: Driver,
    keys: list[str],
    *,
    max_edges: int = 200,
    max_nodes: int = 80,
) -> dict[str, Any]:
    """按节点 ``key`` 列表拉取诱导子图（节点 + 这些节点之间的边），结构与 ``expand_subgraph`` 一致。"""
    ordered = list(dict.fromkeys([k for k in keys if k]))[:max_nodes]
    if not ordered:
        return {"center": None, "nodes": [], "edges": [], "raw": {}}

    nodes: dict[str, dict] = {}
    edges_out: list[dict] = []
    seen_edge: set[tuple[str, str, str]] = set()

    def take_node(raw: Any, la: str) -> Optional[str]:
        if raw is None:
            return None
        props = dict(raw)
        key = props.get("key")
        if not key:
            return None
        group = la or "Node"
        if key not in nodes:
            nodes[key] = {
                "id": key,
                "label": _node_label(props, group),
                "group": group,
                "props": _trim_props(props),
            }
        return key

    extra_fetch: list[str] = []
    with neo4j_session(driver) as s:
        for row in s.run(
            "MATCH (n) WHERE n.key IN $keys RETURN n, labels(n)[0] AS la",
            keys=ordered,
        ):
            take_node(row.get("n"), row.get("la") or "Node")

        extra_fetch = _strategy_card_extra_fetch_keys(nodes, max_total_nodes=max_nodes)
        if extra_fetch:
            for row in s.run(
                "MATCH (n) WHERE n.key IN $keys RETURN n, labels(n)[0] AS la",
                keys=extra_fetch,
            ):
                take_node(row.get("n"), row.get("la") or "Node")

        all_keys = list(nodes.keys())
        q_rel = """
        MATCH (a)-[r]-(b)
        WHERE a.key IN $keys AND b.key IN $keys
        RETURN a, labels(a)[0] AS la, b, labels(b)[0] AS lb,
               type(r) AS rt, properties(r) AS rp
        LIMIT $lim
        """
        for row in s.run(q_rel, keys=all_keys, lim=int(max_edges)):
            ka = take_node(row.get("a"), row.get("la") or "Node")
            kb = take_node(row.get("b"), row.get("lb") or "Node")
            rt = row.get("rt") or "REL"
            if not ka or not kb:
                continue
            sig = tuple(sorted([ka, kb]) + [rt])
            if sig in seen_edge:
                continue
            seen_edge.add(sig)
            edges_out.append(
                {
                    "from": ka,
                    "to": kb,
                    "type": rt,
                    "props": _trim_props(dict(row.get("rp") or {})),
                }
            )

    _append_strategy_card_synthetic_edges(nodes, edges_out)

    center = ordered[0] if ordered else None
    # 会把 Strategy 的 key 插在列表前部便于进快照上限；画布中心仍优先对准业务节点
    if ordered and any(str(k).startswith("card:") for k in ordered):
        for k in ordered:
            if k and not str(k).startswith("card:"):
                center = k
                break
    return {
        "center": center,
        "nodes": list(nodes.values()),
        "edges": edges_out,
        "raw": {
            "keys_requested": len(keys),
            "keys_used": len(nodes),
            "extra_fetch": len(extra_fetch),
        },
    }


# ---------------------------------------------------------------------- #
# 全局 / 局部 图可视化 (前端 "全局图" 浏览模式专用)
# ---------------------------------------------------------------------- #
# 设计原则：**永远不返回完整���。**
#
# - ``global_graph_snapshot``：默认 ``domain_roots_only=True``，只返回
#   ``Domain`` 节点（无边），作为"骨架/导航起点"。前端再通过 click /
#   double-click 触发 :func:`expand_node_snapshot` 展开全邻域。
# - ``domain_graph_snapshot``：单个 Domain 的 ``HAS_METRIC`` 骨架，给
#   "我先想看 ChatApp 域的所有 Metric" 这种用法。
# - ``expand_node_layer``：按方向（``down`` 出边 / ``up`` 入边）拉取一层邻居。
# - ``expand_node_snapshot``：单节点全部邻居（不区分方向）；前端的"双击节点"对接它。
#
# 共同特点：
# - 都加 ``max_edges`` / ``max_nodes`` 上限，防止 ``Column`` /
#   ``DimensionValue`` 这种密集节点把画布撑爆。
# - 默认排除 ``zone='trace'`` 与 ``zone='knowledge'`` 节点（``Task / Step / … / Event``）；可以按需打开。
# - 返回结构与 :func:`expand_subgraph` 完全一致：
#   ``{nodes, edges, raw, center?}``，前端 ``applyGraphDelta`` 直接消费。
# ---------------------------------------------------------------------- #

# 列级 / join 级的高密度边——全局骨架默认排除，避免画布炸开
_HIGH_DENSITY_EDGES = (
    "HAS_COLUMN",
    "USES_COLUMN",
    "MAPS_TO_COLUMN",
    "JOINS_ON",
    "FILTER_ON",
    "HAS_VALUE",
)

# 默认对全局视图隐藏的 zone（trace / knowledge 节点）
_NON_METADATA_ZONES = ("trace", "knowledge")


# 图层「知识 / 轨迹」：按 Neo4j **节点标签** 全库枚举（与 props.zone 独立，避免仅靠 zone 漏点）
# Claim 属于轨迹层（ToolCall PRODUCES、zone=trace），不进知识图层枚举
_KNOWLEDGE_LAYER_LABELS = (
    "Entity",
    "Event",
)
_TRACE_LAYER_LABELS = (
    "Task",
    "Step",
    "ToolCall",
    "Experience",
    "Strategy",
    "Turn",
    "Claim",
    "Session",
    "User",
)

# 图层「元数据」：语义层 + 物理层结点类型（与 trace/knowledge 并列的全库枚举）
_METADATA_LAYER_LABELS = (
    "Database",
    "Schema",
    "Table",
    "Column",
    "Domain",
    "Metric",
    "Formula",
    "Dimension",
    "DimensionValue",
    "Caliber",
    "Dataset",
)


def _primary_group_for_layer_snapshot(node: Any, preferred: tuple[str, ...]) -> str:
    """画布 ``group``：优先命中图层枚举标签中的第一个，否则用 Neo4j 的首标签。"""
    try:
        nl = tuple(getattr(node, "labels", ()) or ())
    except Exception:
        nl = ()
    for lab in preferred:
        if lab in nl:
            return lab
    return str(nl[0]) if nl else "Node"


def _snapshot_by_neo4j_labels(
    session,
    *,
    labels: tuple[str, ...],
    add_node,
    add_edge,
    max_nodes: int,
    max_edges: int,
) -> dict[str, int]:
    """匹配任一给定标签的结点（全库 ``MATCH``，按 key/id 排序截断）。

    关系：**两端**都必须命中图层标签集合（保证画布上两端都能渲染），
    端点不受 ``max_nodes`` 预算限制——它们已通过标签过滤，必然相关。
    跨界边（如 ``Entity -[:SURFACE_METRIC]-> Metric``）仅在两端标签
    都在 ``labels`` 中时才会出现；``zone_mode=all`` 合并了三层标签，
    因此跨界边在"全部"视图中正常展示。
    """
    labs = list(labels)
    labs_t = tuple(labels)
    per_group: dict[str, int] = {}
    rows = session.run(
        """
        MATCH (n)
        WHERE any(l IN labels(n) WHERE l IN $labs)
        WITH n, [l IN labels(n) WHERE l IN $labs][0] AS grp
        ORDER BY coalesce(n.key, toString(id(n)))
        LIMIT $cap
        RETURN n AS node, grp AS grp
        """,
        labs=labs,
        cap=int(max_nodes),
    ).data()
    for r in rows:
        grp = str(r.get("grp") or "Node")
        k = add_node(r["node"], grp)
        if k:
            per_group[grp] = per_group.get(grp, 0) + 1
    if max_edges > 0:
        erows = session.run(
            """
            MATCH (a)-[rel]->(b)
            WHERE any(la IN labels(a) WHERE la IN $labs)
              AND any(lb IN labels(b) WHERE lb IN $labs)
            RETURN properties(a) AS aprops, labels(a) AS alabs,
                   elementId(a) AS aeid,
                   properties(b) AS bprops, labels(b) AS blabs,
                   elementId(b) AS beid,
                   type(rel) AS rel,
                   properties(rel) AS rprops
            LIMIT $ecap
            """,
            labs=labs,
            ecap=int(max_edges),
        ).data()
        for r in erows:
            a_obj = _LightNode(r["aprops"], r["alabs"], r["aeid"])
            b_obj = _LightNode(r["bprops"], r["blabs"], r["beid"])
            la = _primary_group_for_layer_snapshot(a_obj, labs_t)
            lb = _primary_group_for_layer_snapshot(b_obj, labs_t)
            # 边端点不受 max_nodes 预算限制——已通过图层标签过滤
            ak = _add_node_unchecked(add_node, a_obj, la)
            bk = _add_node_unchecked(add_node, b_obj, lb)
            if ak and bk:
                add_edge(ak, bk, r["rel"], _trim_props(dict(r.get("rprops") or {})))
    return per_group


class _LightNode:
    """Neo4j Node 的轻量替代——边查询返回 properties / labels / elementId 而非完整 Node。"""

    __slots__ = ("_props", "labels", "element_id")

    def __init__(self, props: dict, labels: list[str], element_id: str):
        self._props = dict(props or {})
        self.labels = tuple(labels or ())
        self.element_id = element_id

    def __iter__(self):
        return iter(self._props)

    def __getitem__(self, key):
        return self._props[key]

    def get(self, key, default=None):
        return self._props.get(key, default)

    def keys(self):
        return self._props.keys()


def _add_node_unchecked(add_node, node, group: str):
    """确保边端点被加入画布，即使超出 ``max_nodes`` 预算。

    ``add_node`` 接受 ``force=True`` 时跳过预算检查；
    若不支持 ``force``（其他调用点的旧签名），退化为普通调用。
    """
    key = add_node(node, group)
    if key:
        return key
    try:
        return add_node(node, group, force=True)
    except TypeError:
        return None


def _all_layer_labels_union() -> tuple[str, ...]:
    """``zone_mode=all`` 时合并三类图层的结点标签（去重保序）。"""
    merged: dict[str, None] = {}
    for lab in _METADATA_LAYER_LABELS + _KNOWLEDGE_LAYER_LABELS + _TRACE_LAYER_LABELS:
        merged.setdefault(lab, None)
    return tuple(merged.keys())


def _zone_mode_allowed(zone_mode: str) -> Optional[tuple[str, ...]]:
    """图层工具栏：``all`` = 不限 zone；否则仅保留对应分区 + ``_shared``。"""
    zm = (zone_mode or "all").strip().lower()
    if zm in ("", "all"):
        return None
    if zm == "metadata":
        return ("metadata", "_shared")
    if zm == "trace":
        return ("trace", "_shared")
    if zm == "knowledge":
        return ("knowledge", "_shared")
    raise ValueError(
        f"zone_mode must be one of: all, metadata, trace, knowledge (got {zone_mode!r})"
    )


def _canvas_node_key(props: dict, group: str) -> Optional[str]:
    """前端画布节点 id；兼容 legacy ``GraphWriter`` 未写 ``key`` 的 :Table/:Column（仅有 db/name）。"""
    k = props.get("key")
    if k:
        return str(k)
    name = props.get("name")
    db = props.get("db")
    if group == "Database" and name:
        return f"db:{name}"
    if group == "Schema" and name:
        return f"schema:{name}"
    if group == "Domain" and name:
        return f"dom:{name}"
    if group == "Table" and db and name:
        sch = (props.get("schema") or "").strip() or "main"
        return f"tbl:{db}.{sch}.{name}"
    tbl = props.get("table")
    if group == "Column" and db and tbl and name:
        sch = (props.get("schema") or "").strip() or "main"
        return f"col:{db}.{sch}.{tbl}.{name}"
    return None


def _canvas_node_key_or_fallback(node: Any, group: str) -> Optional[str]:
    """画布 id：优先 ``props.key`` / 表名列名规则；否则用 Neo4j ``element_id`` / 旧版 internal id / ``name``。

    Event / KnowledgeChunk 等常未写 ``key``，仅用 :func:`_canvas_node_key` 会导致结点全部被丢弃。
    """
    try:
        props = dict(node)
    except Exception:
        props = {}
    key = _canvas_node_key(props, group)
    if key:
        return key
    eid = getattr(node, "element_id", None)
    if eid:
        return f"{group}:{eid}"
    nid = getattr(node, "id", None)
    if nid is not None:
        return f"{group}:{nid}"
    nm = props.get("name") or props.get("title") or props.get("chunk_id")
    if nm:
        return f"{group}:{nm}"
    return None


_LABEL_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _parse_element_id_canvas_key(node_key: str) -> Optional[tuple[str, str]]:
    """画布 fallback id ``<Neo4jLabel>:<elementId>``（无 ``key`` 时），供 ``MATCH … WHERE elementId(a)=…``。"""
    nk = (node_key or "").strip()
    if ":" not in nk:
        return None
    if any(nk.startswith(p) for p in _GRAPH_KEY_PREFIXES):
        return None
    label, _, eid = nk.partition(":")
    label, eid = label.strip(), eid.strip()
    if not label or not eid or not _LABEL_TOKEN_RE.match(label):
        return None
    return label, eid


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


def _explorer_match_score(
    q_lower: str,
    node_id: str,
    label: str,
    group: str,
    props: dict[str, Any],
) -> int:
    best = 0
    seen: set[str] = set()
    for raw in _explorer_collect_haystacks(node_id, label, group, props):
        h = str(raw).lower()
        if not h or h in seen:
            continue
        seen.add(h)
        if h == q_lower:
            best = max(best, 100)
        elif h.startswith(q_lower):
            best = max(best, 85)
        elif q_lower in h:
            best = max(best, 55)
    return best


def search_explorer_nodes(
    driver: Driver,
    query: str,
    *,
    limit: int = 25,
    fetch_cap: int = 500,
) -> list[dict[str, Any]]:
    """Explorer「找节点」：在当前 Neo4j 逻辑库全库子串匹配（非仅画布已加载子集）。

    用若干常用字符串属性做 ``CONTAINS`` 初筛，再按与前端一致的规则打分、去重、截断。
    """
    q = (query or "").strip()
    if not q:
        return []
    q_lower = q.lower()
    lim = max(1, min(int(limit), 100))
    cap = max(lim, min(int(fetch_cap), 2000))

    cypher = """
    MATCH (n)
    WHERE any(x IN [n.key, n.name, n.title, n.domain, n.table, n.schema, n.column_key, n.chunk_id]
              WHERE x IS NOT NULL AND toLower(toString(x)) CONTAINS $q)
    WITH n, head(labels(n)) AS la
    LIMIT $cap
    RETURN n, la
    """
    with neo4j_session(driver) as s:
        rows = s.run(cypher, q=q_lower, cap=cap).data()

    scored: dict[str, dict[str, Any]] = {}
    for row in rows:
        node = row.get("n")
        la = row.get("la") or "Node"
        if node is None:
            continue
        try:
            props = dict(node)
        except Exception:
            continue
        group = str(la)
        cid = _canvas_node_key_or_fallback(node, group)
        if not cid:
            continue
        label = _node_label(props, group)
        tprops = _trim_props(props)
        score = _explorer_match_score(q_lower, cid, label, group, tprops)
        if score <= 0:
            continue
        prev = scored.get(cid)
        if prev is None or score > int(prev["score"]):
            scored[cid] = {
                "id": cid,
                "label": label,
                "group": group,
                "props": tprops,
                "score": score,
            }

    out = sorted(scored.values(), key=lambda x: (-int(x["score"]), str(x["id"])))
    return out[:lim]


def search_explorer_subgraph(
    driver: Driver,
    query: str,
    *,
    allowed_labels: Optional[list[str] | tuple[str, ...] | set[str]] = None,
    label_zones: Optional[Mapping[str, str]] = None,
    match_mode: str = "fuzzy",
    hops: int = 1,
    limit: int = 50,
    max_nodes: int = 250,
    max_edges: int = 200,
) -> dict[str, list[dict[str, Any]]]:
    """Search Explorer nodes and return one bounded, scope-filtered traversal."""
    q = (query or "").strip()
    if not q:
        return {"hit_nodes": [], "nodes": [], "edges": []}

    restrict_labels = allowed_labels is not None
    labels: list[str] = []
    for raw_label in allowed_labels or ():
        label = str(raw_label).strip()
        if not _LABEL_TOKEN_RE.fullmatch(label):
            raise ValueError(f"Invalid Neo4j label: {raw_label!r}")
        if label not in labels:
            labels.append(label)
    labels.sort()

    mode = (match_mode or "fuzzy").strip().lower()
    if mode not in ("exact", "fuzzy"):
        raise ValueError("match_mode must be 'exact' or 'fuzzy'")
    hop_cap = max(1, min(int(hops), 3))
    seed_cap = max(1, min(int(limit), 200))
    node_cap = max(0, int(max_nodes))
    edge_cap = max(0, int(max_edges))
    if node_cap == 0 or (restrict_labels and not labels):
        return {"hit_nodes": [], "nodes": [], "edges": []}
    seed_cap = min(seed_cap, node_cap)

    if mode == "exact":
        match_filter = "(node.key = $search_query OR node.name = $search_query)"
    else:
        match_filter = """(
            toLower(coalesce(node.key, '')) CONTAINS $search_query_lower
            OR toLower(coalesce(node.name, '')) CONTAINS $search_query_lower
            OR toLower(coalesce(node.canonical_name, '')) CONTAINS $search_query_lower
            OR toLower(coalesce(node.description, '')) CONTAINS $search_query_lower
            OR any(alias IN coalesce(node.aliases, [])
                   WHERE toLower(toString(alias)) CONTAINS $search_query_lower)
        )"""

    hit_query = f"""
    MATCH (node)
    WHERE node.key IS NOT NULL
      AND {match_filter}
      AND (NOT $restrict_labels OR any(label IN labels(node) WHERE label IN $allowed_labels))
    WITH node,
         CASE WHEN $restrict_labels
              THEN head([label IN $allowed_labels WHERE label IN labels(node)])
              ELSE head(labels(node)) END AS label
    RETURN node.key AS key, label,
           coalesce(node.zone, '') AS zone,
           coalesce(node.name, node.canonical_name, node.goal, node.key) AS display_name
    ORDER BY toString(node.key), label
    LIMIT $limit
    """
    params = {
        "search_query": q,
        "search_query_lower": q.lower(),
        "restrict_labels": restrict_labels,
        "allowed_labels": labels,
        "limit": seed_cap,
    }
    with neo4j_session(driver) as session:
        hit_rows = session.run(hit_query, **params).data()

    zones = label_zones or {}

    def normalize_node(row: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        key = row.get("key")
        if key is None or not str(key):
            return None
        label = str(row.get("label") or "")
        if restrict_labels and label not in labels:
            return None
        return {
            "key": str(key),
            "label": label,
            "zone": row.get("zone") or zones.get(label, ""),
            "display_name": row.get("display_name", ""),
        }

    hits_by_key: dict[str, dict[str, Any]] = {}
    for row in hit_rows:
        node = normalize_node(row)
        if node is not None:
            hits_by_key.setdefault(node["key"], node)
    hit_nodes = sorted(hits_by_key.values(), key=lambda node: node["key"])
    if not hit_nodes:
        return {"hit_nodes": [], "nodes": [], "edges": []}

    nodes_by_key = dict(hits_by_key)
    edges_by_id: dict[tuple[str, str, str], dict[str, Any]] = {}
    path_limit = max(1, node_cap + edge_cap)
    traversal_query = f"""
    MATCH path = (hit)-[*1..{hop_cap}]-(neighbor)
    WHERE hit.key IN $hit_keys
      AND neighbor <> hit
      AND all(path_node IN nodes(path) WHERE path_node.key IS NOT NULL)
      AND (NOT $restrict_labels OR all(
            path_node IN nodes(path)
            WHERE any(label IN labels(path_node) WHERE label IN $allowed_labels)
          ))
    WITH path,
         [path_node IN nodes(path) | toString(path_node.key)] AS path_node_keys,
         [rel IN relationships(path) | type(rel)] AS path_rel_types
    ORDER BY length(path), path_node_keys, path_rel_types
    LIMIT $path_limit
    RETURN [path_node IN nodes(path) | {{
             key: path_node.key,
             label: CASE WHEN $restrict_labels
                         THEN head([label IN labels(path_node) WHERE label IN $allowed_labels])
                         ELSE head(labels(path_node)) END,
             zone: coalesce(path_node.zone, ''),
             display_name: coalesce(path_node.name, path_node.canonical_name,
                                    path_node.goal, path_node.key)
           }}] AS path_nodes,
           [rel IN relationships(path) | {{
             source_key: startNode(rel).key,
             target_key: endNode(rel).key,
             rel_type: type(rel)
           }}] AS path_edges
    """
    with neo4j_session(driver) as session:
        path_rows = session.run(
            traversal_query,
            hit_keys=[node["key"] for node in hit_nodes],
            restrict_labels=restrict_labels,
            allowed_labels=labels,
            path_limit=path_limit,
        ).data()

    for row in path_rows:
        path_nodes: dict[str, dict[str, Any]] = {}
        for raw_node in row.get("path_nodes") or []:
            node = normalize_node(raw_node)
            if node is not None:
                path_nodes.setdefault(node["key"], node)
        missing = [node for key, node in path_nodes.items() if key not in nodes_by_key]
        if len(nodes_by_key) + len(missing) > node_cap:
            continue
        for node in missing:
            nodes_by_key[node["key"]] = node
        for edge in row.get("path_edges") or []:
            edge_id = (
                str(edge.get("source_key") or ""),
                str(edge.get("target_key") or ""),
                str(edge.get("rel_type") or ""),
            )
            if not all(edge_id) or edge_id in edges_by_id:
                continue
            if edge_id[0] not in nodes_by_key or edge_id[1] not in nodes_by_key:
                continue
            if len(edges_by_id) >= edge_cap:
                break
            edges_by_id[edge_id] = {
                "source_key": edge_id[0],
                "target_key": edge_id[1],
                "rel_type": edge_id[2],
                "properties": {},
            }

    return {
        "hit_nodes": hit_nodes,
        "nodes": sorted(nodes_by_key.values(), key=lambda node: node["key"]),
        "edges": [edges_by_id[key] for key in sorted(edges_by_id)],
    }


_GLOBAL_GRAPH_SNAPSHOT_CACHE_TTL_SECONDS = 8.0
_GLOBAL_GRAPH_SNAPSHOT_CACHE_MAX_ENTRIES = 128
_GLOBAL_GRAPH_SNAPSHOT_CACHE_LOCK = threading.Lock()
_GLOBAL_GRAPH_SNAPSHOT_CACHE: OrderedDict[
    tuple[Any, ...], tuple[float, dict[str, Any]]
] = OrderedDict()
_GLOBAL_GRAPH_SNAPSHOT_CACHE_GENERATION = 0


def invalidate_global_graph_snapshot_cache() -> None:
    """Clear all cached global snapshots and invalidate in-flight fills."""
    global _GLOBAL_GRAPH_SNAPSHOT_CACHE_GENERATION
    with _GLOBAL_GRAPH_SNAPSHOT_CACHE_LOCK:
        _GLOBAL_GRAPH_SNAPSHOT_CACHE.clear()
        _GLOBAL_GRAPH_SNAPSHOT_CACHE_GENERATION += 1


def global_graph_snapshot(
    driver: Driver,
    *,
    max_edges: int = 800,
    max_nodes: int = 600,
    skeleton: bool = True,
    domain_roots_only: bool = True,
    task_roots: bool = False,
    max_task_roots: int = 10,
    zone_mode: str = "all",
) -> dict[str, Any]:
    """Return a cached global graph snapshot."""
    cache_key = (
        id(driver),
        neo4j_database_ctx.get() or CFG.neo4j_database,
        max_edges,
        max_nodes,
        skeleton,
        domain_roots_only,
        task_roots,
        max_task_roots,
        zone_mode,
    )
    now = time.monotonic()
    with _GLOBAL_GRAPH_SNAPSHOT_CACHE_LOCK:
        generation = _GLOBAL_GRAPH_SNAPSHOT_CACHE_GENERATION
        cached = _GLOBAL_GRAPH_SNAPSHOT_CACHE.get(cache_key)
        if cached is not None:
            stored_at, value = cached
            if now - stored_at < _GLOBAL_GRAPH_SNAPSHOT_CACHE_TTL_SECONDS:
                _GLOBAL_GRAPH_SNAPSHOT_CACHE.move_to_end(cache_key)
                return copy.deepcopy(value)
            del _GLOBAL_GRAPH_SNAPSHOT_CACHE[cache_key]

    value = _global_graph_snapshot_uncached(
        driver,
        max_edges=max_edges,
        max_nodes=max_nodes,
        skeleton=skeleton,
        domain_roots_only=domain_roots_only,
        task_roots=task_roots,
        max_task_roots=max_task_roots,
        zone_mode=zone_mode,
    )
    cached_value = copy.deepcopy(value)
    with _GLOBAL_GRAPH_SNAPSHOT_CACHE_LOCK:
        if generation == _GLOBAL_GRAPH_SNAPSHOT_CACHE_GENERATION:
            _GLOBAL_GRAPH_SNAPSHOT_CACHE[cache_key] = (time.monotonic(), cached_value)
            _GLOBAL_GRAPH_SNAPSHOT_CACHE.move_to_end(cache_key)
            while (
                len(_GLOBAL_GRAPH_SNAPSHOT_CACHE)
                > _GLOBAL_GRAPH_SNAPSHOT_CACHE_MAX_ENTRIES
            ):
                _GLOBAL_GRAPH_SNAPSHOT_CACHE.popitem(last=False)
    return value


def _global_graph_snapshot_uncached(
    driver: Driver,
    *,
    max_edges: int = 800,
    max_nodes: int = 600,
    skeleton: bool = True,
    domain_roots_only: bool = True,
    task_roots: bool = False,
    max_task_roots: int = 10,
    zone_mode: str = "all",
) -> dict[str, Any]:
    """全局图的"起步骨架"——给前端 "全局图" 按钮用。

    四种模式：

    - ``domain_roots_only=True``（默认）：只返回所有 ``Domain`` 节点，**无边**。
      前端把它们摆开，用户双击一个 Domain → 调 ``expand_node_layer``
      逐层下钻。这是 demo 里最推荐的入口。
    - ``domain_roots_only=False, skeleton=True``：按边采样，但排除
      ``HAS_COLUMN / USES_COLUMN / MAPS_TO_COLUMN / JOINS_ON / FILTER_ON``
      这些"列级"高密度边，得到一张语义层路线图。
    - ``domain_roots_only=False, skeleton=False``：按边采样，包含全部边。
      仅适合超小图调试用。
    - ``task_roots=True``：在前两种基础上额外追加 ``max_task_roots`` 个
      最近的 ``Task``（按 ``created_at desc`` 排），方便 demo 同时看到
      trace 区。

    ``zone_mode``（``all`` / ``metadata`` / ``trace`` / ``knowledge``）：
    在**图数据库全库**范围内选取结点（不是对前端已展开子集的二次过滤）。

    - ``knowledge`` / ``trace``：按 **Neo4j 节点标签** 全库枚举（见
      ``_KNOWLEDGE_LAYER_LABELS`` / ``_TRACE_LAYER_LABELS``），与 ``zone`` 属性独立，
      避免仅靠 zone 漏点。
    - ``metadata``：按 ``_METADATA_LAYER_LABELS`` 枚举语义层 + 物理层结点类型及跨界边。
    - ``all`` 且 ``domain_roots_only=False``：三类图层标签并集（
      :func:`_all_layer_labels_union`），枚举所有类型结点；``domain_roots_only=True``
      时仍保留仅 ``Domain`` 顶层的旧入口（便于 GET 默认行为）。
    """
    allowed = _zone_mode_allowed(zone_mode)
    az: list[str] = list(allowed) if allowed is not None else []
    zm = (zone_mode or "all").strip().lower()
    trace_layer = zm == "trace"
    knowledge_layer = zm == "knowledge"
    effective_task_roots = bool(task_roots or trace_layer)
    eff_task_cap = max(int(max_task_roots), 40) if trace_layer else int(max_task_roots)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(node, group: str, *, force: bool = False) -> Optional[str]:
        if node is None:
            return None
        props = dict(node)
        key = _canvas_node_key_or_fallback(node, group)
        if not key:
            return None
        if not props.get("key"):
            props["key"] = key
        if key not in nodes and (force or len(nodes) < max_nodes):
            nodes[key] = {
                "id": key,
                "label": _node_label(props, group),
                "group": group,
                "props": _trim_props(props),
            }
        return key if key in nodes else None

    def add_edge(
        src: Optional[str], dst: Optional[str], rel: str, props: Optional[dict] = None
    ) -> None:
        if not (src and dst):
            return
        if len(edges) >= max_edges:
            return
        edges.append({"from": src, "to": dst, "type": rel, "props": props or {}})

    counts: dict[str, int] = {}
    with neo4j_session(driver) as s:
        # 知识 / 轨迹图层：全库按 Neo4j 标签扫描（非当前子图、非仅靠 props.zone）
        if knowledge_layer:
            lg = _snapshot_by_neo4j_labels(
                s,
                labels=_KNOWLEDGE_LAYER_LABELS,
                add_node=add_node,
                add_edge=add_edge,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
            counts.update(lg)
            return {
                "center": None,
                "nodes": list(nodes.values()),
                "edges": edges,
                "raw": {
                    "zone_mode": zm,
                    "mode": "neo4j_labels_knowledge",
                    "neo4j_labels": list(_KNOWLEDGE_LAYER_LABELS),
                    "stats": {
                        "node_count": len(nodes),
                        "edge_count": len(edges),
                        **counts,
                    },
                },
            }

        if trace_layer:
            tg = _snapshot_by_neo4j_labels(
                s,
                labels=_TRACE_LAYER_LABELS,
                add_node=add_node,
                add_edge=add_edge,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
            counts.update(tg)
            return {
                "center": None,
                "nodes": list(nodes.values()),
                "edges": edges,
                "raw": {
                    "zone_mode": zm,
                    "mode": "neo4j_labels_trace",
                    "neo4j_labels": list(_TRACE_LAYER_LABELS),
                    "stats": {
                        "node_count": len(nodes),
                        "edge_count": len(edges),
                        **counts,
                    },
                },
            }

        if zm == "metadata":
            mg = _snapshot_by_neo4j_labels(
                s,
                labels=_METADATA_LAYER_LABELS,
                add_node=add_node,
                add_edge=add_edge,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
            counts.update(mg)
            return {
                "center": None,
                "nodes": list(nodes.values()),
                "edges": edges,
                "raw": {
                    "zone_mode": zm,
                    "mode": "neo4j_labels_metadata",
                    "neo4j_labels": list(_METADATA_LAYER_LABELS),
                    "stats": {
                        "node_count": len(nodes),
                        "edge_count": len(edges),
                        **counts,
                    },
                },
            }

        if zm == "all" and not domain_roots_only:
            all_labs = _all_layer_labels_union()
            ag = _snapshot_by_neo4j_labels(
                s,
                labels=all_labs,
                add_node=add_node,
                add_edge=add_edge,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
            counts.update(ag)
            return {
                "center": None,
                "nodes": list(nodes.values()),
                "edges": edges,
                "raw": {
                    "zone_mode": zm,
                    "mode": "neo4j_labels_all",
                    "neo4j_labels": list(all_labs),
                    "stats": {
                        "node_count": len(nodes),
                        "edge_count": len(edges),
                        **counts,
                    },
                },
            }

        # 1) Domain 节点（Default 等业务图有此层级，作为顶级骨架）
        for r in s.run(
            "MATCH (d:Domain) "
            "WHERE size($az) = 0 OR coalesce(d.zone, 'metadata') IN $az "
            "RETURN d AS dom, "
            "       size([(d)-[:HAS_METRIC]->(:Metric) | 1]) AS metric_count "
            "ORDER BY d.name",
            az=az,
        ).data():
            d_key = add_node(r["dom"], "Domain")
            if d_key:
                nodes[d_key]["props"]["metric_count"] = int(r.get("metric_count") or 0)
        counts["domain"] = sum(1 for n in nodes.values() if n["group"] == "Domain")

        # 1b) 兜底：无 Domain 的图改用 Database 节点作顶层入口
        if counts["domain"] == 0:
            # 无 Domain：Database -[:HAS_TABLE]-> Table
            # Default 物理层：Database -[:HAS_SCHEMA]-> Schema -[:HAS_TABLE]-> Table
            # （OF_VIEW→Dataset→CONTAINS_TABLE 是 Formula→Table 路径，不能用来数表）
            db_rows = s.run(
                "MATCH (db:Database) "
                "WHERE size($az) = 0 OR coalesce(db.zone, 'metadata') IN $az "
                "OPTIONAL MATCH (db)-[:HAS_TABLE]->(td:Table) "
                "WITH db, count(DISTINCT td) AS n_direct "
                "OPTIONAL MATCH (db)-[:HAS_SCHEMA]->(:Schema)-[:HAS_TABLE]->(ts:Table) "
                "WITH db, n_direct, count(DISTINCT ts) AS n_schema "
                "RETURN db AS db, db.name AS db_name, n_direct + n_schema AS table_count "
                "ORDER BY db.name "
                "LIMIT 200",
                az=az,
            ).data()
            for r in db_rows:
                props = dict(r["db"])
                # Database 节点没有 key，用 name 兜底合成
                db_name = props.get("name") or r.get("db_name") or ""
                if not db_name:
                    continue
                synth_key = props.get("key") or f"db:{db_name}"
                props.setdefault("key", synth_key)
                if synth_key not in nodes and len(nodes) < max_nodes:
                    nodes[synth_key] = {
                        "id": synth_key,
                        "label": db_name,
                        "group": "Database",
                        "props": {
                            **_trim_props(props),
                            "table_count": int(r.get("table_count") or 0),
                        },
                    }
            counts["database"] = sum(
                1 for n in nodes.values() if n["group"] == "Database"
            )

            # 如果仍然没有 Database，再试 Schema / Table 根节点
            if counts.get("database", 0) == 0:
                for r in s.run(
                    "MATCH (sc:Schema) "
                    "WHERE size($az) = 0 OR coalesce(sc.zone, 'metadata') IN $az "
                    "RETURN sc ORDER BY sc.name LIMIT 200",
                    az=az,
                ).data():
                    props = dict(r["sc"])
                    sc_name = props.get("name") or props.get("key") or ""
                    synth_key = props.get("key") or f"schema:{sc_name}"
                    props.setdefault("key", synth_key)
                    if synth_key not in nodes and len(nodes) < max_nodes:
                        nodes[synth_key] = {
                            "id": synth_key,
                            "label": sc_name,
                            "group": "Schema",
                            "props": _trim_props(props),
                        }
                counts["schema"] = sum(
                    1 for n in nodes.values() if n["group"] == "Schema"
                )

        # 2) 可选：最近的 N 个 Task（``all`` / ``metadata`` 浏览时追加 trace 入口）
        if effective_task_roots and eff_task_cap > 0:
            rows = s.run(
                "MATCH (t:Task) "
                "WHERE size($az) = 0 OR coalesce(t.zone, 'metadata') IN $az "
                "RETURN t AS task "
                "ORDER BY coalesce(t.created_at, datetime('1970-01-01T00:00:00Z')) DESC "
                "LIMIT $k",
                k=int(eff_task_cap),
                az=az,
            ).data()
            for r in rows:
                add_node(r["task"], "Task")
            counts["task"] = sum(1 for n in nodes.values() if n["group"] == "Task")

        # 3) 非 domain_roots_only：按边采样
        if not domain_roots_only:
            cypher_edges = """
            MATCH (a)-[r]->(b)
            WHERE (size($az) = 0 OR (
              coalesce(a.zone, 'metadata') IN $az AND coalesce(b.zone, 'metadata') IN $az
            ))
              AND ($skeleton = false OR NOT type(r) IN $skip_types)
            RETURN a, b, type(r) AS rel,
                   labels(a)[0] AS la, labels(b)[0] AS lb,
                   properties(r) AS rprops
            LIMIT $cap
            """
            rows = s.run(
                cypher_edges,
                az=az,
                skeleton=skeleton,
                skip_types=list(_HIGH_DENSITY_EDGES),
                cap=int(max_edges),
            ).data()
            for r in rows:
                a_key = add_node(r["a"], r.get("la") or "Node")
                b_key = add_node(r["b"], r.get("lb") or "Node")
                add_edge(
                    a_key, b_key, r["rel"], _trim_props(dict(r.get("rprops") or {}))
                )

    return {
        "center": None,
        "nodes": list(nodes.values()),
        "edges": edges,
        "raw": {
            "zone_mode": zm,
            "mode": "domain_roots_only"
            if domain_roots_only
            else ("skeleton" if skeleton else "full_sampled"),
            "stats": {
                "node_count": len(nodes),
                "edge_count": len(edges),
                **counts,
            },
        },
    }


def domain_graph_snapshot(
    driver: Driver,
    domain: str,
    *,
    max_nodes: int = 200,
) -> dict[str, Any]:
    """单个 Domain 的 ``HAS_METRIC`` 骨架——前端 "选域" 模式用。

    返回 ``Domain`` + 所有 Metric + 一条 ``HAS_METRIC`` 边 / Metric。
    超过 ``max_nodes-1`` 的 metric 按 ``name`` 字典序截断（保证可重现）。
    """
    if not (domain or "").strip():
        raise ValueError("domain is required")

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(node, group: str) -> Optional[str]:
        if node is None:
            return None
        props = dict(node)
        key = props.get("key")
        if not key:
            return None
        if key not in nodes and len(nodes) < max_nodes:
            nodes[key] = {
                "id": key,
                "label": _node_label(props, group),
                "group": group,
                "props": _trim_props(props),
            }
        return key if key in nodes else None

    cypher = """
    MATCH (d:Domain {name: $domain})
    OPTIONAL MATCH (d)-[:HAS_METRIC]->(m:Metric)
      WHERE (m.valid_to IS NULL OR m.valid_to > datetime())
    WITH d, m ORDER BY m.name
    LIMIT $cap
    RETURN d AS dom, collect(m) AS metrics
    """
    with neo4j_session(driver) as s:
        rec = s.run(cypher, domain=domain, cap=int(max_nodes)).single()
    if not rec or rec["dom"] is None:
        raise ValueError(f"Domain {domain!r} not found")

    dom_key = add_node(rec["dom"], "Domain")
    metrics = [m for m in (rec["metrics"] or []) if m is not None]
    for m in metrics:
        m_key = add_node(m, "Metric")
        if dom_key and m_key:
            edges.append(
                {"from": dom_key, "to": m_key, "type": "HAS_METRIC", "props": {}}
            )

    return {
        "center": dom_key,
        "nodes": list(nodes.values()),
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


def _zone_clause(exclude_trace_knowledge: bool) -> str:
    """生成 Cypher 片段：当 ``exclude_trace_knowledge=True`` 时过滤目标节点 zone。"""
    if not exclude_trace_knowledge:
        return ""
    return " AND NOT coalesce(nb.zone, 'metadata') IN " + repr(
        list(_NON_METADATA_ZONES)
    )


def expand_node_snapshot(
    driver: Driver,
    node_key: str,
    *,
    max_edges: int = 80,
    exclude_trace_knowledge: bool = False,
) -> dict[str, Any]:
    """单节点邻域（不区分方向）。

    Cypher：``MATCH (a {key:$k})--(nb)`` —— 把 ``a`` 的所有边都拉回。
    再追加节点本身（万一是孤立节点也能展示）。
    """
    if not (node_key or "").strip():
        raise ValueError("node_key is required")

    nodes: dict[str, dict] = {}
    edges_seen: set[tuple] = set()
    edges: list[dict] = []

    _SYNTH_PREFIXES_SNAP = {"db:": ("Database", "name"), "schema:": ("Schema", "name")}
    synth_label_snap: Optional[str] = None
    synth_name_snap: Optional[str] = None
    for prefix, (lbl, attr) in _SYNTH_PREFIXES_SNAP.items():
        if node_key.startswith(prefix):
            synth_label_snap = lbl
            synth_name_snap = node_key[len(prefix) :]
            break

    def add_node(node, group: str, force_key: Optional[str] = None) -> Optional[str]:
        if node is None:
            return None
        props = dict(node)
        key = force_key or _canvas_node_key(props, group)
        if not key:
            return None
        if key not in nodes:
            nodes[key] = {
                "id": key,
                "label": _node_label(props, group),
                "group": group,
                "props": _trim_props(props),
            }
        return key

    zone_filter = _zone_clause(exclude_trace_knowledge)
    rel_collect = (
        f"collect(DISTINCT {{r: r, nb: nb, "
        f"dir: CASE WHEN startNode(r) = a THEN 'out' ELSE 'in' END, "
        f"la: labels(a)[0], lb: labels(nb)[0], "
        f"rel: type(r), rprops: properties(r)}})[..{int(max_edges)}] AS rels"
    )
    optional_tail = f"""
        OPTIONAL MATCH (a)-[r]-(nb)
        WHERE nb IS NOT NULL{zone_filter}
        WITH a, {rel_collect}
        RETURN a, labels(a)[0] AS la, rels
        """
    matched_by_element_id = False
    if synth_label_snap:
        cypher = f"""
        MATCH (a:{synth_label_snap} {{name: $nm}})
        {optional_tail}
        """
        snap_params: dict = {"nm": synth_name_snap}
    else:
        cypher = f"""
        MATCH (a {{key: $k}})
        {optional_tail}
        """
        snap_params = {"k": node_key}
    with neo4j_session(driver) as s:
        rec = s.run(cypher, **snap_params).single()
        if (not rec or rec.get("a") is None) and not synth_label_snap:
            el = _parse_element_id_canvas_key(node_key)
            if el:
                lbl, eid = el
                cypher_el = f"""
                MATCH (a:{lbl})
                WHERE elementId(a) = $eid
                {optional_tail}
                """
                rec = s.run(cypher_el, eid=eid).single()
                if rec and rec.get("a") is not None:
                    matched_by_element_id = True
    if not rec or rec["a"] is None:
        raise ValueError(f"Node {node_key!r} not found")

    force_canvas = bool(synth_label_snap) or matched_by_element_id
    center_key = add_node(
        rec["a"], rec.get("la") or "Node", force_key=node_key if force_canvas else None
    )
    for row in rec["rels"] or []:
        if not row or row.get("nb") is None:
            continue
        nb_key = add_node(row["nb"], row.get("lb") or "Node")
        rel = row.get("rel")
        if not nb_key or not rel:
            continue
        if row.get("dir") == "out":
            src, dst = center_key, nb_key
        else:
            src, dst = nb_key, center_key
        sig = (src, dst, rel)
        if sig in edges_seen:
            continue
        edges_seen.add(sig)
        edges.append(
            {
                "from": src,
                "to": dst,
                "type": rel,
                "props": _trim_props(dict(row.get("rprops") or {})),
            }
        )

    return {
        "center": center_key,
        "nodes": list(nodes.values()),
        "edges": edges,
        "raw": {"stats": {"node_count": len(nodes), "edge_count": len(edges)}},
    }


def expand_node_layer(
    driver: Driver,
    node_key: str,
    *,
    direction: str = "down",
    max_edges: int = 80,
    fallback_neighbors: bool = True,
    exclude_trace_knowledge: bool = False,
) -> dict[str, Any]:
    """按"方向"展开一层邻居（可选 API；Topology UI 双击改用 :func:`expand_node_snapshot`）。

    - ``direction='down'``：只走出边 ``(a)-[r]->(nb)``。语义层里
      ``Domain --HAS_METRIC--> Metric --HAS_FORMULA--> Formula
      --OF_VIEW--> Dataset --CONTAINS_TABLE--> Table`` 全是出边方向，于是"下钻"等价于沿出边。
    - ``direction='up'``：只走入边 ``(a)<-[r]-(nb)``。
    - ``fallback_neighbors=True`` 且指定方向无邻居时，自动回退到
      :func:`expand_node_snapshot` 的全邻域，避免叶子节点点了没反应。

    返回与 :func:`expand_node_snapshot` 同形。
    """
    if not (node_key or "").strip():
        raise ValueError("node_key is required")
    direction = (direction or "down").lower()
    if direction not in ("down", "up"):
        raise ValueError(f"direction must be 'down' or 'up', got {direction!r}")

    arrow = "(a)-[r]->(nb)" if direction == "down" else "(a)<-[r]-(nb)"
    zone_filter = _zone_clause(exclude_trace_knowledge)

    # 合成 key（如 "db:california_schools"）→ 用 name 匹配 Database / Schema 节点
    _SYNTH_PREFIXES = {"db:": ("Database", "name"), "schema:": ("Schema", "name")}
    synth_label: Optional[str] = None
    synth_name: Optional[str] = None
    for prefix, (lbl, attr) in _SYNTH_PREFIXES.items():
        if node_key.startswith(prefix):
            synth_label = lbl
            synth_name = node_key[len(prefix) :]
            break

    if synth_label:
        cypher = f"""
        MATCH (a:{synth_label} {{name: $nm}})
        OPTIONAL MATCH {arrow}
        WHERE nb IS NOT NULL{zone_filter}
        WITH a, collect(DISTINCT {{r: r, nb: nb,
                                  la: labels(a)[0], lb: labels(nb)[0],
                                  rel: type(r), rprops: properties(r)}})[..{int(max_edges)}] AS rels
        RETURN a, labels(a)[0] AS la, rels
        """
        cypher_params: dict = {"nm": synth_name}
    else:
        cypher = f"""
        MATCH (a {{key: $k}})
        OPTIONAL MATCH {arrow}
        WHERE nb IS NOT NULL{zone_filter}
        WITH a, collect(DISTINCT {{r: r, nb: nb,
                                  la: labels(a)[0], lb: labels(nb)[0],
                                  rel: type(r), rprops: properties(r)}})[..{int(max_edges)}] AS rels
        RETURN a, labels(a)[0] AS la, rels
        """
        cypher_params = {"k": node_key}

    nodes: dict[str, dict] = {}
    edges_seen: set[tuple] = set()
    edges: list[dict] = []

    def add_node(node, group: str, force_key: Optional[str] = None) -> Optional[str]:
        if node is None:
            return None
        props = dict(node)
        key = force_key or _canvas_node_key(props, group)
        if not key:
            return None
        if key not in nodes:
            nodes[key] = {
                "id": key,
                "label": _node_label(props, group),
                "group": group,
                "props": _trim_props(props),
            }
        return key

    matched_by_element_id_layer = False
    with neo4j_session(driver) as s:
        rec = s.run(cypher, **cypher_params).single()
        if (not rec or rec.get("a") is None) and not synth_label:
            el = _parse_element_id_canvas_key(node_key)
            if el:
                lbl, eid = el
                cypher_el = f"""
                MATCH (a:{lbl})
                WHERE elementId(a) = $eid
                OPTIONAL MATCH {arrow}
                WHERE nb IS NOT NULL{zone_filter}
                WITH a, collect(DISTINCT {{r: r, nb: nb,
                                          la: labels(a)[0], lb: labels(nb)[0],
                                          rel: type(r), rprops: properties(r)}})[..{int(max_edges)}] AS rels
                RETURN a, labels(a)[0] AS la, rels
                """
                rec = s.run(cypher_el, eid=eid).single()
                if rec and rec.get("a") is not None:
                    matched_by_element_id_layer = True
    if not rec or rec["a"] is None:
        raise ValueError(f"Node {node_key!r} not found")

    layer_force = bool(synth_label) or matched_by_element_id_layer
    center_key = add_node(
        rec["a"], rec.get("la") or "Node", force_key=node_key if layer_force else None
    )
    rels = rec["rels"] or []

    if not rels and fallback_neighbors:
        # 该方向无邻居 → 退回到全邻域；这种情况通常是叶子（Column）或
        # 反向只走 _shared 节点（Domain）
        return expand_node_snapshot(
            driver,
            node_key,
            max_edges=max_edges,
            exclude_trace_knowledge=exclude_trace_knowledge,
        )

    for row in rels:
        if not row or row.get("nb") is None:
            continue
        nb_key = add_node(row["nb"], row.get("lb") or "Node")
        rel = row.get("rel")
        if not nb_key or not rel:
            continue
        if direction == "down":
            src, dst = center_key, nb_key
        else:
            src, dst = nb_key, center_key
        sig = (src, dst, rel)
        if sig in edges_seen:
            continue
        edges_seen.add(sig)
        edges.append(
            {
                "from": src,
                "to": dst,
                "type": rel,
                "props": _trim_props(dict(row.get("rprops") or {})),
            }
        )

    return {
        "center": center_key,
        "nodes": list(nodes.values()),
        "edges": edges,
        "raw": {
            "direction": direction,
            "stats": {"node_count": len(nodes), "edge_count": len(edges)},
        },
    }


def _json_safe_topology_val(x: Any) -> Any:
    """Neo4j temporal / nested structures → JSON-serializable."""
    if x is None:
        return None
    if isinstance(x, (bool, int, float, str)):
        return x
    if isinstance(x, dict):
        return {str(k): _json_safe_topology_val(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe_topology_val(v) for v in x]
    iso_fmt = getattr(x, "iso_format", None)
    if callable(iso_fmt):
        try:
            return iso_fmt()
        except Exception:
            pass
    if hasattr(x, "isoformat"):
        try:
            return x.isoformat()
        except Exception:
            pass
    return str(x)


def _sanitize_strategy_card_row(row: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "key",
        "task_type",
        "polarity",
        "composite_score",
        "cos_sim",
        "hit_count",
        "success_rate",
        "memory_tier",
        "source_trust",
        "sql_template",
        "strategy_semantics",
        "path_subgraph_keys",
        "trigger_conditions",
        "valid_to_is_null",
        "graph_db_id",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k not in row:
            continue
        out[k] = _json_safe_topology_val(row[k])
    st = out.get("sql_template") or ""
    if isinstance(st, str) and len(st) > 1200:
        out["sql_template"] = st[:1200] + "\n-- …"
    return out


def topology_insights_for_query(
    driver: Driver,
    query: str,
    *,
    k_cards: int = 6,
    db_id: str = "",
) -> dict[str, Any]:
    """Topology Explorer：锚点 + 规则任务类型 + Strategy ANN（不调 Decision LLM）。

    用于可视化界面「拓扑洞察」面板；依赖 ``strategy_vec`` 与 embedding 服务召回经验卡。
    ``db_id`` 与流水线 ``Example.db_id`` 一致时，仅召回该物理图库下的策略卡。
    """
    out: dict[str, Any] = {
        "anchor_time_hints": [],
        "decision_task_type_estimate": None,
        "trace_anchors": [],
        "strategy_cards": [],
        "strategy_card_gate": None,
        "trace_error": None,
    }
    q = (query or "").strip()
    if not q:
        return out

    from ..runtime.decision_llm import estimate_task_type
    from ..runtime.anchors import resolve_anchors
    from ..graph.strategy_card import StrategyCardRetriever

    tt = estimate_task_type(q)
    out["decision_task_type_estimate"] = tt

    gdb = (db_id or "").strip()

    try:
        aset = resolve_anchors(
            driver,
            q,
            db_id=gdb,
            embedder=embed_one,
            knowledge_score_scale=CFG.recall_anchor_knowledge_score_scale,
        )
        out["anchor_time_hints"] = list(aset.time_hints)
        out["trace_anchors"] = [
            {
                "key": a.key,
                "label": a.label,
                "name": a.name,
                "score": round(float(a.score), 4),
                "source": a.source,
            }
            for a in sorted(aset.anchors, key=lambda x: -x.score)[:24]
        ]
    except Exception as exc:
        log.warning("topology_insights anchors failed: %s", exc)
        out["trace_error"] = f"anchors: {exc}"

    emb: Optional[list[float]] = None
    try:
        emb = embed_one(q)
    except Exception as exc:
        log.debug("topology_insights embed failed: %s", exc)
        if not out["trace_error"]:
            out["trace_error"] = f"embed: {exc}"

    if emb:
        try:
            retriever = StrategyCardRetriever(driver)
            cards = retriever.recall_top_k(
                emb,
                task_type=None,
                graph_db_id=gdb,
                k=k_cards,
                allow_avoid=True,
            )
            out["strategy_cards"] = [
                _sanitize_strategy_card_row(dict(c)) for c in cards
            ]
            dec = retriever.top_card_decision(cards)
            out["strategy_card_gate"] = {
                "auto_accept": bool(dec.get("auto_accept")),
                "top_card_key": (dec.get("top_card") or {}).get("key"),
                "avoid_count": len(dec.get("avoid_cards") or []),
            }
        except Exception as exc:
            log.warning("topology_insights cards failed: %s", exc)
            msg = f"strategy_cards: {exc}"
            prev = out["trace_error"]
            out["trace_error"] = f"{prev}; {msg}" if prev else msg

    return out


def recall_strategy_cards(
    driver: Driver,
    query_emb: list[float],
    *,
    task_type: Optional[str] = None,
    graph_db_id: str = "",
    k: int = 5,
    allow_avoid: bool = True,
) -> list[dict]:
    """Strategy ANN 检索薄封装（runtime / §3.4）。

    Args:
        driver:     Neo4j driver.
        query_emb:  Query embedding vector.
        task_type:  Optional pre-estimated task type for first-level filtering.
        k:          Number of candidates to return.
        allow_avoid: Whether to include polarity='avoid' cards (for negative_hints).

    Returns:
        List of card dicts sorted by composite_score DESC.
        Each dict includes: key, task_type, polarity, path_subgraph_keys,
        sql_template, hit_count, success_rate, memory_tier, composite_score,
        trigger_conditions.
    """
    from ..graph.strategy_card import (
        StrategyCardRetriever,
    )  # avoid circular at module level

    retriever = StrategyCardRetriever(driver)
    candidates = retriever.recall_top_k(
        query_emb,
        task_type=task_type,
        graph_db_id=graph_db_id,
        k=k,
        allow_avoid=allow_avoid,
    )
    return candidates


# ═══════════════════════════════════════════════════════════════════════ #
#  Phase 2 helpers — node edges, experiences, tags
# ═══════════════════════════════════════════════════════════════════════ #

_STRIP_KEYS_RETR = frozenset(
    {
        "embedding",
        "embedding_hash",
        "signature_emb",
        "query_emb",
        "query_embedding",
        "strategy_vec",
    }
)


def _to_jsonable_retr(obj):
    """Lightweight serialiser for Neo4j temporal types."""
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _to_jsonable_retr(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable_retr(x) for x in obj]
    iso = getattr(obj, "isoformat", None) or getattr(obj, "iso_format", None)
    if callable(iso):
        try:
            return iso()
        except Exception:
            pass
    return str(obj)


def list_node_edges(
    driver: Driver,
    key: str,
    *,
    direction: str = "both",
    rel_type: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """List edges connected to a node, with optional direction/rel_type filter + pagination."""
    k = (key or "").strip()
    if not k:
        return [], 0

    dir_lower = (direction or "both").lower()
    if dir_lower == "out":
        pattern = "(n {key: $k})-[r]->(m)"
        dir_val = "'out'"
    elif dir_lower == "in":
        pattern = "(n {key: $k})<-[r]-(m)"
        dir_val = "'in'"
    else:
        pattern = "(n {key: $k})-[r]-(m)"
        dir_val = "CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END"

    rt_filter = " AND type(r) = $rt" if rel_type else ""
    params: dict = {"k": k}
    if rel_type:
        params["rt"] = rel_type

    count_cypher = f"""
    MATCH {pattern}
    WHERE m IS NOT NULL{rt_filter}
    RETURN count(r) AS total
    """
    skip = (page - 1) * page_size
    params["skip"] = skip
    params["limit"] = page_size
    data_cypher = f"""
    MATCH {pattern}
    WHERE m IS NOT NULL{rt_filter}
    RETURN type(r) AS rel_type,
           {dir_val} AS direction,
           CASE WHEN startNode(r) = n THEN n.key ELSE m.key END AS source_key,
           CASE WHEN startNode(r) = n THEN m.key ELSE n.key END AS target_key,
           head(labels(m)) AS target_label,
           coalesce(m.name, m.canonical_name, m.goal, m.key) AS target_display,
           properties(r) AS properties
    SKIP $skip LIMIT $limit
    """
    with neo4j_session(driver) as s:
        total_rec = s.run(
            count_cypher,
            **{kk: vv for kk, vv in params.items() if kk not in ("skip", "limit")},
        ).single()
        total = int(total_rec["total"]) if total_rec else 0
        rows = s.run(data_cypher, **params).data()

    edges = []
    for row in rows:
        rp = row.get("properties") or {}
        rp = {
            kk: vv
            for kk, vv in (rp if isinstance(rp, dict) else {}).items()
            if kk not in _STRIP_KEYS_RETR
        }
        edges.append(
            {
                "rel_type": row.get("rel_type", ""),
                "direction": row.get("direction", ""),
                "source_key": row.get("source_key", ""),
                "target_key": row.get("target_key", ""),
                "target_label": row.get("target_label", ""),
                "target_display": row.get("target_display", ""),
                "properties": _to_jsonable_retr(rp),
            }
        )
    return edges, total


def list_experiences(
    driver: Driver,
    *,
    task_signature: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """Paginated Experience listing with task_signature and keyword filters."""
    conditions = ["1=1"]
    params: dict = {}
    if task_signature:
        conditions.append("e.task_signature = $task_signature")
        params["task_signature"] = task_signature
    if q:
        conditions.append("toLower(coalesce(e.key_insight, '')) CONTAINS $q")
        params["q"] = q.lower()
    where = " AND ".join(conditions)
    skip = (page - 1) * page_size
    params["skip"] = skip
    params["limit"] = page_size

    count_cypher = f"MATCH (e:Experience) WHERE {where} RETURN count(e) AS total"
    data_cypher = f"""
    MATCH (e:Experience)
    WHERE {where}
    RETURN e.key AS key,
           coalesce(e.task_signature, '') AS task_signature,
           coalesce(e.outcome, '') AS outcome,
           coalesce(e.key_insight, '') AS key_insight,
           coalesce(e.applicable_scope, '') AS applicable_scope,
           toString(e.created_at) AS created_at
    ORDER BY e.created_at DESC
    SKIP $skip LIMIT $limit
    """
    with neo4j_session(driver) as s:
        total_rec = s.run(
            count_cypher,
            **{k: v for k, v in params.items() if k not in ("skip", "limit")},
        ).single()
        total = int(total_rec["total"]) if total_rec else 0
        rows = s.run(data_cypher, **params).data()
    return rows, total


def get_experience_detail(driver: Driver, key: str) -> dict | None:
    """Return an Experience's full properties and its derived-from Tasks."""
    with neo4j_session(driver) as s:
        rec = s.run(
            """
            MATCH (e:Experience {key: $key})
            OPTIONAL MATCH (e)-[:DERIVED_FROM]->(t:Task)
            RETURN properties(e) AS props,
                   collect({key: t.key, goal: t.goal, status: t.status}) AS derived_from_tasks
            """,
            key=key,
        ).single()
    if not rec:
        return None
    props = dict(rec["props"] or {})
    for k in _STRIP_KEYS_RETR:
        props.pop(k, None)
    tasks = [t for t in (rec["derived_from_tasks"] or []) if t.get("key")]
    return {"experience": _to_jsonable_retr(props), "derived_from_tasks": tasks}


def list_tags(
    driver: Driver,
    *,
    category: str | None = None,
    q: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict], int]:
    """Paginated Tag listing with per-tag usage count from TAGGED edges."""
    conditions = ["1=1"]
    params: dict = {}
    if category:
        conditions.append("tag.category = $category")
        params["category"] = category
    if q:
        conditions.append("toLower(coalesce(tag.name, '')) CONTAINS $q")
        params["q"] = q.lower()
    where = " AND ".join(conditions)
    skip = (page - 1) * page_size
    params["skip"] = skip
    params["limit"] = page_size

    count_cypher = f"MATCH (tag:Tag) WHERE {where} RETURN count(tag) AS total"
    data_cypher = f"""
    MATCH (tag:Tag)
    WHERE {where}
    OPTIONAL MATCH (tag)<-[:TAGGED]-(t:Task)
    WITH tag, count(t) AS tagged_task_count
    RETURN tag.key AS key,
           coalesce(tag.name, '') AS name,
           coalesce(tag.category, '') AS category,
           tagged_task_count
    ORDER BY tag.name
    SKIP $skip LIMIT $limit
    """
    with neo4j_session(driver) as s:
        total_rec = s.run(
            count_cypher,
            **{k: v for k, v in params.items() if k not in ("skip", "limit")},
        ).single()
        total = int(total_rec["total"]) if total_rec else 0
        rows = s.run(data_cypher, **params).data()
    return rows, total


__all__ = [
    "detect_dimensions",
    "domain_graph_snapshot",
    "expand_node_layer",
    "expand_node_snapshot",
    "expand_subgraph",
    "global_graph_snapshot",
    "invalidate_global_graph_snapshot_cache",
    "list_domains",
    "recall_strategy_cards",
    "resolve_metric",
    "search_events",
    "search_explorer_subgraph",
    "subgraph_snapshot_from_keys",
    "guess_physical_db_id",
    "topology_insights_for_query",
    "list_node_edges",
    "list_experiences",
    "get_experience_detail",
    "list_tags",
]

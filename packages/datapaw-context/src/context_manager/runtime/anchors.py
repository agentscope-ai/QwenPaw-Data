"""Step 1: Entry anchor resolution for the topology pipeline (v4 图).

AnchorSet = set of graph node keys that serve as entry points for typed traversal.
Sources:
  1. **Schema 分标签检索**：Metric、Dimension、Column 各自 fulltext+向量 RRF（互不混排）。
  2. **知识库**：Event / Entity 全文索引仅 ``description``（精确释义匹配）；若有 embedding 与 ``ev_vec``/``ent_vec``，
     则与 Metric 一样「全文 + 向量 → RRF」按标签融合后再池化去重；可选 ``knowledge_score_scale`` 作用于融合分。
  3. Entity → SURFACE_METRIC → Metric expansion（概念 Entity 命中则展开挂接的 surface Metric 并入 Metric 桶）
  4. Current date only（→ Task context，非图结点；不问句做日期规则抽取）

``AnchorSet.anchors`` 仍为各桶合并去重后的全集，供遍历与候选边收集；分桶字段供 Decision LLM 分段展示。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

from neo4j import Driver

from ..rrf import rrf_merge
from ..utils import get_logger, neo4j_session

log = get_logger("runtime.anchors")


# ---------------------------------------------------------------------- #
# Data structures
# ---------------------------------------------------------------------- #

@dataclass
class AnchorNode:
    key: str
    label: str   # Metric | Dimension | Column | Event | Entity
    name: str = ""
    score: float = 1.0
    # Raw vector cosine (0–1) from the per-label ANN search, preserved alongside
    # ``score`` (RRF rank-fusion). RRF is rank-only and loses absolute similarity,
    # so the relevance gate uses ``vec_score`` — not ``score`` — as the dense signal.
    vec_score: float = 0.0
    source: str = "rule"  # rrf | rule (fusion of fulltext + vector per label)
    # Enriched by `_enrich_anchor_descriptions` so the Decision LLM can
    # identify anchors by *meaning* (e.g. ``met:Studio:active_usercnt_1d`` →
    # description "当日活跃用户数-dau" lets the LLM realize this is a DAU metric).
    description: str = ""
    aliases: list[str] = field(default_factory=list)
    # Semantic precision-rerank score (0–1) written by ``runtime.rerank`` when the
    # rerank stage runs. ``-1.0`` means "not reranked". ``api.semantic_pack.score_anchor``
    # blends this in so the reranker's meaning-based judgment actually drives L1
    # ordering — the text+vec blend alone can't separate near-tied niche vs.
    # canonical metrics (e.g. ``分享页访问用户数`` vs ``当日访问用户数``).
    rerank_score: float = -1.0
    # Business domain (e.g. "ChatApp", "ImageGen"). Populated from the graph node's
    # ``domain`` property during fulltext/vector anchor search. Used by
    # ``_domain_soft_filter`` and ``score_anchor`` to prefer same-domain anchors
    # and penalise cross-domain pollution.
    domain: str = ""


@dataclass
class AnchorSet:
    anchors: list[AnchorNode] = field(default_factory=list)
    time_hints: list[str] = field(default_factory=list)  # e.g. ["today=20260509"]
    question: str = ""
    db_id: str = ""
    # Schema 与知识库分桶；Metric 桶含 Entity→SURFACE_METRIC 展开的 surface metrics
    anchors_metric: list[AnchorNode] = field(default_factory=list)
    anchors_dimension: list[AnchorNode] = field(default_factory=list)
    anchors_column: list[AnchorNode] = field(default_factory=list)
    # Event / Entity 合并列表（v4 无 Policy 全文桶）
    anchors_knowledge: list[AnchorNode] = field(default_factory=list)
    # Trace Claims — 蒸馏出的经验断言，可被向量/全文召回
    anchors_claim: list[AnchorNode] = field(default_factory=list)

    def label_types(self) -> list[str]:
        return sorted({a.label for a in self.anchors})

    def keys_by_label(self, label: str) -> list[str]:
        return [a.key for a in self.anchors if a.label == label]

    def top_keys(self, n: int = 5) -> list[str]:
        sorted_anchors = sorted(self.anchors, key=lambda a: -a.score)
        return [a.key for a in sorted_anchors[:n]]


# ---------------------------------------------------------------------- #
# Query clock (no NL date parsing at anchor stage)
# ---------------------------------------------------------------------- #


def extract_time_hints(question: str = "", *, today: Optional[date] = None) -> list[str]:
    """Supply a **calendar "today"** for downstream SQL LLM context (``today=YYYYMMDD``).

    ``question`` is ignored — anchor resolution does not regex-parse dates or relative
    phrases from the user query. By default ``today`` is the server local date; callers
    (e.g. Topology Explorer ``as_of_date``) may override so time-relative NL aligns with
    the user-selected partition day.
    """
    today = today or date.today()
    return [f"today={today.strftime('%Y%m%d')}"]


# ---------------------------------------------------------------------- #
# Fulltext anchor search
# ---------------------------------------------------------------------- #

def _fulltext_anchors(
    session, index_name: str, label: str, question: str, k: int = 5
) -> list[AnchorNode]:
    cypher = f"""
    CALL db.index.fulltext.queryNodes('{index_name}', $q) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
    RETURN node.key AS key, coalesce(node.name, '') AS name,
           coalesce(node.domain, '') AS domain, score
    ORDER BY score DESC LIMIT $k
    """
    try:
        rows = session.run(cypher, q=question, k=k).data()
    except Exception as exc:
        log.debug("fulltext_anchors[%s] failed: %s", index_name, exc)
        return []
    return [
        AnchorNode(
            key=row["key"],
            label=label,
            name=row.get("name") or "",
            score=float(row.get("score") or 0.5),
            source="fulltext",
            domain=str(row.get("domain") or ""),
        )
        for row in rows
    ]


# ---------------------------------------------------------------------- #
# Vector anchor search
# ---------------------------------------------------------------------- #

def _vector_anchors(
    session, index_name: str, label: str, emb: list[float], k: int = 5
) -> list[AnchorNode]:
    cypher = f"""
    CALL db.index.vector.queryNodes('{index_name}', $k, $emb) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
    RETURN node.key AS key, coalesce(node.name, '') AS name,
           coalesce(node.domain, '') AS domain, score
    LIMIT $k
    """
    try:
        rows = session.run(cypher, k=k, emb=emb).data()
    except Exception as exc:
        log.debug("vector_anchors[%s] failed: %s", index_name, exc)
        return []
    return [
        AnchorNode(
            key=row["key"],
            label=label,
            name=row.get("name") or "",
            score=float(row.get("score") or 0.0),
            source="vector",
            domain=str(row.get("domain") or ""),
        )
        for row in rows
    ]


def _nodes_to_rrf_rows(nodes: list[AnchorNode]) -> list[dict[str, Any]]:
    return [
        {"key": n.key, "label": n.label, "name": n.name, "score": n.score, "domain": n.domain}
        for n in nodes
    ]


def _rrf_rows_to_anchors(
    rows: list[dict[str, Any]],
    *,
    vec_score_by_key: Optional[dict[str, float]] = None,
) -> list[AnchorNode]:
    vmap = vec_score_by_key or {}
    return [
        AnchorNode(
            key=r["key"],
            label=r["label"],
            name=r.get("name") or "",
            score=float(r["score"]),
            vec_score=float(vmap.get(r["key"], 0.0)),
            source="rrf",
            domain=str(r.get("domain") or ""),
        )
        for r in rows
    ]


def _anchors_for_label_rrf(
    session,
    *,
    label: str,
    ft_index: str,
    vec_index: Optional[str] = None,
    combined_query: str,
    emb: Optional[list[float]],
    k_fulltext: int,
    k_vector: int,
    exact_match_terms: Optional[set[str]] = None,
    exact_match_boost: float = 3.0,
) -> list[AnchorNode]:
    ft = _fulltext_anchors(session, ft_index, label, combined_query, k_fulltext)
    rankings: list[list[dict[str, Any]]] = [_nodes_to_rrf_rows(ft)]
    vec_score_by_key: dict[str, float] = {}
    if emb and vec_index:
        vec = _vector_anchors(session, vec_index, label, emb, k_vector)
        rankings.append(_nodes_to_rrf_rows(vec))
        # Preserve the raw cosine per key (RRF merge below discards it).
        for n in vec:
            prev = vec_score_by_key.get(n.key)
            if prev is None or n.score > prev:
                vec_score_by_key[n.key] = float(n.score)
    merged = rrf_merge(rankings, key_field="key")
    anchors = _rrf_rows_to_anchors(merged, vec_score_by_key=vec_score_by_key)
    # Exact-match boost: if a recall anchor's name or alias exactly matches
    # one of the LLM-extracted entity terms, multiply its RRF score.  This
    # ensures that "点赞次数" extracted from the query decisively outranks
    # "点赞率" which only partially overlaps.
    if exact_match_terms:
        terms_lower = {t.lower() for t in exact_match_terms if t}
        for a in anchors:
            candidates = [a.name, *a.aliases]
            if any(c.lower() in terms_lower for c in candidates if c):
                a.score *= exact_match_boost
        anchors.sort(key=lambda a: -a.score)
    return anchors


def _anchors_knowledge_union(
    session,
    *,
    combined_query: str,
    emb: Optional[list[float]] = None,
    k_fulltext: int,
    k_vector: int = 10,
    merged_cap: Optional[int] = None,
    knowledge_score_scale: float = 1.0,
) -> list[AnchorNode]:
    """知识库合并召回：Event / Entity 各自「全文(description 索引) + 向量(ev_vec/ent_vec) → RRF」，再池化去重。

    无 ``emb`` 或向量索引不可用时退化为仅全文。``knowledge_score_scale`` 作用于融合后的 score。
    """
    pooled: list[AnchorNode] = []
    if emb:
        pooled.extend(
            _anchors_for_label_rrf(
                session,
                label="Event",
                ft_index="event_text",
                vec_index="ev_vec",
                combined_query=combined_query,
                emb=emb,
                k_fulltext=k_fulltext,
                k_vector=k_vector,
            )
        )
        pooled.extend(
            _anchors_for_label_rrf(
                session,
                label="Entity",
                ft_index="entity_text",
                vec_index="ent_vec",
                combined_query=combined_query,
                emb=emb,
                k_fulltext=k_fulltext,
                k_vector=k_vector,
            )
        )
    if not pooled:
        pooled.extend(_fulltext_anchors(session, "event_text", "Event", combined_query, k_fulltext))
        pooled.extend(_fulltext_anchors(session, "entity_text", "Entity", combined_query, k_fulltext))
    merged = _dedupe_anchors_max_score(pooled)
    sc = float(knowledge_score_scale)
    if sc != 1.0:
        for a in merged:
            a.score = float(a.score) * sc
    merged.sort(key=lambda a: -a.score)
    cap = merged_cap if merged_cap is not None else max(k_fulltext * 8, 64)
    return merged[:cap] if cap > 0 else merged


def _dedupe_anchors_max_score(nodes: list[AnchorNode]) -> list[AnchorNode]:
    by_key: dict[str, AnchorNode] = {}
    for a in nodes:
        prev = by_key.get(a.key)
        if prev is None or a.score > prev.score:
            by_key[a.key] = a
    return list(by_key.values())


def _expand_entity_surface_metrics(
    driver: Driver, anchors: list[AnchorNode]
) -> list[AnchorNode]:
    """For each recalled Entity with ``SURFACE_METRIC``→Metric edges, add those Metrics as anchors."""
    ent_by_key = {a.key: a for a in anchors if a.label == "Entity"}
    if not ent_by_key:
        return anchors
    ent_keys = list(ent_by_key.keys())
    decay = 0.97
    extra: list[AnchorNode] = []
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            UNWIND $ent_keys AS ek
            MATCH (e:Entity {key: ek})-[:SURFACE_METRIC]->(m:Metric)
            WHERE (m.valid_to IS NULL OR m.valid_to > datetime())
            RETURN ek AS ent_key, m.key AS key, coalesce(m.name, '') AS name
            """,
            ent_keys=ent_keys,
        ).data()
    seen_pair: set[tuple[str, str]] = set()
    for r in rows:
        ek = str(r.get("ent_key") or "")
        mk = str(r.get("key") or "")
        if not ek or not mk:
            continue
        pair = (ek, mk)
        if pair in seen_pair:
            continue
        seen_pair.add(pair)
        parent = ent_by_key.get(ek)
        base = float(parent.score) if parent else 0.0
        extra.append(
            AnchorNode(
                key=mk,
                label="Metric",
                name=str(r.get("name") or ""),
                score=base * decay,
                source="entity_surface",
            )
        )
    return anchors + extra


# ---------------------------------------------------------------------- #
# Domain soft filter
# ---------------------------------------------------------------------- #


def _domain_soft_filter(
    anchors: list[AnchorNode],
    domain: str,
) -> list[AnchorNode]:
    """Prefer same-domain anchors; fall back to full list when no same-domain hit.

    This is a *soft* filter: if the specified domain has zero matches in
    ``anchors``, the original list is returned unchanged so that recall is
    never dropped to empty.  Mirrors the pattern in ``retrieval.resolve_metric``.
    """
    if not domain:
        return anchors
    dl = domain.lower()
    same = [a for a in anchors if (a.domain or "").lower() == dl]
    return same if same else anchors


# ---------------------------------------------------------------------- #
# Main entry
# ---------------------------------------------------------------------- #

def resolve_anchors(
    driver: Driver,
    question: str,
    *,
    evidence: str = "",
    db_id: str = "",
    embedder: Optional[Callable[[str], list[float]]] = None,
    k_fulltext: int = 25,
    k_vector: int = 25,
    knowledge_merged_cap: Optional[int] = None,
    knowledge_score_scale: float = 1.0,
    time_anchor_today: Optional[date] = None,
    domain: str = "",
    exact_match_terms: Optional[set[str]] = None,
) -> AnchorSet:
    """Resolve anchors：schema（Metric/Dim/Column）分标签 RRF；知识库 Event/Entity 为 description 全文 + ev_vec/ent_vec RRF。

    知识库 Entity 经 SURFACE_METRIC 展开到 Metric。

    Args:
        driver:     Neo4j driver.
        question:   User NL question.
        evidence:   Optional extra evidence text (appended to query for vector search).
        db_id:      Neo4j logical database (from neo4j_database_ctx if not provided).
        embedder:   Optional embedder callable. If None, only fulltext contributes per label.
        k_fulltext: Number of fulltext candidates per index.
        k_vector:   Number of vector candidates per index.
        knowledge_merged_cap: Optional cap on knowledge pool size after merge (default: max(k_fulltext*8, 64)).
        knowledge_score_scale: Multiplier for Event/Entity fused scores before merge into anchor union (default 1.0).
        time_anchor_today: If set, ``time_hints`` use this date as ``today`` instead of server clock.

    Returns:
        AnchorSet：``anchors`` 为全集；``anchors_metric`` 等分桶字段供展示与调试。
    """
    combined_query = question
    if evidence:
        combined_query = f"{question} {evidence}"

    time_hints = extract_time_hints(today=time_anchor_today)

    emb: Optional[list[float]] = None
    if embedder:
        try:
            emb = embedder(combined_query)
        except Exception as exc:
            log.warning("embedder failed: %s", exc)

    metrics_raw: list[AnchorNode] = []
    dimensions_raw: list[AnchorNode] = []
    columns_raw: list[AnchorNode] = []
    knowledge_raw: list[AnchorNode] = []

    with neo4j_session(driver) as s:
        metrics_raw = _anchors_for_label_rrf(
            s,
            label="Metric",
            ft_index="metric_text",
            vec_index="met_vec",
            combined_query=combined_query,
            emb=emb,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
            exact_match_terms=exact_match_terms,
        )
        dimensions_raw = _anchors_for_label_rrf(
            s,
            label="Dimension",
            ft_index="dim_text",
            vec_index="dim_vec",
            combined_query=combined_query,
            emb=emb,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
            exact_match_terms=exact_match_terms,
        )
        columns_raw = _anchors_for_label_rrf(
            s,
            label="Column",
            ft_index="col_text",
            vec_index="col_vec",
            combined_query=combined_query,
            emb=emb,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
            exact_match_terms=exact_match_terms,
        )
        knowledge_raw = _anchors_knowledge_union(
            s,
            combined_query=combined_query,
            emb=emb,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
            merged_cap=knowledge_merged_cap,
            knowledge_score_scale=knowledge_score_scale,
        )
        claim_raw = _anchors_for_label_rrf(
            s,
            label="Claim",
            ft_index="claim_text",
            vec_index="claim_vec" if emb else None,
            combined_query=combined_query,
            emb=emb,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
        )

    # Domain soft filter on schema buckets (metric / dimension / column).
    # Knowledge (Event / Entity) is not domain-scoped in the same
    # way — it represents cross-domain concepts.
    metrics_raw = _domain_soft_filter(metrics_raw, domain)
    dimensions_raw = _domain_soft_filter(dimensions_raw, domain)
    columns_raw = _domain_soft_filter(columns_raw, domain)

    entity_from_kb = [a for a in knowledge_raw if a.label == "Entity"]
    schema_for_expand = metrics_raw + entity_from_kb
    expanded_schema = _expand_entity_surface_metrics(driver, schema_for_expand)

    metrics_expanded = [a for a in expanded_schema if a.label == "Metric"]

    anchors_metric = _dedupe_anchors_max_score(metrics_expanded)
    anchors_dimension = _dedupe_anchors_max_score(dimensions_raw)
    anchors_column = _dedupe_anchors_max_score(columns_raw)
    anchors_knowledge = knowledge_raw
    anchors_claim = _dedupe_anchors_max_score(claim_raw)

    merged_union = _dedupe_anchors_max_score(
        list(expanded_schema) + dimensions_raw + columns_raw + knowledge_raw
    )

    # 一次性把 description / aliases 拉回来，给 Decision LLM 识别 anchor 用；
    # 同时给所有 Metric / Dimension 算真实 vec_score（不依赖 ANN 排名）
    try:
        _enrich_anchor_descriptions(
            driver, merged_union, query=combined_query, embedder=embedder,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("_enrich_anchor_descriptions failed: %s", exc)

    # Enrichment mutated nodes in merged_union; mirror description/synonyms onto bucket copies by key
    rich_by_key = {a.key: a for a in merged_union}

    def _sync_enrichment(dest: list[AnchorNode]) -> None:
        for a in dest:
            src = rich_by_key.get(a.key)
            if src is None:
                continue
            a.description = getattr(src, "description", "") or ""
            a.aliases = list(getattr(src, "aliases", None) or [])

    _sync_enrichment(anchors_metric)
    _sync_enrichment(anchors_dimension)
    _sync_enrichment(anchors_column)
    _sync_enrichment(anchors_knowledge)

    # Claims are NOT in merged_union — enrich them separately.
    _enrich_claim_anchors(driver, anchors_claim)

    return AnchorSet(
        anchors=merged_union,
        time_hints=time_hints,
        question=question,
        db_id=db_id,
        anchors_metric=anchors_metric,
        anchors_dimension=anchors_dimension,
        anchors_column=anchors_column,
        anchors_knowledge=anchors_knowledge,
        anchors_claim=anchors_claim,
    )


def merge_anchor_sets(
    sets: list[AnchorSet],
    *,
    primary_question: str = "",
    db_id: str = "",
    time_anchor_today: Optional[date] = None,
) -> AnchorSet:
    """Union multiple :class:`AnchorSet` from per-facet ``resolve_anchors``; dedupe by key keeping max score."""
    if not sets:
        return AnchorSet(
            anchors=[],
            time_hints=extract_time_hints(today=time_anchor_today),
            question=primary_question,
            db_id=db_id,
        )
    if len(sets) == 1:
        s0 = sets[0]
        if primary_question or db_id:
            return AnchorSet(
                anchors=list(s0.anchors),
                time_hints=list(s0.time_hints),
                question=primary_question or s0.question,
                db_id=db_id or s0.db_id,
                anchors_metric=list(s0.anchors_metric),
                anchors_dimension=list(s0.anchors_dimension),
                anchors_column=list(s0.anchors_column),
                anchors_knowledge=list(s0.anchors_knowledge),
                anchors_claim=list(s0.anchors_claim),
            )
        return s0

    th: list[str] = []
    seen_t: set[str] = set()
    for s in sets:
        for h in s.time_hints or []:
            if h not in seen_t:
                seen_t.add(h)
                th.append(h)
    if not th:
        th = extract_time_hints(today=time_anchor_today)

    merged_all = _dedupe_anchors_max_score(
        [a for s in sets for a in (s.anchors or [])]
    )
    m_m = _dedupe_anchors_max_score(
        [a for s in sets for a in (s.anchors_metric or [])]
    )
    m_d = _dedupe_anchors_max_score(
        [a for s in sets for a in (s.anchors_dimension or [])]
    )
    m_c = _dedupe_anchors_max_score(
        [a for s in sets for a in (s.anchors_column or [])]
    )
    m_k = _dedupe_anchors_max_score(
        [a for s in sets for a in (s.anchors_knowledge or [])]
    )
    m_cl = _dedupe_anchors_max_score(
        [a for s in sets for a in (s.anchors_claim or [])]
    )

    return AnchorSet(
        anchors=merged_all,
        time_hints=th,
        question=primary_question or sets[0].question,
        db_id=db_id or sets[0].db_id,
        anchors_metric=m_m,
        anchors_dimension=m_d,
        anchors_column=m_c,
        anchors_knowledge=m_k,
        anchors_claim=m_cl,
    )


def resolve_anchors_multi(
    driver: Driver,
    queries: list[str],
    *,
    evidence: str = "",
    primary_question: str = "",
    db_id: str = "",
    embedder: Optional[Callable[[str], list[float]]] = None,
    k_fulltext: int = 25,
    k_vector: int = 25,
    knowledge_merged_cap: Optional[int] = None,
    knowledge_score_scale: float = 1.0,
    time_anchor_today: Optional[date] = None,
    domain: str = "",
    exact_match_terms: Optional[set[str]] = None,
) -> AnchorSet:
    """Run :func:`resolve_anchors` per non-empty query string and merge results."""
    cleaned = [q.strip() for q in queries if q and str(q).strip()]
    if not cleaned:
        return resolve_anchors(
            driver,
            "",
            evidence=evidence,
            db_id=db_id,
            embedder=embedder,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
            knowledge_merged_cap=knowledge_merged_cap,
            knowledge_score_scale=knowledge_score_scale,
            time_anchor_today=time_anchor_today,
            domain=domain,
            exact_match_terms=exact_match_terms,
        )
    if len(cleaned) == 1:
        return resolve_anchors(
            driver,
            cleaned[0],
            evidence=evidence,
            db_id=db_id,
            embedder=embedder,
            k_fulltext=k_fulltext,
            k_vector=k_vector,
            knowledge_merged_cap=knowledge_merged_cap,
            knowledge_score_scale=knowledge_score_scale,
            time_anchor_today=time_anchor_today,
            domain=domain,
            exact_match_terms=exact_match_terms,
        )

    sets: list[AnchorSet] = []
    for q in cleaned:
        sets.append(
            resolve_anchors(
                driver,
                q,
                evidence=evidence,
                db_id=db_id,
                embedder=embedder,
                k_fulltext=k_fulltext,
                k_vector=k_vector,
                knowledge_merged_cap=knowledge_merged_cap,
                knowledge_score_scale=knowledge_score_scale,
                time_anchor_today=time_anchor_today,
                domain=domain,
                exact_match_terms=exact_match_terms,
            )
        )
    merged = merge_anchor_sets(
        sets,
        primary_question=primary_question or cleaned[0],
        db_id=db_id,
        time_anchor_today=time_anchor_today,
    )
    try:
        _enrich_anchor_descriptions(
            driver, merged.anchors,
            query=primary_question or cleaned[0], embedder=embedder,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("_enrich_anchor_descriptions (multi) failed: %s", exc)

    rich_by_key = {a.key: a for a in merged.anchors}

    def _sync_enrichment(dest: list[AnchorNode]) -> None:
        for a in dest:
            src = rich_by_key.get(a.key)
            if src is None:
                continue
            a.description = getattr(src, "description", "") or ""
            a.aliases = list(getattr(src, "aliases", None) or [])

    _sync_enrichment(merged.anchors_metric)
    _sync_enrichment(merged.anchors_dimension)
    _sync_enrichment(merged.anchors_column)
    _sync_enrichment(merged.anchors_knowledge)

    # Claims are enriched separately (different node properties).
    try:
        _enrich_claim_anchors(driver, merged.anchors_claim)
    except Exception as exc:  # noqa: BLE001
        log.debug("_enrich_claim_anchors (multi) failed: %s", exc)

    return merged


def _enrich_claim_anchors(
    driver: Driver,
    anchors_claim: list[AnchorNode],
) -> None:
    """Fetch text / predicate / confidence for Claim anchors so the Decision LLM
    can read the actual assertion, not just a bare key.

    Claim nodes don't have ``name`` / ``description`` / ``aliases``; they store
    ``text``, ``predicate``, ``subject_type``, ``confidence``. This maps those
    into the ``AnchorNode.description`` field for rendering.
    """
    if not anchors_claim:
        return
    keys = [a.key for a in anchors_claim if a.key]
    if not keys:
        return
    try:
        with neo4j_session(driver) as s:
            rows = s.run(
                """
                UNWIND $keys AS k
                OPTIONAL MATCH (cl:Claim {key: k})
                RETURN k AS key,
                       coalesce(cl.text, '') AS text,
                       coalesce(cl.predicate, '') AS predicate,
                       coalesce(cl.subject_type, '') AS subject_type,
                       coalesce(cl.confidence, 0.0) AS confidence
                """,
                keys=keys,
            ).data()
    except Exception as exc:  # noqa: BLE001
        log.debug("_enrich_claim_anchors failed: %s", exc)
        return
    by_key = {r["key"]: r for r in rows}
    for a in anchors_claim:
        r = by_key.get(a.key)
        if not r:
            continue
        text = str(r.get("text") or "").strip()
        pred = str(r.get("predicate") or "").strip()
        stype = str(r.get("subject_type") or "").strip()
        conf = float(r.get("confidence") or 0.0)
        a.name = text[:120]
        a.description = text
        a.aliases = []
        if pred:
            a.aliases = [pred]
        if stype:
            a.aliases.append(stype)
        a.vec_score = conf


def _enrich_anchor_descriptions(
    driver: Driver,
    anchors: list[AnchorNode],
    *,
    query: str = "",
    embedder: Optional[Callable[[str], list[float]]] = None,
) -> None:
    """Top-N anchor 节点上补 description / aliases；同时给所有 Metric / Dimension 计算真实 vec_score。

    schema_auto 自动抽出来的 Metric/Column 名字常常是裸列名 (``active_usercnt_1d``)，
    Decision LLM 只看 key + name 完全猜不出业务含义。把 description / aliases 一起
    拉到 anchor 上，``_format_anchors`` 渲染时附带，模型才能识别 "active_usercnt_1d
    其实是 DAU 列"。

    只对 top-20 anchors 拉 description / aliases (≈ 一次 Cypher round-trip)，
    避免放大全集开销。但 ``vec_score`` 会为 **所有** Metric / Dimension anchor
    计算真实 cosine（从 graph 取 embedding 与 query embedding 做 dot product），
    不再用 ANN 排名当代理 —— 即使某个 metric 没被 ``met_vec`` ANN 召回
    （典型情况：核心 DAU 类指标在同 domain 具体指标里被挤到 rank 30+），
    也能拿到真实的向量相似度供下游 ``score_candidate`` 使用。
    """
    if not anchors:
        return

    # 拉 description / aliases / embedding：top-20 by RRF（渲染范围）+ 所有
    # Metric / Dimension（要算真实 vec_score）。
    top_by_rrf = sorted(anchors, key=lambda x: -x.score)[:20]
    schema_anchors = [a for a in anchors if a.label in ("Metric", "Dimension")]
    seen: set[str] = set()
    fetch: list[AnchorNode] = []
    for a in (*top_by_rrf, *schema_anchors):
        if a.key and a.key not in seen:
            seen.add(a.key)
            fetch.append(a)
    if not fetch:
        return
    keys = [a.key for a in fetch]
    with neo4j_session(driver) as s:
        rows = s.run(
            """
            UNWIND $keys AS k
            OPTIONAL MATCH (n {key: k})
            RETURN k AS key,
                   coalesce(n.description, n.summary, n.body_md, n.comment, '') AS description,
                   coalesce(n.aliases, []) AS aliases,
                   n.embedding AS embedding
            """,
            keys=keys,
        ).data()
    by_key = {r["key"]: r for r in rows}

    # 真实 cosine for Metric / Dimension anchors (不管 RRF 排名)
    q_emb = None
    if embedder is not None:
        try:
            q_emb = embedder(query or "") if query else None
        except Exception:  # noqa: BLE001
            q_emb = None

    for a in fetch:
        r = by_key.get(a.key)
        if not r:
            continue
        # description / aliases: 只对 top-20 by RRF 写回（避免污染非 top 节点的渲染）
        if a in top_by_rrf:
            a.description = str(r.get("description") or "").strip()
            syns = r.get("aliases") or []
            a.aliases = [str(x) for x in syns if x]
        # vec_score: 给所有 Metric/Dimension 算真实 cosine（覆盖 ANN 排名代理）
        if a.label in ("Metric", "Dimension") and q_emb is not None:
            n_emb = r.get("embedding")
            if n_emb and len(n_emb) == len(q_emb):
                cos = sum(x * y for x, y in zip(n_emb, q_emb))  # L2-normalized
                a.vec_score = float(max(0.0, min(1.0, cos)))


__all__ = [
    "AnchorNode",
    "AnchorSet",
    "extract_time_hints",
    "merge_anchor_sets",
    "resolve_anchors",
    "resolve_anchors_multi",
]

"""Assembly layer: convert ContextSnapshot internal state into public API models.

All Pydantic response models are defined here so ctx_service.py and ctx_session.py
remain independent of each other. The assembly functions are pure / side-effect
free—they only read from the snapshot.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel

from ..graph.keys import logical_dataset_name as _logical_ds


def _to_ds(raw: str) -> str:
    """Map to logical dataset name only when input is a qualified (dotted) reference."""
    if "." in raw:
        return _logical_ds(raw)
    return raw


# =========================================================================== #
# Shared leaf types
# =========================================================================== #

class MetricBrief(BaseModel):
    key: str
    name: str
    domain: str = ""
    definition: str = ""
    description: str = ""
    unit: str = ""
    aliases: list[str] = []
    formula_evidence: str = ""
    score: float = 0.0


class DimensionBrief(BaseModel):
    key: str
    name: str
    domain: str = ""
    aliases: list[str] = []
    sample_values: list[str] = []


class ColumnBrief(BaseModel):
    key: str
    name: str
    table: str = ""
    comment: str = ""
    granularity_role: str = ""
    topline_value: str = ""
    data_type: str = ""
    sample_values: list[str] = []


class TableBrief(BaseModel):
    key: str
    name: str
    db: str = ""
    partition_columns: list[str] = []
    comment: str = ""


class KnowledgeSnippet(BaseModel):
    key: str
    label: str              # "Entity" | "Event"
    name: str
    summary: str = ""


class CardBrief(BaseModel):
    key: str
    polarity: str           # "positive" | "negative"
    task_type: str = ""
    strategy_semantics: str = ""
    lesson: str = ""
    composite_score: float = 0.0


class ExperienceStats(BaseModel):
    visible_n: int
    blocked_n: int
    top_score: float
    gap: float              # score gap between top-1 and top-2


class ColumnRef(BaseModel):
    key: str
    name: str
    table: str = ""
    role: str = ""


class DimensionRef(BaseModel):
    key: str
    name: str


class MetricRef(BaseModel):
    key: str
    name: str


class TopoNeighbor(BaseModel):
    key: str
    label: str
    name: str
    rel_type: str


class EventRef(BaseModel):
    key: str
    name: str
    description: str = ""


class ValueItem(BaseModel):
    value: str
    business_meaning: str = ""
    frequency: Optional[float] = None


# =========================================================================== #
# Composite response models
# =========================================================================== #

class SemanticsBlock(BaseModel):
    metrics: list[MetricBrief] = []
    dimensions: list[DimensionBrief] = []
    columns: list[ColumnBrief] = []
    tables: list[TableBrief] = []
    relations_nl: list[str] = []
    knowledge: list[KnowledgeSnippet] = []
    business_rules: list[str] = []


class ExperienceBlock(BaseModel):
    cards: list[CardBrief] = []
    negative_hints: list[str] = []
    stats: ExperienceStats


class AmbiguityItem(BaseModel):
    aspect: str             # "metric" | "dimension" | "time" | "column"
    candidates: list[str] = []
    rationale: str = ""


class ContextPack(BaseModel):
    session_ref: str
    snapshot_id: str
    snapshot_index: int
    semantics: SemanticsBlock
    experience: ExperienceBlock
    ambiguity_candidates: list[AmbiguityItem] = []
    time_hints: list[str] = []
    debug: Optional[dict[str, Any]] = None
    operation: Optional[dict[str, Any]] = None  # pipeline 步骤 + ContextPack 组装过程


class EntityDetail(BaseModel):
    key: str
    label: str
    name: str
    match_confidence: float = 1.0
    operation: Optional[dict[str, Any]] = None

    # MG — metadata graph (schema / semantic layer)
    definition: str = ""
    formula_semantic: Optional[str] = None
    source_columns: list[ColumnRef] = []
    drill_dimensions: list[DimensionRef] = []
    common_filters: list[str] = []
    related_metrics: list[MetricRef] = []

    topology_neighbors: list[TopoNeighbor] = []  # MG — OF_VIEW / join neighborhood
    sample_values: list[ValueItem] = []          # MG — column/dimension samples

    # KG — external knowledge
    related_knowledge: list[KnowledgeSnippet] = []
    related_events: list[EventRef] = []

    # TG — strategy cards (:Strategy)
    related_cards: list[CardBrief] = []


class SupersedeChain(BaseModel):
    root_key: str
    chain: list[str] = []


class ExperienceFragment(BaseModel):
    operation: Optional[dict[str, Any]] = None
    new_cards: list[CardBrief] = []
    supersede_chains: list[SupersedeChain] = []
    stats: ExperienceStats


# =========================================================================== #
# Assembly helpers
# =========================================================================== #

def _center_props(center: Any) -> dict[str, Any]:
    """Normalize expand_subgraph ``center`` (may be key str, props dict, or Neo4j node)."""
    if center is None:
        return {}
    if isinstance(center, dict):
        return center
    if isinstance(center, str):
        return {"key": center, "name": center}
    props = getattr(center, "_properties", None) or getattr(center, "items", None)
    if callable(props):
        try:
            return dict(center)
        except Exception:
            pass
    if hasattr(center, "get"):
        try:
            return dict(center)
        except Exception:
            pass
    return {"name": str(center)}


def _card_brief(c: dict[str, Any]) -> CardBrief:
    return CardBrief(
        key=str(c.get("key") or ""),
        polarity=str(c.get("polarity") or "positive"),
        task_type=str(c.get("task_type") or ""),
        strategy_semantics=str(c.get("strategy_semantics") or "")[:500],
        lesson=str(c.get("lesson") or c.get("avoid_lesson") or "")[:300],
        composite_score=float(c.get("composite_score") or 0.0),
    )


def _anchor_to_brief(a: Any) -> dict[str, Any]:
    return {
        "key": getattr(a, "key", ""),
        "label": getattr(a, "label", ""),
        "name": getattr(a, "name", ""),
        "score": float(getattr(a, "score", 0.0)),
    }


def assemble_context_pack(
    session_ref: str,
    snapshot_index: int,
    snapshot: Any,          # ContextSnapshot
    *,
    include_debug: bool = False,
    pipeline_steps: Optional[list[dict[str, Any]]] = None,
    operation_extras: Optional[dict[str, Any]] = None,
    endpoint: str = "search_context",
) -> ContextPack:
    """Assemble ContextPack from a ContextSnapshot."""
    from .ctx_session import ContextSnapshot  # local to avoid circular import

    anchors = snapshot.anchors

    # ── Semantics ─────────────────────────────────────────────────────────── #
    metrics: list[MetricBrief] = []
    dimensions: list[DimensionBrief] = []
    columns: list[ColumnBrief] = []
    tables: list[TableBrief] = []
    knowledge: list[KnowledgeSnippet] = []
    business_rules: list[str] = []
    seen_keys: set[str] = set()

    for a in getattr(anchors, "anchors_metric", []) or []:
        k = str(getattr(a, "key", "") or "")
        if k in seen_keys:
            continue
        seen_keys.add(k)
        exp = (snapshot.expanded_subgraphs or {}).get(k, {})
        raw = exp.get("raw", {}) if isinstance(exp, dict) else {}
        definition = ""
        formula_evidence = ""
        if raw.get("formulas"):
            f0 = raw["formulas"][0]
            definition = str(f0.get("expression") or f0.get("formula") or "")
            formula_evidence = str(f0.get("formula_evidence") or "")
            for cal in raw.get("calibers") or []:
                cal_props = cal.get("props", {}) if isinstance(cal, dict) else {}
                rule = str(cal_props.get("description") or cal_props.get("filter_expr") or "")
                if rule and rule not in business_rules:
                    business_rules.append(rule)
        center = _center_props(exp.get("center") if isinstance(exp, dict) else None)
        metrics.append(MetricBrief(
            key=k,
            name=str(getattr(a, "name", "") or "") or str(center.get("name") or ""),
            domain=str(center.get("domain") or ""),
            definition=definition,
            description=str(center.get("description") or getattr(a, "description", "") or ""),
            unit=str(center.get("unit") or ""),
            aliases=list(center.get("aliases") or getattr(a, "aliases", []) or []),
            formula_evidence=formula_evidence,
            score=float(getattr(a, "score", 0.0) or 0.0),
        ))

    for a in getattr(anchors, "anchors_dimension", []) or []:
        k = getattr(a, "key", "")
        if k in seen_keys:
            continue
        seen_keys.add(k)
        # Try to find this dimension in expanded subgraphs to get richer info
        dim_aliases = list(getattr(a, "aliases", []) or [])
        dim_samples: list[str] = []
        for exp in (snapshot.expanded_subgraphs or {}).values():
            if not isinstance(exp, dict):
                continue
            for n in exp.get("nodes") or []:
                if not isinstance(n, dict):
                    continue
                if n.get("id") == k and n.get("group") == "Dimension":
                    props = n.get("props") or {}
                    dim_aliases = dim_aliases or list(props.get("aliases") or [])
                    dim_samples = list(props.get("sample_values") or [])
                    break
        dimensions.append(DimensionBrief(
            key=str(k),
            name=str(getattr(a, "name", "") or ""),
            domain=str(getattr(a, "domain", "") or ""),
            aliases=dim_aliases,
            sample_values=dim_samples,
        ))

    # Index Column-group nodes across all expanded metric subgraphs so we can
    # (a) enrich anchored columns with granularity_role/topline_value and
    # (b) surface partition / rollup columns that weren't directly anchored
    # but the agent needs to see (e.g. terminal_type with topline '全部').
    col_props_by_key: dict[str, dict[str, Any]] = {}
    _col_to_dim_name: dict[str, str] = {}
    for exp in (snapshot.expanded_subgraphs or {}).values():
        if not isinstance(exp, dict):
            continue
        for n in exp.get("nodes") or []:
            if not isinstance(n, dict):
                continue
            nid = str(n.get("id") or "")
            if n.get("group") == "Column" and nid and nid not in col_props_by_key:
                col_props_by_key[nid] = dict(n.get("props") or {})
        # Build column→dimension name map from MAPS_TO_COLUMN edges
        dim_nodes = {
            str(n.get("id") or ""): n for n in (exp.get("nodes") or [])
            if isinstance(n, dict) and n.get("group") == "Dimension"
        }
        for e in exp.get("edges") or []:
            if isinstance(e, dict) and (e.get("type") or e.get("label")) == "MAPS_TO_COLUMN":
                src = str(e.get("from") or e.get("source") or "")
                tgt = str(e.get("to") or e.get("target") or "")
                dn = dim_nodes.get(src)
                if dn and tgt not in _col_to_dim_name:
                    dname = str((dn.get("props") or {}).get("name") or "")
                    if dname:
                        _col_to_dim_name[tgt] = dname

    def _col_table_from_props(props: dict[str, Any]) -> str:
        return _to_ds(str(props.get("table") or props.get("dataset_name") or ""))

    def _sem_col_name(key: str, phys_name: str, comment: str = "") -> str:
        """Dimension name > comment > physical name."""
        dim = _col_to_dim_name.get(key)
        if dim:
            return dim
        if comment and comment.strip():
            return comment.strip()
        return phys_name

    for a in getattr(anchors, "anchors_column", []) or []:
        k = str(getattr(a, "key", "") or "")
        if k in seen_keys:
            continue
        seen_keys.add(k)
        props = col_props_by_key.get(k, {})
        phys = str(getattr(a, "name", "") or "") or str(props.get("name") or "")
        cmt = str(getattr(a, "comment", "") or "") or str(getattr(a, "description", "") or "")
        columns.append(ColumnBrief(
            key=k,
            name=_sem_col_name(k, phys, cmt),
            table=_to_ds(str(getattr(a, "table", "") or "")) or _col_table_from_props(props),
            comment=cmt,
            granularity_role=str(props.get("granularity_role") or ""),
            topline_value=str(props.get("topline_value") or ""),
            data_type=str(props.get("type") or props.get("data_type") or ""),
            sample_values=list(props.get("sample_values") or [])[:5],
        ))

    # Emit partition / rollup columns from expanded subgraphs that aren't
    # already in the anchored set. These are the columns an agent must respect
    # when writing dimensional-drill SQL (filter out rollup rows, etc.).
    for ck, props in col_props_by_key.items():
        if ck in seen_keys:
            continue
        gr = str(props.get("granularity_role") or "")
        tv = str(props.get("topline_value") or "")
        if not (gr or tv):
            continue
        seen_keys.add(ck)
        phys = str(props.get("name") or "")
        cmt = str(props.get("description") or props.get("comment") or "")
        columns.append(ColumnBrief(
            key=ck,
            name=_sem_col_name(ck, phys, cmt),
            table=_col_table_from_props(props),
            comment=cmt,
            granularity_role=gr,
            topline_value=tv,
            data_type=str(props.get("type") or props.get("data_type") or ""),
            sample_values=list(props.get("sample_values") or [])[:5],
        ))

    for a in getattr(anchors, "anchors_knowledge", []) or []:
        k = str(getattr(a, "key", "") or "")
        if k in seen_keys:
            continue
        seen_keys.add(k)
        knowledge.append(KnowledgeSnippet(
            key=k,
            label=str(getattr(a, "label", "Entity") or "Entity"),
            name=str(getattr(a, "name", "") or ""),
            summary=str(getattr(a, "description", "") or ""),
        ))

    # Tables from subgraph nodes_by_label
    sg = snapshot.subgraph
    if sg:
        for tbl_key in (getattr(sg, "nodes_by_label", {}) or {}).get("Table", []):
            if tbl_key in seen_keys:
                continue
            seen_keys.add(tbl_key)
            parts = str(tbl_key).split(".")
            tbl_name = parts[-1] if parts else tbl_key
            tables.append(TableBrief(key=tbl_key, name=_to_ds(tbl_name)))

    semantics = SemanticsBlock(
        metrics=metrics,
        dimensions=dimensions,
        columns=columns,
        tables=tables,
        relations_nl=[],
        knowledge=knowledge,
        business_rules=business_rules,
    )

    # ── Experience ────────────────────────────────────────────────────────── #
    cards = [_card_brief(c) for c in (snapshot.cards_visible or [])]
    negative_hints = [
        str(c.get("lesson") or c.get("avoid_lesson") or "")
        for c in (snapshot.cards_visible or [])
        if c.get("polarity") in ("negative", "avoid")
        and (c.get("lesson") or c.get("avoid_lesson"))
    ]
    scores = [c.composite_score for c in cards]
    top_score = max(scores) if scores else 0.0
    second_score = sorted(scores, reverse=True)[1] if len(scores) > 1 else 0.0
    gap = top_score - second_score
    experience = ExperienceBlock(
        cards=cards,
        negative_hints=negative_hints,
        stats=ExperienceStats(
            visible_n=len(snapshot.cards_visible or []),
            blocked_n=len(snapshot.cards_blocked or []),
            top_score=top_score,
            gap=gap,
        ),
    )

    # ── Ambiguity candidates ───────────────────────────────────────────────── #
    ambiguity: list[AmbiguityItem] = []
    # If multiple metrics found, suggest as metric ambiguity
    if len(metrics) >= 2:
        ambiguity.append(AmbiguityItem(
            aspect="metric",
            candidates=[m.name for m in metrics[:5]],
            rationale="Multiple metrics matched; consider using explore_entity to disambiguate.",
        ))

    time_hints = list(getattr(anchors, "time_hints", []) or [])

    debug: Optional[dict[str, Any]] = None
    if include_debug:
        dec = snapshot.decision
        debug = {
            "facets": snapshot.facets,
            "anchor_count": len(getattr(anchors, "anchors", []) or []),
            "subgraph_method": getattr(sg, "method", "") if sg else "none",
            "subgraph_nodes": len(getattr(sg, "node_keys", []) or []) if sg else 0,
            "task_type": getattr(dec, "task_type", "") if dec else "",
            "reuse_key": getattr(dec, "reuse_key", None) if dec else None,
        }

    from .ctx_trace import build_assembly_trace, build_operation_trace

    assembly_steps = build_assembly_trace(
        snapshot=snapshot,
        metrics_n=len(metrics),
        dimensions_n=len(dimensions),
        columns_n=len(columns),
        tables_n=len(tables),
        knowledge_n=len(knowledge),
        business_rules_n=len(business_rules),
        cards_n=len(cards),
        ambiguity_n=len(ambiguity),
    )
    operation = build_operation_trace(
        endpoint=endpoint,
        pipeline_steps=pipeline_steps,
        assembly_steps=assembly_steps,
        extras=operation_extras,
    )

    return ContextPack(
        session_ref=session_ref,
        snapshot_id=snapshot.snapshot_id,
        snapshot_index=snapshot_index,
        semantics=semantics,
        experience=experience,
        ambiguity_candidates=ambiguity,
        time_hints=[str(h) for h in time_hints],
        debug=debug,
        operation=operation,
    )


def assemble_entity_detail_from_expand(
    *,
    entity_key: str,
    label: str,
    name: str,
    match_confidence: float,
    expand_data: dict[str, Any],
    topology_neighbors: Optional[list[dict[str, Any]]] = None,
    kg_data: Optional[dict[str, Any]] = None,
    sample_vals: Optional[list[str]] = None,
    related_cards: Optional[list[dict[str, Any]]] = None,
    operation: Optional[dict[str, Any]] = None,
) -> EntityDetail:
    """Build EntityDetail from the various graph query results."""
    raw = expand_data.get("raw", {})
    center = _center_props(expand_data.get("center"))

    # MG fields
    definition = str(center.get("definition") or center.get("description") or "")
    formula_semantic: Optional[str] = None
    source_columns: list[ColumnRef] = []
    drill_dimensions: list[DimensionRef] = []
    common_filters: list[str] = []

    # Build column→dimension semantic name mapping from subgraph edges
    _col_to_dim: dict[str, str] = {}
    _dim_nodes = {
        str(n.get("id") or ""): n
        for n in (expand_data.get("nodes") or [])
        if isinstance(n, dict) and n.get("group") == "Dimension"
    }
    for e in expand_data.get("edges") or []:
        if isinstance(e, dict) and (e.get("type") or e.get("label")) == "MAPS_TO_COLUMN":
            src = str(e.get("from") or e.get("source") or "")
            tgt = str(e.get("to") or e.get("target") or "")
            dn = _dim_nodes.get(src)
            if dn:
                dname = str((dn.get("props") or {}).get("name") or "")
                if dname:
                    _col_to_dim[tgt] = dname

    for fr in raw.get("formulas") or []:
        if fr.get("expression"):
            formula_semantic = str(fr["expression"])
            break

    for fr in raw.get("formulas") or []:
        col = fr.get("col") or {}
        if col and col.get("key"):
            ckey = str(col["key"])
            phys = str(col.get("name") or "")
            sem = _col_to_dim.get(ckey) or str(col.get("comment") or "") or phys
            source_columns.append(ColumnRef(
                key=ckey,
                name=sem,
                table=_to_ds(str(col.get("table") or "")),
                role=str(fr.get("role") or ""),
            ))

    for dr in raw.get("drill_dims") or []:
        dim = dr.get("dim") or {}
        if dim and dim.get("key"):
            drill_dimensions.append(DimensionRef(
                key=str(dim["key"]),
                name=str(dim.get("name") or ""),
            ))

    for cr in raw.get("calibers") or []:
        cal = cr.get("cal") or {}
        if cal:
            props = cal.get("props") or {}
            rule = str(props.get("description") or props.get("filter_expr") or "")
            if rule:
                common_filters.append(rule)

    related_metrics: list[MetricRef] = []
    for dr in raw.get("derived") or []:
        parent = dr.get("parent") or {}
        if parent and parent.get("key"):
            related_metrics.append(MetricRef(
                key=str(parent["key"]),
                name=str(parent.get("name") or ""),
            ))

    # MG topology neighbors
    topo: list[TopoNeighbor] = []
    for n in (topology_neighbors or []):
        topo.append(TopoNeighbor(
            key=str(n.get("key") or ""),
            label=str(n.get("label") or ""),
            name=str(n.get("name") or ""),
            rel_type=str(n.get("rel_type") or ""),
        ))

    # KG
    related_knowledge: list[KnowledgeSnippet] = []
    related_events: list[EventRef] = []
    for n in (kg_data or {}).get("neighbors", []):
        lbl = str(n.get("label") or "")
        if lbl == "Entity":
            related_knowledge.append(KnowledgeSnippet(
                key=str(n.get("key") or ""),
                label=lbl,
                name=str(n.get("name") or ""),
                summary=(str(n.get("description") or "") + (" | " + str(n.get("filter_summary") or "") if n.get("filter_summary") else ""))[:200],
            ))
        elif lbl == "Event":
            related_events.append(EventRef(
                key=str(n.get("key") or ""),
                name=str(n.get("name") or ""),
                description=(str(n.get("description") or "") + (" | " + str(n.get("filter_summary") or "") if n.get("filter_summary") else ""))[:200],
            ))

    # Sample values
    sv: list[ValueItem] = []
    for v in (sample_vals or []):
        sv.append(ValueItem(value=str(v)))

    # Cards
    rc: list[CardBrief] = [_card_brief(c) for c in (related_cards or [])]

    return EntityDetail(
        key=entity_key,
        label=label,
        name=name,
        match_confidence=match_confidence,
        definition=definition,
        formula_semantic=formula_semantic,
        source_columns=source_columns,
        drill_dimensions=drill_dimensions,
        common_filters=common_filters,
        related_metrics=related_metrics,
        topology_neighbors=topo,
        sample_values=sv,
        related_knowledge=related_knowledge,
        related_events=related_events,
        related_cards=rc,
        operation=operation,
    )

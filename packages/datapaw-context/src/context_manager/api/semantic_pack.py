"""Shared semantic-card builders + caps, reused by every CM interface.

Centralises the ``MetricFocus`` / ``MetricCandidate`` / ``KnowledgeNote`` /
``EventCard`` vocabulary so L1 (``search_context``) and L2
(``explore_entity`` / ``search_event`` / ``compare_entities``) return coherent,
capped, relevance-ranked output built on the same primitives.

Caps are read from :data:`context_manager.config.CFG` so they are tuned from a
single place (``config/agent_explorer.json``).
"""
from __future__ import annotations

from typing import Any, Optional

from ..config import CFG
from ..runtime.relevance import (
    blend_relevance,
    normalize_cosine,
    score_candidate,
    soft_text_match,
)
from .cm_models import (
    CommonFilter,
    DrillDimension,
    EventCard,
    KnowledgeNote,
    MetricCandidate,
    MetricFocus,
    SourceColumn,
)


# ---------------------------------------------------------------------- #
# Ranking
# ---------------------------------------------------------------------- #

def _anchor_fields(a: Any) -> tuple[str, list[str], str, float]:
    """Pull (name, aliases, description, raw_vec_score) off an AnchorNode."""
    name = str(getattr(a, "name", "") or "")
    aliases = list(getattr(a, "aliases", []) or [])
    description = str(getattr(a, "description", "") or "")
    vec = float(getattr(a, "vec_score", 0.0) or 0.0)
    return name, aliases, description, vec


def score_anchor(query: str, anchor: Any, *, domain: str = "") -> float:
    """Blended relevance score for one anchor (0–1), uses ``vec_score`` if set.

    When the precision-rerank stage has run (``anchor.rerank_score >= 0``), its
    semantic 0–1 score is blended in (weighted by ``CFG.rerank_score_weight``).
    This is what lets a meaning-based judgment ("访问趋势分析 ↔ DAU/当日访问用户数")
    decisively outrank a niche metric (``分享页访问用户数``) that merely shares the
    "访问" surface concept — the text+vec blend alone leaves them in a noisy tie.

    When *domain* is provided, a conservative same-domain bonus (+0.05) or
    cross-domain penalty (−0.10) is applied so that metrics from unrelated
    business domains are demoted in L1 ranking.
    """
    name, aliases, description, vec = _anchor_fields(anchor)
    blended, _text, _vecn = score_candidate(
        query, name=name, aliases=aliases, description=description,
        raw_vec_score=vec,
    )
    rr = float(getattr(anchor, "rerank_score", -1.0) or -1.0)
    if 0.0 <= rr <= 1.0:
        # Apply the rerank as a *zero-centered* nudge rather than a linear blend:
        # a high semantic score (>0.5) boosts the candidate, a low one demotes it,
        # while a neutral 0.5 leaves the text+vec magnitude untouched. This fixes
        # ordering (当日访问用户数 ↑, 分享页访问用户数 ↓) without halving the
        # absolute relevance and accidentally dropping the true match below the
        # relevance threshold.
        blended += CFG.rerank_score_weight * (rr - 0.5)
    # Domain bonus/penalty: nudge same-domain anchors up and cross-domain down.
    if domain:
        anchor_domain = getattr(anchor, "domain", "") or ""
        if anchor_domain and anchor_domain.lower() == domain.lower():
            blended += 0.05
        elif anchor_domain and anchor_domain.lower() != domain.lower():
            blended -= 0.10
    return max(0.0, min(1.0, blended))


def rank_metric_anchors(
    query: str,
    anchors: Any,
    *,
    domain: str = "",
) -> list[tuple[Any, float]]:
    """Rank ``anchors.anchors_metric`` by blended relevance, descending.

    Empty bucket → empty list. Duplicates are not collapsed here (dedup by key
    happens upstream in the pipeline's anchor resolution).
    """
    scored: list[tuple[Any, float]] = []
    for a in getattr(anchors, "anchors_metric", []) or []:
        if not getattr(a, "key", ""):
            continue
        scored.append((a, score_anchor(query, a, domain=domain)))
    scored.sort(key=lambda x: -x[1])
    return scored


def rank_knowledge_anchors(query: str, anchors: Any) -> list[tuple[Any, float]]:
    """Rank ``anchors.anchors_knowledge`` by blended relevance."""
    scored: list[tuple[Any, float]] = []
    for a in getattr(anchors, "anchors_knowledge", []) or []:
        if not getattr(a, "key", ""):
            continue
        scored.append((a, score_anchor(query, a)))
    scored.sort(key=lambda x: -x[1])
    return scored


# ---------------------------------------------------------------------- #
# MetricFocus — full semantic card (primary L1 + explore_entity core)
# ---------------------------------------------------------------------- #

def _node_props_map(expanded: dict[str, Any], groups: tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Build {node_id: props} for nodes whose group is in ``groups``."""
    out: dict[str, dict[str, Any]] = {}
    for n in expanded.get("nodes", []) or []:
        if not isinstance(n, dict):
            continue
        if n.get("group") not in groups:
            continue
        out[str(n.get("id") or "")] = dict(n.get("props") or {})
    return out


def _build_source_columns(
    expanded: dict[str, Any],
    cap: int,
) -> list[SourceColumn]:
    """Cap-capped source columns from USES_COLUMN edges + Column node props."""
    col_props = _node_props_map(expanded, ("Column", "DatasetColumn"))
    seen: set[tuple[str, str]] = set()
    out: list[SourceColumn] = []

    for e in expanded.get("edges", []) or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("type") or e.get("label")) != "USES_COLUMN":
            continue
        col_key = str(e.get("to") or "")
        role = str((e.get("props") or {}).get("role") or "")
        if not col_key or (col_key, role) in seen:
            continue
        seen.add((col_key, role))
        props = col_props.get(col_key, {})
        phys = str(props.get("name") or col_key.rsplit(".", 1)[-1])
        dataset = str(props.get("table") or "")
        out.append(SourceColumn(
            name=phys,
            dataset=dataset,
            role=role or "measure",
            granularity_role=str(props.get("granularity_role") or ""),
            topline_value=str(props.get("topline_value") or ""),
        ))
        if len(out) >= cap:
            break
    return out


def _build_drill_dimensions(expanded: dict[str, Any], cap: int) -> list[DrillDimension]:
    """Cap-capped drill dims from ``raw.drill_dims``."""
    raw = expanded.get("raw", {}) or {}
    out: list[DrillDimension] = []
    seen: set[str] = set()
    for dr in raw.get("drill_dims", []) or []:
        if not isinstance(dr, dict):
            continue
        dim = dr.get("dim") or {}
        key = str(dim.get("key") or dr.get("key") or "")
        name = str(dim.get("name") or dr.get("name") or "")
        if not name or key in seen:
            continue
        seen.add(key)
        dim_type = str(dim.get("dimension_type") or dr.get("dimension_type") or "")
        out.append(DrillDimension(
            name=name,
            relationship=dim_type,
        ))
        if len(out) >= cap:
            break
    return out


def _build_common_filters(expanded: dict[str, Any], cap: int) -> list[CommonFilter]:
    """Cap-capped calibers / business rules from ``raw.calibers``."""
    raw = expanded.get("raw", {}) or {}
    out: list[CommonFilter] = []
    seen: set[str] = set()
    for cal in raw.get("calibers", []) or []:
        if not isinstance(cal, dict):
            continue
        props = dict(cal.get("props") or {})
        rule = str(props.get("description") or props.get("filter_expr") or "")
        if not rule or rule in seen:
            continue
        seen.add(rule)
        out.append(CommonFilter(description=rule, sql_fragment=""))
        if len(out) >= cap:
            break
    return out


def _extract_caliber(expanded: dict[str, Any]) -> str:
    """Pick formula_evidence or first formula expression as the caliber."""
    raw = expanded.get("raw", {}) or {}
    for fr in raw.get("formulas", []) or []:
        if not isinstance(fr, dict):
            continue
        ev = str(fr.get("formula_evidence") or "").strip()
        if ev:
            return ev
    for fr in raw.get("formulas", []) or []:
        if not isinstance(fr, dict):
            continue
        expr = str(fr.get("formula") or fr.get("expression") or "").strip()
        if expr:
            return expr
    return ""


def build_metric_focus(
    query: str,
    anchor: Any,
    expanded_subgraph: dict[str, Any],
    *,
    relevance_score: Optional[float] = None,
) -> MetricFocus:
    """Full semantic card from an anchor + its expanded subgraph (caps enforced)."""
    name, aliases, description, vec = _anchor_fields(anchor)
    if relevance_score is None:
        relevance_score = score_anchor(query, anchor)

    center = expanded_subgraph.get("center") if isinstance(expanded_subgraph, dict) else None
    center_props: dict[str, Any] = {}
    if isinstance(center, dict):
        center_props = center
    elif isinstance(center, str) and center:
        center_props = {"key": center, "name": center}

    raw = (expanded_subgraph.get("raw") or {}) if isinstance(expanded_subgraph, dict) else {}
    metric_props = dict(raw.get("metric") or {})

    unit = str(center_props.get("unit") or metric_props.get("unit") or "")
    role = str(center_props.get("role") or metric_props.get("role") or "")
    desc = str(center_props.get("description") or metric_props.get("description") or description)
    als = list(center_props.get("aliases") or metric_props.get("aliases") or aliases or [])

    return MetricFocus(
        metric_name=str(center_props.get("name") or name or ""),
        aliases=als,
        role=role,
        unit=unit,
        description=desc,
        caliber=_extract_caliber(expanded_subgraph),
        source_columns=_build_source_columns(
            expanded_subgraph, CFG.pack_max_source_columns,
        ),
        drill_dimensions=_build_drill_dimensions(
            expanded_subgraph, CFG.pack_max_drill_dimensions,
        ),
        common_filters=_build_common_filters(
            expanded_subgraph, CFG.pack_max_common_filters,
        ),
        relevance_score=round(relevance_score, 3),
    )


# ---------------------------------------------------------------------- #
# MetricCandidate — brief alt (alternatives list + ambiguity)
# ---------------------------------------------------------------------- #

def build_metric_candidate(
    query: str,
    anchor: Any,
    *,
    relevance_score: Optional[float] = None,
) -> MetricCandidate:
    """Brief reference for non-primary metric hits."""
    name, aliases, description, _vec = _anchor_fields(anchor)
    if relevance_score is None:
        relevance_score = score_anchor(query, anchor)
    als_str = "、".join(aliases[:3]) if aliases else ""
    hint = ""
    if als_str:
        hint = f"别名: {als_str}"
    return MetricCandidate(
        metric_name=str(name or ""),
        description=str(description or "")[:300],
        relevance_score=round(relevance_score, 3),
        disambiguation_hint=hint,
    )


# ---------------------------------------------------------------------- #
# KnowledgeNote — threshold-gated knowledge snippet
# ---------------------------------------------------------------------- #

def build_knowledge_notes(
    query: str,
    anchors_knowledge: list[Any],
    *,
    max_items: Optional[int] = None,
    summary_chars: Optional[int] = None,
    threshold: Optional[float] = None,
) -> list[KnowledgeNote]:
    """Rank + cap knowledge anchors; drop those below ``relevance_threshold``.
    
    Accepts both objects with attributes (AnchorNode) and dicts.
    """
    cap = max(0, max_items if max_items is not None else CFG.pack_max_knowledge)
    if cap <= 0:
        return []
    sc = summary_chars if summary_chars is not None else CFG.pack_knowledge_summary_chars
    th = threshold if threshold is not None else CFG.relevance_threshold

    out: list[KnowledgeNote] = []
    seen: set[str] = set()
    for a in anchors_knowledge or []:
        # Support both dicts and objects with attributes
        if isinstance(a, dict):
            k = str(a.get("key", "") or "")
            name = str(a.get("name", "") or "")
            desc = str(a.get("description", "") or "")
            vec = float(a.get("vec_score", 0.0) or 0.0)
            label = str(a.get("label", "Entity") or "Entity")
        else:
            k = str(getattr(a, "key", "") or "")
            name = str(getattr(a, "name", "") or "")
            desc = str(getattr(a, "description", "") or "")
            vec = float(getattr(a, "vec_score", 0.0) or 0.0)
            label = str(getattr(a, "label", "Entity") or "Entity")
        
        if not k or k in seen:
            continue
        seen.add(k)
        blended, _text, _vecn = score_candidate(
            query, name=name, description=desc, raw_vec_score=vec,
        )
        if blended < th:
            continue
        out.append(KnowledgeNote(
            label=label,
            name=name,
            summary=desc[:sc],
            relevance_score=round(blended, 3),
        ))
        if len(out) >= cap:
            break
    out.sort(key=lambda x: -x.relevance_score)
    return out


# ---------------------------------------------------------------------- #
# EventCard — relevance-ranked event
# ---------------------------------------------------------------------- #

def build_event_card(
    query: str,
    row: dict[str, Any],
    *,
    summary_chars: Optional[int] = None,
) -> EventCard:
    """Build an :class:`EventCard` from a ``search_events`` row (capped summary)."""
    sc = summary_chars if summary_chars is not None else CFG.pack_event_desc_chars
    desc = str(row.get("description") or "")
    name = str(row.get("name") or "")
    vec = float(row.get("vec_score") or 0.0)
    blended, _text, _vecn = score_candidate(
        query, name=name, description=desc, raw_vec_score=vec,
    )
    return EventCard(
        name=name,
        type=str(row.get("type") or ""),
        scope=str(row.get("scope") or ""),
        date_from=str(row.get("date_from") or ""),
        date_to=str(row.get("date_to") or ""),
        summary=desc[:sc],
        relevance_score=round(blended, 3),
        about_entity_name=str(row.get("about_entity_name") or ""),
    )


def build_event_cards(
    query: str,
    rows: list[dict[str, Any]],
    *,
    max_items: Optional[int] = None,
) -> list[EventCard]:
    """Rank + cap event rows into ``EventCard`` list (relevance desc)."""
    cap = max(1, max_items if max_items is not None else CFG.pack_max_events)
    cards = [build_event_card(query, r) for r in rows]
    cards.sort(key=lambda c: -c.relevance_score)
    return cards[:cap]


# ---------------------------------------------------------------------- #
# Schema prompt — metric-centric NL (used by L1)
# ---------------------------------------------------------------------- #

def render_schema_prompt(
    primary: list[MetricFocus],
    alternatives: list[MetricCandidate],
) -> str:
    """Build a compact metric-centric ``schema_prompt`` string.

    Only the primary metrics contribute their columns / dims / filters.
    Alternatives get a single tail line naming them.
    """
    if not primary:
        return ""
    sections: list[str] = []
    for m in primary:
        parts: list[str] = [f"指标: {m.metric_name}"]
        if m.description:
            parts.append(f"  含义: {m.description[:200]}")
        if m.caliber:
            parts.append(f"  口径: {m.caliber[:200]}")
        if m.aliases:
            parts.append(f"  别名: {', '.join(m.aliases[:5])}")
        if m.unit:
            parts.append(f"  单位: {m.unit}")
        if m.source_columns:
            col_lines: list[str] = []
            for c in m.source_columns:
                line = f"  · {c.name}"
                if c.dataset:
                    line += f" ({c.dataset})"
                tags: list[str] = []
                if c.granularity_role:
                    tags.append(f"role={c.granularity_role}")
                if c.topline_value:
                    tags.append(f"topline='{c.topline_value}'")
                if c.role and c.role != "measure":
                    tags.append(c.role)
                if tags:
                    line += f" [{' '.join(tags)}]"
                col_lines.append(line)
            parts.append("  相关列:")
            parts.extend(col_lines)
        if m.drill_dimensions:
            dim_names = ", ".join(d.name for d in m.drill_dimensions)
            parts.append(f"  可下钻维度: {dim_names}")
        if m.common_filters:
            cf = "; ".join(c.description for c in m.common_filters if c.description)
            if cf:
                parts.append(f"  常见过滤: {cf[:200]}")
        sections.append("\n".join(parts))

    out = "\n\n".join(sections)
    if alternatives:
        names = ", ".join(c.metric_name for c in alternatives)
        out += f"\n\n其他可能相关指标: {names}"
    return out


# ---------------------------------------------------------------------- #
# Helpers for L1 (primary / alternatives split)
# ---------------------------------------------------------------------- #

def split_primary_alternatives(
    ranked: list[tuple[Any, float]],
    *,
    primary_cap: Optional[int] = None,
    alternative_cap: Optional[int] = None,
    tie_gap: Optional[float] = None,
    floor: Optional[float] = None,
    promote_floor: Optional[float] = None,
) -> tuple[list[tuple[Any, float]], list[tuple[Any, float]]]:
    """Partition ranked metrics into primary + alternatives (above floor).

    Primary cap = ``primary_cap`` (default 1). When ``primary_cap=1`` we only
    promote #2 to a *second primary* (→ the caller flags genuine ambiguity) when
    BOTH of the top two are confident matches: their scores are within ``tie_gap``
    AND #2 itself clears ``promote_floor`` (the relevance threshold). Without the
    threshold guard almost every query promoted a near-tied runner-up — even two
    sub-threshold guesses (e.g. 0.24 vs 0.21) — so ``ambiguous`` was always True.
    A runner-up below threshold is just an *alternative*, not a competing
    interpretation.
    """
    pc = primary_cap if primary_cap is not None else CFG.l1_primary_metrics
    ac = alternative_cap if alternative_cap is not None else CFG.l1_max_alternatives
    gap = tie_gap if tie_gap is not None else CFG.recall_strategy_card_auto_accept_gap
    fl = floor if floor is not None else CFG.relevance_floor
    pf = promote_floor if promote_floor is not None else CFG.relevance_threshold

    above = [(a, s) for (a, s) in ranked if s >= fl]
    if not above:
        return [], []

    primary = above[:pc]
    rest = above[pc:]

    # Tie promotion: only when top-1 AND top-2 are both relevant (≥ pf) and within
    # gap. A sub-threshold runner-up stays an alternative (no false ambiguity).
    if (
        pc == 1
        and len(rest) >= 1
        and primary[0][1] >= pf
        and rest[0][1] >= pf
        and (primary[0][1] - rest[0][1]) <= gap
    ):
        primary.append(rest.pop(0))

    alternatives = rest[:ac]
    return primary, alternatives


__all__ = [
    "build_event_card",
    "build_event_cards",
    "build_knowledge_notes",
    "build_metric_candidate",
    "build_metric_focus",
    "rank_knowledge_anchors",
    "rank_metric_anchors",
    "render_schema_prompt",
    "score_anchor",
    "split_primary_alternatives",
]

"""L1 response shaping — primary + alternatives + knowledge notes + metric-centric schema_prompt.

This module is the single entry point for assembling the L1 (search_context)
response payload. It replaces the old ad-hoc ``_build_schema_prompt`` + metric
ambiguity logic with a structured, capped, relevance-ranked approach built on
the shared semantic-card vocabulary in :mod:`semantic_pack`.

Returns
-------
tuple[MetricFocus, MetricFocus, list[MetricCandidate], list[KnowledgeNote], str]
    - primary_metrics: 1–2 most relevant metrics (full semantic cards)
    - alternative_metrics: next-best matches (brief references)
    - knowledge_notes: threshold-gated knowledge snippets
    - schema_prompt: metric-centric NL built from primary only
"""
from __future__ import annotations

from typing import Any

from ..config import CFG
from .cm_models import KnowledgeNote, MetricCandidate, MetricFocus
from .semantic_pack import (
    build_knowledge_notes,
    build_metric_candidate,
    build_metric_focus,
    rank_knowledge_anchors,
    rank_metric_anchors,
    render_schema_prompt,
    split_primary_alternatives,
)


def shape_l1(
    query: str,
    anchors: Any,
    expanded_subgraphs: dict[str, dict[str, Any]],
    *,
    domain: str = "",
) -> tuple[list[MetricFocus], list[MetricCandidate], list[KnowledgeNote], str]:
    """Shape the L1 response: rank, cap, build cards, render schema_prompt.

    Parameters
    ----------
    query : str
        The user's natural-language query.
    anchors : Any
        The AnchorSet from the pipeline (has ``anchors_metric``, ``anchors_knowledge``).
    expanded_subgraphs : dict[str, dict[str, Any]]
        Mapping of metric_key → expanded subgraph dict (from pipeline).
    domain : str
        Business domain scope (e.g. "ChatApp"). When set, same-domain anchors
        get a ranking bonus and cross-domain anchors are penalised.

    Returns
    -------
    primary_metrics : list[MetricFocus]
        Top 1–2 metrics with full semantic context.
    alternative_metrics : list[MetricCandidate]
        Next-best matches (brief references, up to ``l1_max_alternatives``).
    knowledge_notes : list[KnowledgeNote]
        Threshold-gated knowledge snippets (up to ``l1_knowledge_max``).
    schema_prompt : str
        Metric-centric NL built from primary only (alternatives get a tail line).
    """
    # ── 1. Rank metric anchors by blended relevance ─────────────────────────
    ranked = rank_metric_anchors(query, anchors, domain=domain)
    if not ranked:
        return [], [], [], ""

    # ── 2. Split into primary + alternatives (caps + tie promotion) ─────────
    primary_pairs, alt_pairs = split_primary_alternatives(ranked)

    # ── 3. Build full MetricFocus cards for primary ─────────────────────────
    primary_metrics: list[MetricFocus] = []
    for anchor, score in primary_pairs:
        key = getattr(anchor, "key", "")
        expanded = expanded_subgraphs.get(key, {})
        card = build_metric_focus(query, anchor, expanded, relevance_score=score)
        primary_metrics.append(card)

    # ── 4. Build brief MetricCandidate for alternatives ─────────────────────
    alternative_metrics: list[MetricCandidate] = []
    seen_names: set[str] = set()
    # Deduplicate by metric name: the same metric can appear multiple times
    # in the anchor list via different KG paths (e.g. DAU from both MG and KG).
    # Keep the highest-scored occurrence only.
    for anchor, score in alt_pairs:
        cand = build_metric_candidate(query, anchor, relevance_score=score)
        if cand.metric_name in seen_names:
            continue
        seen_names.add(cand.metric_name)
        alternative_metrics.append(cand)

    # ── 5. Build knowledge notes (threshold-gated) ──────────────────────────
    knowledge_notes = build_knowledge_notes(query, getattr(anchors, "anchors_knowledge", []))

    # ── 6. Render metric-centric schema_prompt ──────────────────────────────
    schema_prompt = render_schema_prompt(primary_metrics, alternative_metrics)

    return primary_metrics, alternative_metrics, knowledge_notes, schema_prompt


__all__ = ["shape_l1"]

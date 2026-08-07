"""Pipeline & assembly trace builders for CM Context API transparency."""
from __future__ import annotations

from typing import Any, Optional


def _anchor_rows(anchors: Any, *, limit: int = 12) -> list[dict[str, Any]]:
    rows = []
    for a in sorted(
        getattr(anchors, "anchors", []) or [],
        key=lambda x: -float(getattr(x, "score", 0.0)),
    )[:limit]:
        rows.append({
            "key": getattr(a, "key", ""),
            "label": getattr(a, "label", ""),
            "name": getattr(a, "name", "") or "",
            "score": round(float(getattr(a, "score", 0.0)), 4),
        })
    return rows


def anchor_bucket_counts(anchors: Any) -> dict[str, int]:
    return {
        "metric": len(getattr(anchors, "anchors_metric", []) or []),
        "dimension": len(getattr(anchors, "anchors_dimension", []) or []),
        "column": len(getattr(anchors, "anchors_column", []) or []),
        "knowledge": len(getattr(anchors, "anchors_knowledge", []) or []),
        "claim": len(getattr(anchors, "anchors_claim", []) or []),
        "total": len(getattr(anchors, "anchors", []) or []),
    }


def cards_trace_summary(
    recalled: int,
    visible: list[dict],
    blocked: list[dict],
    gate: dict[str, Any],
) -> dict[str, Any]:
    top = sorted(visible, key=lambda c: -float(c.get("composite_score") or 0))[:5]
    return {
        "recalled": recalled,
        "visible": len(visible),
        "blocked": len(blocked),
        "auto_accept": bool((gate or {}).get("auto_accept")),
        "top_card_key": (gate or {}).get("top_card"),
        "top_cards": [
            {
                "key": c.get("key"),
                "polarity": c.get("polarity"),
                "score": round(float(c.get("composite_score") or 0), 4),
                "task_type": c.get("task_type"),
            }
            for c in top
        ],
    }


def subgraph_trace_summary(sg: Any) -> dict[str, Any]:
    if not sg:
        return {"method": "none", "nodes": 0, "edges": 0, "by_label": {}}
    nbl = dict(getattr(sg, "nodes_by_label", {}) or {})
    by_label = {k: len(v) if isinstance(v, list) else 0 for k, v in nbl.items()}
    return {
        "method": getattr(sg, "method", ""),
        "nodes": len(getattr(sg, "node_keys", []) or []),
        "edges": len(getattr(sg, "edges", []) or []),
        "by_label": by_label,
    }


def decision_trace_summary(dec: Any) -> dict[str, Any]:
    if not dec:
        return {}
    return {
        "task_type": getattr(dec, "task_type", ""),
        "reuse_key": getattr(dec, "reuse_key", None),
        "card_confidence": round(float(getattr(dec, "card_confidence", 0) or 0), 4),
        "card_reason": (getattr(dec, "card_reason", "") or "")[:300],
        "negative_hints_n": len(getattr(dec, "negative_hints", []) or []),
        "llm_calls": getattr(dec, "llm_calls", 0),
    }


def build_assembly_trace(
    *,
    snapshot: Any,
    metrics_n: int,
    dimensions_n: int,
    columns_n: int,
    tables_n: int,
    knowledge_n: int,
    business_rules_n: int,
    cards_n: int,
    ambiguity_n: int,
) -> list[dict[str, Any]]:
    """How ContextPack fields were stitched from snapshot internals."""
    exp_keys = list((getattr(snapshot, "expanded_subgraphs", None) or {}).keys())
    steps: list[dict[str, Any]] = [
        {
            "target": "semantics.metrics",
            "sources": [
                "snapshot.anchors.anchors_metric",
                f"snapshot.expanded_subgraphs[{len(exp_keys)} keys] → formula/caliber",
            ],
            "output": f"{metrics_n} MetricBrief（含 definition / business_rules 抽取）",
            "detail": {"expanded_metric_keys": exp_keys[:8]},
        },
        {
            "target": "semantics.dimensions",
            "sources": ["snapshot.anchors.anchors_dimension"],
            "output": f"{dimensions_n} DimensionBrief",
        },
        {
            "target": "semantics.columns",
            "sources": ["snapshot.anchors.anchors_column"],
            "output": f"{columns_n} ColumnBrief",
        },
        {
            "target": "semantics.tables",
            "sources": ["snapshot.subgraph.nodes_by_label['Table']"],
            "output": f"{tables_n} TableBrief",
        },
        {
            "target": "semantics.knowledge",
            "sources": ["snapshot.anchors.anchors_knowledge (Entity/Event)"],
            "output": f"{knowledge_n} KnowledgeSnippet",
        },
        {
            "target": "semantics.business_rules",
            "sources": ["expanded_subgraphs.raw.calibers → filter_expr / description"],
            "output": f"{business_rules_n} 条口径规则",
        },
        {
            "target": "experience.cards",
            "sources": [
                "snapshot.cards_visible（strategy_card gate 后）",
                "snapshot.cards_blocked（仅 debug 统计）",
            ],
            "output": f"{cards_n} CardBrief + negative_hints + ExperienceStats",
            "detail": {
                "blocked_n": len(getattr(snapshot, "cards_blocked", []) or []),
                "gate": getattr(snapshot, "top_card_gate", {}) or {},
            },
        },
        {
            "target": "ambiguity_candidates",
            "sources": ["启发式：metrics≥2 → aspect=metric 澄清候选"],
            "output": f"{ambiguity_n} AmbiguityItem",
        },
        {
            "target": "time_hints",
            "sources": ["snapshot.anchors.time_hints（时间解析）"],
            "output": str(getattr(snapshot.anchors, "time_hints", []) or [])[:200],
        },
    ]
    return steps


def build_operation_trace(
    *,
    endpoint: str,
    pipeline_steps: Optional[list[dict[str, Any]]] = None,
    assembly_steps: Optional[list[dict[str, Any]]] = None,
    extras: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    total_ms = sum(int(s.get("ms") or 0) for s in (pipeline_steps or []))
    return {
        "endpoint": endpoint,
        "total_pipeline_ms": total_ms,
        "pipeline": pipeline_steps or [],
        "assembly": assembly_steps or [],
        "extras": extras or {},
    }

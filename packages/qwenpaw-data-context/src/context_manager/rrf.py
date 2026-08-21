"""Reciprocal Rank Fusion (RRF) for merging ordered retrieval lists."""
from __future__ import annotations

from typing import Any

RRF_K = 60  # classic RRF smoothing constant


def rrf_merge(
    rankings: list[list[dict[str, Any]]],
    *,
    key_field: str = "key",
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[dict[str, Any]]:
    """RRF: each ranking contributes ``w / (k + rank)`` (rank 0-based).

    ``weights`` is an optional per-ranking multiplier list (same length as
    ``rankings``). When ``None``, all rankings are weighted 1.0 (classic RRF).

    Returns rows sorted by fused score descending. First-seen row supplies base fields;
    ``score`` is the fused sum; ``rrf_components`` lists
    ``[{source: idx, rank, raw}]`` per contributing stream.
    """
    fused: dict[Any, dict[str, Any]] = {}
    for src_idx, ranking in enumerate(rankings):
        if not ranking:
            continue
        w = 1.0 if weights is None else float(weights[src_idx])
        for rank, item in enumerate(ranking):
            key = item.get(key_field)
            if key is None:
                continue
            increment = w / (k + rank)
            if key not in fused:
                fused[key] = {**item, "score": 0.0, "rrf_components": []}
            fused[key]["score"] += increment
            fused[key]["rrf_components"].append(
                {"source": src_idx, "rank": rank, "raw": item.get("score")}
            )
    return sorted(
        fused.values(),
        key=lambda x: (-x["score"], -x.get("source_trust", 1.0)),
    )

"""Single-turn retrieval preprocessing for the L1 front path (``search_context``).

The chat ``query_rewrite`` only fires on multi-turn (it skips when there is no
follow-up context), and the full ``run_topology_pipeline`` entity-expansion is not
on the ``_run_pipeline_front`` path used by ``cm_search_context``. So a fresh
single-turn question went straight to anchor recall as one raw string — meaning
"ChatApp 访问趋势分析" never produced an alias line like "活跃用户 / DAU / 访问用户数"
for the metric index to hit.

This module bridges that gap: one LLM call extracts entities + catalog-oriented
alias phrases and returns extra anchor-recall lines. On any skip/failure it falls
back to ``[query]`` so callers are always safe.
"""
from __future__ import annotations

from typing import Any, Optional

from ..config import CFG
from ..openai_client import resolve_llm_model
from ..utils import get_logger

log = get_logger("runtime.retrieval_preprocess")

_MAX_RECALL_QUERIES = 22


def _ordered_unique(parts: list[str], *, max_queries: int = _MAX_RECALL_QUERIES) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        s = str(p or "").strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= max_queries:
            break
    return out


def build_anchor_recall_queries(
    query: str,
    *,
    model: Optional[str] = None,
    enabled: Optional[bool] = None,
    domain: str = "",
) -> tuple[list[str], dict[str, Any]]:
    """Return ``(recall_queries, meta)`` for single-turn anchor recall.

    ``recall_queries[0]`` is always the original query (so the literal phrasing is
    still searched); entity/alias lines follow. Disabled or failed expansion just
    returns ``([query], meta)``.
    """
    q = (query or "").strip()
    meta: dict[str, Any] = {"enabled": False, "llm_called": False, "recall_queries": []}
    if not q:
        return [], meta

    use = CFG.llm_retrieval_entity_expand if enabled is None else bool(enabled)
    meta["enabled"] = bool(use)
    if not use:
        return [q], meta

    try:
        from .retrieval_entity_expand import expand_entities_for_anchor_recall

        recall_queries, ee_meta = expand_entities_for_anchor_recall(
            primary_nl=q,
            # Entity/alias extraction is a light templated task; an explicit
            # ``model`` still wins, otherwise fall back to the main LLM model.
            model=resolve_llm_model(model),
            domain=domain,
        )
        meta.update(ee_meta or {})
        merged = _ordered_unique([q, *(recall_queries or [])])
        meta["recall_queries"] = merged
        # Collect extracted entity names for exact-match boosting in anchor recall
        entities = ee_meta.get("entities") or [] if ee_meta else []
        meta["exact_match_terms"] = {
            ent["name"] for ent in entities if ent.get("name")
        }
        return (merged or [q]), meta
    except Exception as exc:  # noqa: BLE001
        log.warning("build_anchor_recall_queries failed: %s", exc)
        meta["error"] = str(exc)
        return [q], meta

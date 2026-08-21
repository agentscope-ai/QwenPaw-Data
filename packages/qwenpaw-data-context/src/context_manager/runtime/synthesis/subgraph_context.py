"""Serialize a ContextPack (the distilled multi-hop subgraph) into compact,
embedding-free, MG/TG/KG-grouped text for interface-level synthesis prompts.
"""
from __future__ import annotations

from typing import Any, Optional


def _g(obj: Any, attr: str, default: str = "") -> str:
    v = getattr(obj, attr, default)
    return "" if v is None else str(v)


def _mg_lines(sem: Any, *, max_metrics: int, max_cols: int, max_tables: int) -> list[str]:
    lines: list[str] = []
    for m in (getattr(sem, "metrics", []) or [])[:max_metrics]:
        parts = [f"指标 {_g(m, 'name')}"]
        if _g(m, "definition"):
            parts.append(_g(m, "definition"))
        lines.append("[MG] " + ": ".join(parts))
    for t in (getattr(sem, "tables", []) or [])[:max_tables]:
        lines.append(f"[MG] 表 {_g(t, 'name')}")
    for c in (getattr(sem, "columns", []) or [])[:max_cols]:
        tags = []
        if _g(c, "granularity_role"):
            tags.append(f"role={_g(c, 'granularity_role')}")
        if _g(c, "topline_value"):
            tags.append(f"topline='{_g(c, 'topline_value')}'")
        tag = f" [{'; '.join(tags)}]" if tags else ""
        lines.append(f"[MG] 列 {_g(c, 'name')} (table={_g(c, 'table')}){tag} {_g(c, 'comment')}".rstrip())
    for r in (getattr(sem, "relations_nl", []) or [])[:max_tables]:
        lines.append(f"[MG] 关系 {r}")
    return lines


def _tg_lines(exp: Any, *, max_cards: int) -> list[str]:
    lines: list[str] = []
    for card in (getattr(exp, "cards", []) or [])[:max_cards]:
        lesson = _g(card, "lesson") or _g(card, "strategy_semantics")
        if not lesson:
            continue
        polarity = _g(card, "polarity") or "positive"
        lines.append(f"[TG] 经验({polarity}): {lesson}")
    return lines


def _kg_lines(sem: Any, *, max_items: int) -> list[str]:
    lines: list[str] = []
    for k in (getattr(sem, "knowledge", []) or [])[:max_items]:
        summary = _g(k, "summary") or _g(k, "name")
        if summary:
            lines.append(f"[KG] 知识({_g(k, 'label') or 'Entity'}): {summary}")
    for rule in (getattr(sem, "business_rules", []) or [])[:max_items]:
        lines.append(f"[KG] 约束: {rule}")
    return lines


def subgraph_to_llm_context(
    pack: Any,
    *,
    center_key: Optional[str] = None,
    max_metrics: int = 6,
    max_cols: int = 16,
    max_tables: int = 6,
    max_cards: int = 4,
    max_kg: int = 6,
) -> str:
    """ContextPack -> compact MG/TG/KG text. Embedding fields are never emitted
    (only the whitelisted semantic attrs are read). Returns "" when empty.

    center_key: when given, MG metrics are reordered so the centered entity (by
    key) comes first.
    """
    sem = getattr(pack, "semantics", None)
    exp = getattr(pack, "experience", None)
    if sem is None:
        return ""

    if center_key:
        metrics = list(getattr(sem, "metrics", []) or [])
        metrics.sort(key=lambda m: 0 if _g(m, "key") == center_key else 1)
        try:
            sem.metrics = metrics
        except Exception:
            pass

    lines: list[str] = []
    lines += _mg_lines(sem, max_metrics=max_metrics, max_cols=max_cols, max_tables=max_tables)
    if exp is not None:
        lines += _tg_lines(exp, max_cards=max_cards)
    lines += _kg_lines(sem, max_items=max_kg)
    return "\n".join(lines)

"""Layered conflict screener — RRF hybrid retrieval + LLM arbitration.

Write-time screening for Entity / Event nodes:

  Layer 0  key 精确匹配          (KnowledgeWriter 已有, ms 级)
  Layer 1  fulltext+vector RRF   (本模块, ms 级)
  Layer 2  LLM 裁决              (本模块, s 级, 仅高相似度触发)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from ..knowledge.resolve import _fulltext_top, _vector_top
from ..openai_client import complete_json
from ..utils import get_logger

log = get_logger("conflict.screener")

# ------------------------------------------------------------------ #
# Tuning constants
# ------------------------------------------------------------------ #
RRF_K: int = 60
SCREEN_THRESHOLD: float = 0.70
LLM_TRIGGER_THRESHOLD: float = 0.88
AUTO_MERGE_CONFIDENCE: float = 0.80

# label → (fulltext index, vector index)  — aligned with schema_init.py
_SCREEN_INDEXES: dict[str, dict[str, str]] = {
    "Entity": {"ft": "entity_text", "vec": "ent_vec"},
    "Event": {"ft": "event_text", "vec": "ev_vec"},
}

# ------------------------------------------------------------------ #
# Data classes
# ------------------------------------------------------------------ #

@dataclass
class ScreenHit:
    key: str
    rrf_score: float
    label: str = ""


@dataclass
class JudgeVerdict:
    decision: str  # SAME_ENTITY | FACTUAL_CONFLICT | DIFFERENT
    reason: str = ""
    confidence: float = 0.0


@dataclass
class ScreenResult:
    action: str  # CLEAR | MAYBE_DUP | MERGE | CONFLICT
    hit: Optional[ScreenHit] = None
    verdict: Optional[JudgeVerdict] = None


# ------------------------------------------------------------------ #
# RRF merge
# ------------------------------------------------------------------ #

def rrf_merge(
    ft_rows: list[dict[str, Any]],
    vec_rows: list[dict[str, Any]],
    *,
    k: int = RRF_K,
) -> list[dict[str, Any]]:
    """Reciprocal Rank Fusion of fulltext and vector result lists.

    Returns ``[{"key": ..., "rrf_score": ...}, ...]`` sorted descending.
    """
    scores: dict[str, float] = {}
    for rank, r in enumerate(ft_rows):
        key = str(r.get("key") or "")
        if key:
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    for rank, r in enumerate(vec_rows):
        key = str(r.get("key") or "")
        if key:
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    return [{"key": key, "rrf_score": score} for key, score in ranked]


# ------------------------------------------------------------------ #
# LLM judge prompt
# ------------------------------------------------------------------ #

_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["SAME_ENTITY", "FACTUAL_CONFLICT", "DIFFERENT"],
        },
        "reason": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["verdict", "reason", "confidence"],
}

_JUDGE_SYSTEM = (
    "你是知识图谱冲突裁决员。判断【新实体】和【已有实体】是否指同一事物。\n"
    "输出 JSON，字段：verdict (SAME_ENTITY / FACTUAL_CONFLICT / DIFFERENT)，"
    "reason (一句话)，confidence (0-1)。\n\n"
    "判定标准：\n"
    "- SAME_ENTITY：完全是同一事物，应合并。\n"
    "- FACTUAL_CONFLICT：同一事物但关键属性矛盾（如公式、定义不同）。\n"
    "- DIFFERENT：不同事物，恰好描述相似。"
)


def _build_judge_user(incoming: dict, existing: dict) -> str:
    def _fmt(props: dict) -> str:
        parts = []
        for f in ("name", "canonical_name", "type", "aliases", "description"):
            v = props.get(f)
            if v:
                parts.append(f"{f}: {v}")
        return "\n".join(parts) or "(empty)"

    return (
        f"【新实体】\n{_fmt(incoming)}\n\n"
        f"【已有实体】\n{_fmt(existing)}"
    )


# ------------------------------------------------------------------ #
# Fetch existing node properties
# ------------------------------------------------------------------ #

_FETCH_PROPS_CYPHER = (
    "MATCH (n {{key: $key}}) WHERE $label IN labels(n) "
    "RETURN properties(n) AS props"
)


def _fetch_node_props(driver: Any, key: str, label: str) -> Optional[dict]:
    from ..utils import neo4j_session  # deferred to avoid circular at module level
    try:
        with neo4j_session(driver) as s:
            rec = s.run(_FETCH_PROPS_CYPHER, key=key, label=label).single()
        if rec is None:
            return None
        return dict(rec["props"])
    except Exception as exc:  # noqa: BLE001
        log.warning("_fetch_node_props failed key=%s: %s", key, exc)
        return None


# ------------------------------------------------------------------ #
# ConflictScreener
# ------------------------------------------------------------------ #

class ConflictScreener:
    """Layered conflict screener for Entity / Event writes."""

    def __init__(
        self,
        *,
        screen_threshold: float = SCREEN_THRESHOLD,
        llm_trigger_threshold: float = LLM_TRIGGER_THRESHOLD,
        auto_merge_confidence: float = AUTO_MERGE_CONFIDENCE,
        rrf_k: int = RRF_K,
        llm_model: str | None = None,
    ) -> None:
        self.screen_threshold = screen_threshold
        self.llm_trigger_threshold = llm_trigger_threshold
        self.auto_merge_confidence = auto_merge_confidence
        self.rrf_k = rrf_k
        self.llm_model = llm_model

    # -------------------------------------------------------------- #
    # Layer 1: RRF hybrid retrieval
    # -------------------------------------------------------------- #

    def screen(
        self,
        driver: Any,
        label: str,
        properties: dict,
        *,
        exclude_key: str = "",
    ) -> list[ScreenHit]:
        indexes = _SCREEN_INDEXES.get(label)
        if not indexes:
            return []

        text = self._screen_text(properties)
        if not text:
            return []

        ft_rows = _fulltext_top(driver, indexes["ft"], text, k=10)
        vec_rows = _vector_top(driver, indexes["vec"], text, k=10)

        merged = rrf_merge(ft_rows, vec_rows, k=self.rrf_k)

        hits = []
        for r in merged:
            if r["key"] == exclude_key:
                continue
            if r["rrf_score"] < self.screen_threshold:
                break  # sorted descending — rest is below threshold
            hits.append(ScreenHit(key=r["key"], rrf_score=r["rrf_score"], label=label))
        return hits

    # -------------------------------------------------------------- #
    # Layer 2: LLM judge
    # -------------------------------------------------------------- #

    def llm_judge(
        self, incoming: dict, existing: dict
    ) -> JudgeVerdict:
        messages = [
            {"role": "system", "content": _JUDGE_SYSTEM},
            {"role": "user", "content": _build_judge_user(incoming, existing)},
        ]
        try:
            parsed = complete_json(
                messages,
                json_schema=_JUDGE_SCHEMA,
                model=self.llm_model,
                max_retries=1,
                temperature=0.0,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_judge failed: %s", exc)
            return JudgeVerdict(decision="DIFFERENT", reason=f"llm error: {exc}", confidence=0.0)

        return JudgeVerdict(
            decision=str(parsed.get("verdict") or "DIFFERENT"),
            reason=str(parsed.get("reason") or ""),
            confidence=float(parsed.get("confidence") or 0.0),
        )

    # -------------------------------------------------------------- #
    # Combined: screen → judge → action
    # -------------------------------------------------------------- #

    def screen_and_judge(
        self,
        driver: Any,
        label: str,
        properties: dict,
        *,
        exclude_key: str = "",
    ) -> ScreenResult:
        hits = self.screen(driver, label, properties, exclude_key=exclude_key)
        if not hits:
            return ScreenResult(action="CLEAR")

        top = hits[0]

        if top.rrf_score < self.llm_trigger_threshold:
            return ScreenResult(action="MAYBE_DUP", hit=top)

        existing_props = _fetch_node_props(driver, top.key, label)
        if existing_props is None:
            return ScreenResult(action="MAYBE_DUP", hit=top)

        verdict = self.llm_judge(properties, existing_props)

        if verdict.decision == "SAME_ENTITY":
            if verdict.confidence >= self.auto_merge_confidence:
                return ScreenResult(action="MERGE", hit=top, verdict=verdict)
            return ScreenResult(action="MAYBE_DUP", hit=top, verdict=verdict)

        if verdict.decision == "FACTUAL_CONFLICT":
            return ScreenResult(action="CONFLICT", hit=top, verdict=verdict)

        # DIFFERENT — no conflict
        return ScreenResult(action="CLEAR", hit=top, verdict=verdict)

    # -------------------------------------------------------------- #
    # Helpers
    # -------------------------------------------------------------- #

    @staticmethod
    def _screen_text(properties: dict) -> str:
        parts = []
        for f in ("name", "canonical_name", "aliases", "description"):
            v = properties.get(f)
            if v:
                if isinstance(v, list):
                    parts.append(" ".join(str(x) for x in v))
                else:
                    parts.append(str(v))
        return " ".join(parts)


__all__ = [
    "ConflictScreener",
    "JudgeVerdict",
    "ScreenHit",
    "ScreenResult",
    "rrf_merge",
]

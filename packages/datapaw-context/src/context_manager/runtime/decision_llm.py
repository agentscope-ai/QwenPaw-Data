"""Step 3: Decision LLM — task_type + card 仲裁；可与图路径合并为单次调用。

候选边由 :func:`context_manager.runtime.path_pick_llm.gather_candidate_edges` 从锚点 BFS 邻域拉取
（不筛关系类型）；具体边由 :func:`decide_with_path` 或 :func:`decide_with_path_react`
在同一次或多轮 JSON 里输出，并由 ``path_pick_llm.resolve_paths_steps`` 校验。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from neo4j import Driver

from ..config import CFG
from ..openai_client import complete_json
from ..utils import get_logger
from .anchors import AnchorSet
from ..config import TraversalEdgeDirection
from .path_pick_llm import (
    CandidateEdge,
    format_candidate_edge_lines,
    gather_out_edges_from_keys,
    merge_edges_dedupe,
    normalize_llm_paths_payload,
    resolve_paths_steps,
    traversal_edge_direction_label,
)
from .prompts import (
    DECISION_LLM_SYSTEM,
    DECISION_LLM_FEW_SHOT_USER,
    DECISION_LLM_FEW_SHOT_ASSISTANT,
    DECISION_LLM_JSON_SCHEMA,
    DECISION_PATH_MERGED_FEW_SHOT_ASSISTANT,
    DECISION_PATH_MERGED_FEW_SHOT_USER,
    DECISION_PATH_MERGED_JSON_SCHEMA,
    DECISION_PATH_MERGED_REACT_SYSTEM,
    DECISION_PATH_MERGED_SYSTEM,
    DECISION_PATH_REACT_JSON_SCHEMA,
    TASK_TYPES,
)

log = get_logger("runtime.decision_llm")


# ---------------------------------------------------------------------- #
# Output dataclass
# ---------------------------------------------------------------------- #

@dataclass
class DecisionOutput:
    task_type: str
    reuse_key: Optional[str]
    card_confidence: float
    card_reason: str
    negative_hints: list[str] = field(default_factory=list)
    llm_calls: int = 1  # for budget tracking


def _decision_output_from_parsed(parsed: dict[str, Any], llm_calls: int) -> DecisionOutput:
    task_type = str(parsed.get("task_type") or "multistep")
    if task_type not in TASK_TYPES:
        task_type = "multistep"
    cd = parsed.get("card_decision") or {}
    reuse_key = cd.get("reuse_key") or None
    confidence = float(cd.get("confidence") or 0.0)
    reason = str(cd.get("reason") or "")
    negative_hints = [str(h) for h in (parsed.get("negative_hints") or [])]
    return DecisionOutput(
        task_type=task_type,
        reuse_key=reuse_key,
        card_confidence=confidence,
        card_reason=reason,
        negative_hints=negative_hints,
        llm_calls=llm_calls,
    )


def _node_keys_from_candidate_edges(edges: list[CandidateEdge]) -> set[str]:
    s: set[str] = set()
    for e in edges:
        if e.from_key:
            s.add(e.from_key)
        if e.to_key:
            s.add(e.to_key)
    return s


def _labels_for_expand(anchors: AnchorSet, edges: list[CandidateEdge]) -> dict[str, str]:
    d: dict[str, str] = {}
    for a in anchors.anchors:
        if a.key:
            d[a.key] = a.label or d.get(a.key, "")
    for e in edges:
        if e.from_key and e.from_label:
            d[e.from_key] = e.from_label
        elif e.from_key:
            d.setdefault(e.from_key, d.get(e.from_key, ""))
        if e.to_key and e.to_label:
            d[e.to_key] = e.to_label
        elif e.to_key:
            d.setdefault(e.to_key, d.get(e.to_key, ""))
    return d


def _expand_fallback_frontier(
    edges: list[CandidateEdge], expanded_from: set[str], limit: int = 12
) -> list[str]:
    from collections import Counter

    outs = Counter(e.from_key for e in edges if e.from_key)
    ranked = [k for k, _ in outs.most_common()]
    return [k for k in ranked if k not in expanded_from][:limit]


# ---------------------------------------------------------------------- #
# Rule-based task_type estimate (used to pre-filter cards before LLM call)
# ---------------------------------------------------------------------- #

_TIME_COMPARE_TOKENS = ("环比", "同比", "dod", "wow", "mom", "yoy", "对比", "上月", "上周", "上季度")
_DRILL_TOKENS = ("按", "分", "拆", "各", "每个", "分组", "拆分")
_RANK_TOKENS = ("top", "top-", "排名", "排行", "最高", "最低", "前", "后")
_ANOMALY_TOKENS = ("异常", "超阈值", "报警", "偏离", "突增", "突降")
_TREND_TOKENS = ("走势", "趋势", "周期", "历史", "连续", "最近", "月均")
_COMPLIANCE_TOKENS = ("手机号", "身份证", "隐私", "pii", "合规", "限制")
_EVENT_TOKENS = ("春节", "国庆", "节假日", "大促", "618", "双11", "发布", "排除节假日")


def estimate_task_type(question: str) -> str:
    """Lightweight rule-based task type estimate for card pre-filtering."""
    q = question.lower()
    if any(t in q for t in _COMPLIANCE_TOKENS):
        return "compliance_check"
    if any(t in q for t in _ANOMALY_TOKENS):
        return "anomaly_detection"
    if any(t in q for t in _EVENT_TOKENS):
        return "event_aligned"
    if any(t in q for t in _TIME_COMPARE_TOKENS):
        return "cross_period_compare"
    if any(t in q for t in _RANK_TOKENS):
        return "ranking_topk"
    if any(t in q for t in _TREND_TOKENS):
        return "trend_analysis"
    if any(t in q for t in _DRILL_TOKENS):
        return "dimensional_drill"
    return "pure_lookup"


# ---------------------------------------------------------------------- #
# Prompt builder
# ---------------------------------------------------------------------- #

def _format_anchor_items(nodes: list[Any], *, limit: int) -> list[str]:
    """Single-line JSON-ish anchor dicts for prompt (shared by bucket / flat layouts)."""
    parts: list[str] = []
    for a in sorted(nodes, key=lambda x: -x.score)[:limit]:
        item = f"{{key: {a.key!r}, label: {a.label!r}, name: {a.name!r}"
        desc = (getattr(a, "description", "") or "").strip()
        syns = list(getattr(a, "aliases", None) or [])
        if syns:
            item += f", aliases: {syns[:6]}"
        if desc:
            item += f", desc: {desc[:80]!r}"
        item += "}"
        parts.append(item)
    return parts


def _format_anchors(anchors: AnchorSet) -> str:
    """Render top anchors with name + a short description blurb so the Decision LLM
    can identify them by *meaning* rather than by raw key.

    When :class:`AnchorSet` has label buckets filled (``resolve_anchors``)，
    按 Metric → Dimension → Column → Knowledge（含 Event / Entity）分段。
    """
    if not anchors.anchors:
        return "(no anchors)"

    am = getattr(anchors, "anchors_metric", None) or []
    ad = getattr(anchors, "anchors_dimension", None) or []
    ac = getattr(anchors, "anchors_column", None) or []
    ak = getattr(anchors, "anchors_knowledge", None) or []

    if any(am or ad or ac or ak):
        sections: list[str] = []
        if am:
            sections.append("Metrics: [" + ", ".join(_format_anchor_items(am, limit=12)) + "]")
        if ad:
            sections.append("Dimensions: [" + ", ".join(_format_anchor_items(ad, limit=10)) + "]")
        if ac:
            sections.append("Columns: [" + ", ".join(_format_anchor_items(ac, limit=10)) + "]")
        if ak:
            sections.append(
                "Knowledge (Event/Entity): ["
                + ", ".join(_format_anchor_items(ak, limit=14))
                + "]"
            )
        if sections:
            return "\n".join(sections)

    parts = _format_anchor_items(anchors.anchors, limit=15)
    return "[" + ", ".join(parts) + "]"


def _format_claims(anchors: AnchorSet) -> str:
    """Render recalled Claims as a separate section for the Decision LLM.

    Claims are experience assertions distilled from past task traces — they
    carry useful semantic mappings, caliber rules, and pitfall warnings that
    may not surface from schema nodes alone.
    """
    claims = getattr(anchors, "anchors_claim", None) or []
    if not claims:
        return "(no recalled claims)"
    lines: list[str] = []
    for a in sorted(claims, key=lambda x: -x.score)[:8]:
        text = (getattr(a, "description", "") or a.name or "").strip()
        if not text:
            continue
        conf = getattr(a, "vec_score", 0.0)
        aliases = getattr(a, "aliases", None) or []
        pred = aliases[0] if aliases else ""
        text_short = text[:200]
        line = f"  - {text_short}"
        if pred:
            line = f"  - [{pred}] {text_short}"
        if conf:
            line += f" (conf={conf:.2f})"
        lines.append(line)
    if not lines:
        return "(no recalled claims)"
    return "\n".join(lines)


def _format_cards(candidate_cards: list[dict]) -> str:
    if not candidate_cards:
        return "(no candidate cards)"
    from ..graph.strategy_card import MIN_HITS_FOR_REUSE

    lines: list[str] = []
    for i, c in enumerate(candidate_cards[:5]):
        key = c.get("key", "")
        task_type = c.get("task_type", "")
        polarity = c.get("polarity", "positive")
        score = round(float(c.get("composite_score", 0)), 3)
        rate = round(float(c.get("success_rate", 0)), 2)
        hits = int(c.get("hit_count", 0))
        try:
            tc = json.loads(c.get("trigger_conditions") or "{}")
            summary = tc.get("question_summary", "")[:60]
        except Exception:
            summary = ""
        lesson = ""
        if polarity in ("avoid", "negative"):
            try:
                tc = json.loads(c.get("trigger_conditions") or "{}")
                lesson = tc.get("lesson", "")[:80]
            except Exception:
                pass
        verified_tag = "" if hits >= MIN_HITS_FOR_REUSE else " [unverified]"
        sem = str(c.get("strategy_semantics") or "").strip().replace("\n", " ")
        if len(sem) > 140:
            sem = sem[:137] + "…"
        line = (
            f"  [{key}] task={task_type}, polarity={polarity}, "
            f"score={score}, rate={rate}, hits={hits}{verified_tag}"
        )
        if summary:
            line += f', summary="{summary}"'
        if lesson:
            line += f', lesson="{lesson}"'
        if sem:
            line += f', strategy="{sem}"'
        lines.append(line)
    return "\n".join(lines)


def _build_user_message(
    question: str,
    anchors: AnchorSet,
    candidate_cards: list[dict],
    ontology_summary: str = "",
    reuse_confirmed: bool = False,
) -> str:
    """Build the user-turn message for the Decision LLM."""
    parts = [
        f'User question: "{question}"',
        f"Entry anchors: {_format_anchors(anchors)}",
        f"Recalled claims (distilled experience assertions):\n{_format_claims(anchors)}",
        f"Candidate cards:\n{_format_cards(candidate_cards)}",
    ]
    if anchors.time_hints:
        parts.append(f"Time hints: {', '.join(anchors.time_hints)}")
    if reuse_confirmed:
        parts.append(
            "Note: the top-1 card exceeded the auto-accept threshold; confirm reuse_key or explain why not."
        )
    if ontology_summary:
        parts.append(f"Ontology addendum: {ontology_summary}")
    return "\n".join(parts)


def _build_merged_user_message(
    question: str,
    anchors: AnchorSet,
    candidate_cards: list[dict],
    *,
    estimated_task_type: str,
    candidate_edges: list[CandidateEdge],
    ontology_summary: str = "",
    reuse_confirmed: bool = False,
    candidate_edge_hops: int = 2,
    max_path_edges: int = 2,
    edge_direction: TraversalEdgeDirection = "out",
) -> str:
    base = _build_user_message(
        question,
        anchors,
        candidate_cards,
        ontology_summary=ontology_summary,
        reuse_confirmed=reuse_confirmed,
    )
    hop_label = traversal_edge_direction_label(edge_direction)
    if candidate_edges:
        ce_block = format_candidate_edge_lines(
            candidate_edges, edge_direction=edge_direction
        )
    else:
        ce_block = "(no candidate edges — expansion returned empty; paths MUST be [])"
    extra = (
        f"Heuristic task_type estimate (classification hint only): {estimated_task_type}\n"
        f"Candidate edges were expanded from entry anchors with ≤{candidate_edge_hops} {hop_label} "
        "(NO filtering by relationship types).\n"
        f"Each contiguous path you emit MUST have ≤{max_path_edges} edges "
        "(≤{max_path_edges + 1} nodes), with ALL steps drawn ONLY from the candidate lines below.\n"
        "Candidate graph edges (grouped by hop index + source node):\n"
        + ce_block
    )
    return f"{base}\n\n{extra}"


def decide_with_path(
    question: str,
    anchors: AnchorSet,
    candidate_cards: list[dict],
    *,
    candidate_edges: list[CandidateEdge],
    estimated_task_type: str,
    candidate_edge_hops: int = 2,
    max_path_edges: int = 2,
    edge_direction: TraversalEdgeDirection = "out",
    model: Optional[str] = None,
    ontology_summary: str = "",
    reuse_confirmed: bool = False,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    reasoning_capture: Optional[List[str]] = None,
    monitor_out: Optional[dict[str, Any]] = None,
    enable_thinking: Optional[bool] = None,
) -> tuple[DecisionOutput, list[list[dict[str, str]]]]:
    """单次 LLM：决策 + 一条或多条具体路径；路径经候选集校验，失败则贪心兜底。"""
    _mr = CFG.agent_decision_max_retries if max_retries is None else max_retries
    _temp = CFG.agent_decision_temperature if temperature is None else temperature
    user_msg = _build_merged_user_message(
        question,
        anchors,
        candidate_cards,
        estimated_task_type=estimated_task_type,
        candidate_edges=candidate_edges,
        ontology_summary=ontology_summary,
        reuse_confirmed=reuse_confirmed,
        candidate_edge_hops=candidate_edge_hops,
        max_path_edges=max_path_edges,
        edge_direction=edge_direction,
    )
    messages = [
        {"role": "system", "content": DECISION_PATH_MERGED_SYSTEM},
        {"role": "user", "content": DECISION_PATH_MERGED_FEW_SHOT_USER},
        {"role": "assistant", "content": DECISION_PATH_MERGED_FEW_SHOT_ASSISTANT},
        {"role": "user", "content": user_msg},
    ]
    llm_meta: dict[str, Any] = {}

    try:
        parsed = complete_json(
            messages,
            json_schema=DECISION_PATH_MERGED_JSON_SCHEMA,
            model=model,
            max_retries=_mr,
            temperature=_temp,
            reasoning_capture=reasoning_capture,
            metadata_out=llm_meta if monitor_out is not None else None,
            enable_thinking=enable_thinking,
        )
        if monitor_out is not None:
            monitor_out.clear()
            monitor_out.update(
                {
                    "role": "decision_path_merged_llm",
                    "messages": messages,
                    "llm": llm_meta,
                    "parsed_json": parsed,
                }
            )

        dec = _decision_output_from_parsed(parsed, 1)

        if dec.reuse_key:
            return dec, []

        raw_paths = normalize_llm_paths_payload(parsed)
        picked_paths = resolve_paths_steps(
            candidate_edges,
            anchors,
            max_path_edges,
            raw_paths,
        )
        return dec, picked_paths

    except Exception as exc:
        log.warning("decide_with_path failed: %s", exc)
        if monitor_out is not None:
            monitor_out.clear()
            monitor_out.update(
                {
                    "role": "decision_path_merged_llm",
                    "messages": messages,
                    "llm": llm_meta,
                    "error": str(exc),
                    "fallback": True,
                }
            )
        estimated = estimate_task_type(question)
        dec = DecisionOutput(
            task_type=estimated,
            reuse_key=None,
            card_confidence=0.0,
            card_reason=f"LLM fallback: {exc}",
            negative_hints=[],
            llm_calls=0,
        )
        picked_paths = resolve_paths_steps(
            candidate_edges,
            anchors,
            max_path_edges,
            [],
        )
        return dec, picked_paths


def decide_with_path_react(
    question: str,
    anchors: AnchorSet,
    candidate_cards: list[dict],
    *,
    driver: Driver,
    candidate_edges: list[CandidateEdge],
    estimated_task_type: str,
    candidate_edge_hops: int,
    max_path_edges: int,
    max_rounds: int,
    expand_new_edges_cap: int,
    max_total_edges: int,
    per_node_limit: int,
    edge_direction: TraversalEdgeDirection = "out",
    model: Optional[str] = None,
    ontology_summary: str = "",
    reuse_confirmed: bool = False,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    reasoning_capture: Optional[List[str]] = None,
    monitor_out: Optional[dict[str, Any]] = None,
    enable_thinking: Optional[bool] = None,
    react_progress_cb: Optional[Callable[[int, dict[str, Any]], None]] = None,
) -> tuple[DecisionOutput, list[list[dict[str, str]]]]:
    """多轮 ReAct：模型可多次 `expand_from_keys` 扩一跳候选边，直到 `exploration_done` 或达轮数上限。"""
    if max_rounds <= 1:
        return decide_with_path(
            question,
            anchors,
            candidate_cards,
            candidate_edges=candidate_edges,
            estimated_task_type=estimated_task_type,
            candidate_edge_hops=candidate_edge_hops,
            max_path_edges=max_path_edges,
            edge_direction=edge_direction,
            model=model,
            ontology_summary=ontology_summary,
            reuse_confirmed=reuse_confirmed,
            max_retries=max_retries,
            temperature=temperature,
            reasoning_capture=reasoning_capture,
            monitor_out=monitor_out,
            enable_thinking=enable_thinking,
        )

    hop_one = traversal_edge_direction_label(edge_direction, plural=False)
    _mr = CFG.agent_decision_max_retries if max_retries is None else max_retries
    _temp = CFG.agent_decision_temperature if temperature is None else temperature

    messages: list[dict[str, str]] = [
        {"role": "system", "content": DECISION_PATH_MERGED_REACT_SYSTEM},
        {"role": "user", "content": DECISION_PATH_MERGED_FEW_SHOT_USER},
        {"role": "assistant", "content": DECISION_PATH_MERGED_FEW_SHOT_ASSISTANT},
    ]
    first_user = (
        _build_merged_user_message(
            question,
            anchors,
            candidate_cards,
            estimated_task_type=estimated_task_type,
            candidate_edges=candidate_edges,
            ontology_summary=ontology_summary,
            reuse_confirmed=reuse_confirmed,
            candidate_edge_hops=candidate_edge_hops,
            max_path_edges=max_path_edges,
            edge_direction=edge_direction,
        )
        + f"\n\n---\nReAct round 1/{max_rounds}: you may set exploration_done=false and expand_from_keys "
        f"(1–16 node keys from the candidate list) to load one more {hop_one} before returning final paths."
    )
    messages.append({"role": "user", "content": first_user})

    expanded_from: set[str] = set()
    seen_triples: set[tuple[str, str, str]] = {
        (e.from_key, e.rel_type, e.to_key) for e in candidate_edges
    }
    round_logs: list[dict[str, Any]] = []
    last_parsed: dict[str, Any] = {}
    total_llm = 0

    try:
        for round_idx in range(1, max_rounds + 1):
            round_meta: dict[str, Any] = {}
            parsed = complete_json(
                messages,
                json_schema=DECISION_PATH_REACT_JSON_SCHEMA,
                model=model,
                max_retries=_mr,
                temperature=_temp,
                reasoning_capture=reasoning_capture,
                metadata_out=round_meta,
                enable_thinking=enable_thinking,
            )
            total_llm += 1
            last_parsed = parsed
            round_logs.append(
                {
                    "round": round_idx,
                    "exploration_done": parsed.get("exploration_done"),
                    "expand_from_keys": parsed.get("expand_from_keys"),
                    "llm": dict(round_meta) if round_meta else {},
                }
            )

            dec_probe = _decision_output_from_parsed(parsed, total_llm)
            if dec_probe.reuse_key:
                if react_progress_cb is not None:
                    try:
                        req_k = parsed.get("expand_from_keys")
                        if not isinstance(req_k, list):
                            req_k = []
                        react_progress_cb(
                            round_idx,
                            {
                                "early_exit": "strategy_card_reuse",
                                "exploration_done": True,
                                "path_reason": str(parsed.get("path_reason") or ""),
                                "task_type": parsed.get("task_type"),
                                "card_decision": parsed.get("card_decision"),
                                "negative_hints": parsed.get("negative_hints"),
                                "paths": parsed.get("paths"),
                                "expand_from_keys_requested": req_k,
                                "expand_from_keys_applied": [],
                                "new_edges": 0,
                                "candidate_edge_count": len(candidate_edges),
                                "reasoning": str(round_meta.get("reasoning") or "").strip(),
                                "raw_llm_content": str(round_meta.get("raw_content") or "").strip(),
                                "model_json": json.dumps(parsed, ensure_ascii=False),
                                "max_rounds": max_rounds,
                            },
                        )
                    except Exception:
                        pass
                if monitor_out is not None:
                    monitor_out.clear()
                    monitor_out.update(
                        {
                            "role": "decision_path_react_llm",
                            "messages": messages,
                            "react_rounds": round_logs,
                            "parsed_json": parsed,
                        }
                    )
                return dec_probe, []

            exploration_done = bool(parsed.get("exploration_done"))
            if round_idx >= max_rounds:
                exploration_done = True

            raw_expand = [
                str(x).strip() for x in (parsed.get("expand_from_keys") or []) if str(x).strip()
            ]
            valid_nodes = _node_keys_from_candidate_edges(candidate_edges)
            expand_keys = [k for k in raw_expand if k in valid_nodes and k not in expanded_from][:16]

            room = max(0, max_total_edges - len(candidate_edges))

            if not exploration_done:
                if room <= 0:
                    exploration_done = True
                elif not expand_keys:
                    expand_keys = _expand_fallback_frontier(candidate_edges, expanded_from)
                    if not expand_keys:
                        exploration_done = True

            new_count = 0
            expand_applied: list[str] = []
            if not exploration_done:
                expanded_from.update(expand_keys)
                expand_applied = list(expand_keys)
                labels = _labels_for_expand(anchors, candidate_edges)
                hop_label = candidate_edge_hops + round_idx
                cap = max(1, min(expand_new_edges_cap, room)) if room else 0
                extra: list[CandidateEdge] = []
                if cap > 0 and expand_keys:
                    extra = gather_out_edges_from_keys(
                        driver,
                        expand_keys,
                        labels_by_key=labels,
                        seen_edges=seen_triples,
                        per_node_limit=per_node_limit,
                        max_new_edges=cap,
                        hop=hop_label,
                        edge_direction=edge_direction,
                    )
                new_count = len(extra)
                if extra:
                    merge_edges_dedupe(candidate_edges, extra, seen=seen_triples)

            req_keys = parsed.get("expand_from_keys")
            if not isinstance(req_keys, list):
                req_keys = []

            if react_progress_cb is not None:
                try:
                    react_progress_cb(
                        round_idx,
                        {
                            "max_rounds": max_rounds,
                            "exploration_done": exploration_done,
                            "path_reason": str(parsed.get("path_reason") or ""),
                            "task_type": parsed.get("task_type"),
                            "card_decision": parsed.get("card_decision"),
                            "negative_hints": parsed.get("negative_hints"),
                            "paths": parsed.get("paths"),
                            "expand_from_keys_requested": req_keys,
                            "expand_from_keys_applied": expand_applied,
                            "new_edges": new_count,
                            "candidate_edge_count": len(candidate_edges),
                            "reasoning": str(round_meta.get("reasoning") or "").strip(),
                            "raw_llm_content": str(round_meta.get("raw_content") or "").strip(),
                            "model_json": json.dumps(parsed, ensure_ascii=False),
                        },
                    )
                except Exception:
                    pass

            if exploration_done:
                break

            messages.append({"role": "assistant", "content": json.dumps(parsed, ensure_ascii=False)})
            follow_user = (
                _build_merged_user_message(
                    question,
                    anchors,
                    candidate_cards,
                    estimated_task_type=estimated_task_type,
                    candidate_edges=candidate_edges,
                    ontology_summary=ontology_summary,
                    reuse_confirmed=reuse_confirmed,
                    candidate_edge_hops=candidate_edge_hops,
                    max_path_edges=max_path_edges,
                )
                + f"\n\n---\nReAct round {round_idx + 1}/{max_rounds}: {new_count} new candidate edge(s) added. "
                "Set exploration_done=true with final paths when ready, or continue exploring."
            )
            messages.append({"role": "user", "content": follow_user})

        dec = _decision_output_from_parsed(last_parsed, total_llm)
        if dec.reuse_key:
            picked_paths: list[list[dict[str, str]]] = []
        else:
            picked_paths = resolve_paths_steps(
                candidate_edges,
                anchors,
                max_path_edges,
                normalize_llm_paths_payload(last_parsed),
            )

        if monitor_out is not None:
            monitor_out.clear()
            monitor_out.update(
                {
                    "role": "decision_path_react_llm",
                    "messages": messages,
                    "react_rounds": round_logs,
                    "parsed_json": last_parsed,
                }
            )

        return dec, picked_paths

    except Exception as exc:
        log.warning("decide_with_path_react failed: %s", exc)
        if monitor_out is not None:
            monitor_out.clear()
            monitor_out.update(
                {
                    "role": "decision_path_react_llm",
                    "messages": messages,
                    "react_rounds": round_logs,
                    "error": str(exc),
                    "fallback": True,
                }
            )
        estimated = estimate_task_type(question)
        dec = DecisionOutput(
            task_type=estimated,
            reuse_key=None,
            card_confidence=0.0,
            card_reason=f"LLM fallback: {exc}",
            negative_hints=[],
            llm_calls=0,
        )
        picked_paths = resolve_paths_steps(
            candidate_edges,
            anchors,
            max_path_edges,
            [],
        )
        return dec, picked_paths


# ---------------------------------------------------------------------- #
# Main entry (decision-only; optional / tests)
# ---------------------------------------------------------------------- #

def decide(
    question: str,
    anchors: AnchorSet,
    candidate_cards: list[dict],
    *,
    model: Optional[str] = None,
    ontology_summary: str = "",
    reuse_confirmed: bool = False,
    max_retries: Optional[int] = None,
    temperature: Optional[float] = None,
    reasoning_capture: Optional[List[str]] = None,
    monitor_out: Optional[dict[str, Any]] = None,
    enable_thinking: Optional[bool] = None,
) -> DecisionOutput:
    """Call Decision LLM (1 LLM call) → DecisionOutput.

    If LLM call fails after retries, returns a fallback output:
      - task_type = rule-based estimate
      - reuse_key = None (forces graph traversal)
    """
    _mr = CFG.agent_decision_max_retries if max_retries is None else max_retries
    _temp = CFG.agent_decision_temperature if temperature is None else temperature
    user_msg = _build_user_message(
        question, anchors, candidate_cards,
        ontology_summary=ontology_summary,
        reuse_confirmed=reuse_confirmed,
    )

    messages = [
        {"role": "system", "content": DECISION_LLM_SYSTEM},
        {"role": "user", "content": DECISION_LLM_FEW_SHOT_USER},
        {"role": "assistant", "content": DECISION_LLM_FEW_SHOT_ASSISTANT},
        {"role": "user", "content": user_msg},
    ]

    llm_meta: dict[str, Any] = {}

    try:
        parsed = complete_json(
            messages,
            json_schema=DECISION_LLM_JSON_SCHEMA,
            model=model,
            max_retries=_mr,
            temperature=_temp,
            reasoning_capture=reasoning_capture,
            metadata_out=llm_meta if monitor_out is not None else None,
            enable_thinking=enable_thinking,
        )
        task_type = str(parsed.get("task_type") or "multistep")
        if task_type not in TASK_TYPES:
            task_type = "multistep"

        cd = parsed.get("card_decision") or {}
        reuse_key = cd.get("reuse_key") or None
        confidence = float(cd.get("confidence") or 0.0)
        reason = str(cd.get("reason") or "")

        negative_hints = [str(h) for h in (parsed.get("negative_hints") or [])]

        if monitor_out is not None:
            monitor_out.clear()
            monitor_out.update(
                {
                    "role": "decision_llm",
                    "messages": messages,
                    "llm": llm_meta,
                    "parsed_json": parsed,
                }
            )

        return DecisionOutput(
            task_type=task_type,
            reuse_key=reuse_key,
            card_confidence=confidence,
            card_reason=reason,
            negative_hints=negative_hints,
            llm_calls=1,
        )

    except Exception as exc:
        log.warning("Decision LLM failed, using fallback: %s", exc)
        if monitor_out is not None:
            monitor_out.clear()
            monitor_out.update(
                {
                    "role": "decision_llm",
                    "messages": messages,
                    "llm": llm_meta,
                    "error": str(exc),
                    "fallback": True,
                }
            )
        estimated = estimate_task_type(question)
        return DecisionOutput(
            task_type=estimated,
            reuse_key=None,
            card_confidence=0.0,
            card_reason=f"LLM fallback: {exc}",
            negative_hints=[],
            llm_calls=0,  # 0 = no successful LLM call
        )


__all__ = [
    "DecisionOutput",
    "decide",
    "decide_with_path",
    "decide_with_path_react",
    "estimate_task_type",
]

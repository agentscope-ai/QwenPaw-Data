"""规则层：由 task_type 决定每一跳允许的关系类型（及端点标签约束）。

LLM 不负责挑选「走 HAS_FORMULA 还是 ANALYZED_BY」这类类型决策（在候选边已按规则过滤的前提下，
由路径 LLM 只在候选集合中选具体边实例）。
"""
from __future__ import annotations

from typing import Any

# 一跳的定义：从满足 from_labels 的节点出发，沿 relationship_types 之一走到 to_labels。
HopRule = dict[str, Any]


def _hop(
    from_labels: list[str],
    rel_types: list[str],
    to_labels: list[str],
) -> HopRule:
    return {
        "from_labels": from_labels,
        "relationship_types": rel_types,
        "to_labels": to_labels,
    }


# 默认：指标 → 公式 → 表 → 列（HAS_COLUMN 挂在 Table 上，前沿在每 hop 后与下一 hop 的 from_labels 对齐）
_DEFAULT_LOOKUP_CHAIN: list[HopRule] = [
    _hop(["Metric"], ["HAS_FORMULA"], ["Formula"]),
    _hop(["Formula"], ["OF_VIEW"], ["Dataset"]),
    _hop(["Dataset"], ["CONTAINS_TABLE"], ["Table"]),
    _hop(["Table"], ["HAS_COLUMN"], ["Column"]),
]


def edge_profile_for_task(task_type: str) -> list[HopRule]:
    """返回有序 hop 列表；下游按 hop 顺序做分层扩展并收集候选边。"""
    t = (task_type or "").strip()
    # 与 pure_lookup 共用链：下钻语义主要在 SQL/证据层表达；单独拆 ANALYZED_BY 易使分层前沿错位
    if t == "dimensional_drill":
        return list(_DEFAULT_LOOKUP_CHAIN)
    if t in (
        "pure_lookup",
        "cross_period_compare",
        "ranking_topk",
        "trend_analysis",
        "event_aligned",
        "anomaly_detection",
        "compliance_check",
        "causal_attribution",
        "multistep",
    ):
        return list(_DEFAULT_LOOKUP_CHAIN)
    return list(_DEFAULT_LOOKUP_CHAIN)


def profile_to_observable_plan(profile: list[HopRule]) -> list[str]:
    """供监控 / 策略语义使用的「类型级」路径描述（单代表项，与 ``path_plan_to_steps`` 解析格式兼容）。"""
    lines: list[str] = []
    for hop in profile:
        fl = hop["from_labels"][0]
        rl = hop["relationship_types"][0]
        tl = hop["to_labels"][0]
        lines.append(f"{fl} -[{rl}]-> {tl}")
    return lines


__all__ = [
    "HopRule",
    "edge_profile_for_task",
    "profile_to_observable_plan",
]

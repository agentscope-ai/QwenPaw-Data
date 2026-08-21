"""Governance 跨图查询与学习入口（Phase 1）。

三个核心纯函数:

- ``calibrate_source_trust``: 基于交叉验证结果动态校准 source_trust
- ``predict_missing_links``: 启发式链接预测（属性相似度 + 邻居重合度）
- ``detect_metric_drift``: 检测同一 Metric 在不同查询中的语义漂移

写入仍走 ``knowledge.py`` 的 ``ingest_knowledge`` 或 runtime writeback。
"""
from __future__ import annotations

from typing import Any, Iterable

from neo4j import Driver

from ..utils import get_logger, neo4j_session

log = get_logger("graph.governance")


# ── R4: Trust 动态校准 ──────────────────────────────────────────────────────

def calibrate_source_trust(claims: list[dict]) -> list[dict]:
    """基于交叉验证结果校准 source_trust。

    每条 claim 需要: ``{source, metric, claimed, verified, current_trust?}``

    返回每条的校准结果: ``{source, metric, claimed, verified, delta_pct,
    verdict, old_trust, new_trust}``

    校准规则:
      - 偏差 < 2%  → hit,    trust +0.02
      - 偏差 2-10% → partial, trust +0.005
      - 偏差 > 10% → miss,   trust -0.03

    trust 值域: [0.3, 0.99]。
    """
    results: list[dict] = []
    for c in claims:
        claimed = float(c.get("claimed", 0))
        verified = float(c.get("verified", 0))

        if claimed == 0:
            delta_pct = abs(verified) if verified else 0.0
        else:
            delta_pct = abs(claimed - verified) / abs(claimed)

        if delta_pct < 0.02:
            trust_delta = +0.02
            verdict = "hit"
        elif delta_pct < 0.10:
            trust_delta = +0.005
            verdict = "partial"
        else:
            trust_delta = -0.03
            verdict = "miss"

        old_trust = float(c.get("current_trust", 0.80))
        new_trust = max(0.3, min(0.99, old_trust + trust_delta))

        results.append({
            "source": str(c.get("source", "")),
            "metric": str(c.get("metric", "")),
            "claimed": claimed,
            "verified": verified,
            "delta_pct": round(delta_pct * 100, 1),
            "verdict": verdict,
            "old_trust": old_trust,
            "new_trust": round(new_trust, 3),
        })
    return results


# ── R5: 链接预测 ────────────────────────────────────────────────────────────

def predict_missing_links(
    driver: Driver,
    *,
    entity_keys: list[str],
    top_k: int = 5,
    min_score: float = 0.5,
) -> list[dict]:
    """启发式链接预测: 属性相似度 + 邻居重合度。

    对每对 ``(entity_i, entity_j)`` 如果没有直接 RELATED_TO / ABOUT 边:
      score = 0.4 * name_similarity + 0.3 * neighbor_overlap + 0.3 * type_match

    返回 score >= min_score 的候选, 按 score 降序, 最多 top_k 条。

    每条返回: ``{from_key, from_name, to_key, to_name, score,
    predicted_relation, reason}``
    """
    if len(entity_keys) < 2:
        return []

    keys = [k for k in entity_keys if k and str(k).strip()]
    if len(keys) < 2:
        return []

    # 批量读取 entity 属性 + 邻居
    entities: dict[str, dict] = {}
    with neo4j_session(driver) as s:
        for rec in s.run(
            """
            UNWIND $keys AS k
            MATCH (e:Entity {key: k})
            OPTIONAL MATCH (e)-[r]-(nb)
            WITH e, collect(DISTINCT {rel: type(r), nb_key: nb.key, nb_name: nb.name, nb_type: nb.type}) AS nbs
            RETURN e.key AS key, e.name AS name, e.type AS type,
                   e.aliases AS aliases, e.description AS desc, nbs
            """,
            keys=keys,
        ):
            entities[rec["key"]] = {
                "key": rec["key"],
                "name": rec["name"] or "",
                "type": rec["type"] or "",
                "aliases": list(rec["aliases"] or []),
                "desc": rec["desc"] or "",
                "neighbors": [n for n in (rec["nbs"] or []) if n.get("nb_key")],
            }

    if len(entities) < 2:
        return []

    # 收集已有直接边的对
    existing_pairs: set[tuple[str, str]] = set()
    with neo4j_session(driver) as s:
        for rec in s.run(
            """
            UNWIND $keys AS k
            MATCH (a:Entity {key: k})-[r]-(b:Entity)
            WHERE b.key IN $keys
            RETURN a.key AS a, b.key AS b
            """,
            keys=keys,
        ):
            a, b = rec["a"], rec["b"]
            existing_pairs.add((min(a, b), max(a, b)))

    candidates: list[dict] = []
    key_list = list(entities.keys())
    for i in range(len(key_list)):
        for j in range(i + 1, len(key_list)):
            k1, k2 = key_list[i], key_list[j]
            pair = (min(k1, k2), max(k1, k2))
            if pair in existing_pairs:
                continue

            e1, e2 = entities[k1], entities[k2]

            # 1. Name similarity (Jaccard on character bigrams)
            name_sim = _bigram_jaccard(e1["name"], e2["name"])

            # 2. Neighbor overlap (Jaccard on neighbor keys)
            nb1 = {n["nb_key"] for n in e1["neighbors"]}
            nb2 = {n["nb_key"] for n in e2["neighbors"]}
            nb_overlap = len(nb1 & nb2) / max(len(nb1 | nb2), 1)
            shared_neighbors = list(nb1 & nb2)

            # 3. Type match
            type_match = 1.0 if e1["type"] and e1["type"] == e2["type"] else 0.0

            score = 0.4 * name_sim + 0.3 * nb_overlap + 0.3 * type_match
            if score < min_score:
                continue

            # Predict relation type
            if nb_overlap > 0.3:
                predicted_rel = "COMPETES_WITH"
            elif type_match > 0:
                predicted_rel = "SIMILAR_TO"
            else:
                predicted_rel = "RELATED_TO"

            reason_parts = []
            if shared_neighbors:
                nb_names = []
                for sk in shared_neighbors[:3]:
                    for n in e1["neighbors"]:
                        if n["nb_key"] == sk:
                            nb_names.append(n.get("nb_name", sk))
                            break
                reason_parts.append(f"共享邻居: [{', '.join(nb_names)}]")
            if type_match:
                reason_parts.append(f"同类型: {e1['type']}")
            if name_sim > 0.2:
                reason_parts.append(f"名称相似度: {name_sim:.0%}")

            candidates.append({
                "from_key": k1,
                "from_name": e1["name"],
                "to_key": k2,
                "to_name": e2["name"],
                "score": round(score, 3),
                "predicted_relation": predicted_rel,
                "reason": "; ".join(reason_parts) if reason_parts else "综合评分",
            })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    return candidates[:top_k]


def _bigram_jaccard(a: str, b: str) -> float:
    """Character bigram Jaccard similarity."""
    if not a or not b:
        return 0.0
    a = a.lower().strip()
    b = b.lower().strip()
    if len(a) < 2 or len(b) < 2:
        return 0.0
    bg_a = {a[i:i+2] for i in range(len(a) - 1)}
    bg_b = {b[i:i+2] for i in range(len(b) - 1)}
    inter = len(bg_a & bg_b)
    union = len(bg_a | bg_b)
    return inter / union if union else 0.0


# ── R6: 本体漂移检测 ────────────────────────────────────────────────────────

def detect_metric_drift(
    driver: Driver,
    *,
    window_days: int = 30,
    min_usages: int = 2,
) -> list[dict]:
    """检测同一 Metric 在不同 Task 中被用于不同语义的情况。

    查 TG 中近 window_days 天的 Task, 按 Metric 聚合, 检查
    是否存在显著不同的 WHERE 条件或聚合粒度。

    返回每条 drift: ``{metric_key, metric_name, current_definition,
    observed_usages: [{context, actual_meaning, count}], proposal, confidence}``

    注: 当前实现为轻量统计风格 (查询 pattern 共现频率),
    不做 NLP 语义分析。后续可接 LLM 做更精准的 meaning 分类。
    """
    results: list[dict] = []

    with neo4j_session(driver) as s:
        # 找出近期被多个 task 引用的 metric
        recs = s.run(
            """
            MATCH (t:Task)-[:RESOLVED_TO]->(m:Metric)
            WHERE t.created_at >= datetime() - duration({days: $days})
            WITH m, collect(DISTINCT t.key) AS tasks, count(DISTINCT t) AS task_count
            WHERE task_count >= $min_usages
            RETURN m.key AS metric_key, m.name AS metric_name,
                   m.definition AS definition, task_count,
                   tasks
            ORDER BY task_count DESC
            LIMIT 10
            """,
            days=window_days,
            min_usages=min_usages,
        )

        for rec in recs:
            metric_key = rec["metric_key"]
            metric_name = rec["metric_name"] or metric_key
            definition = rec["definition"] or ""
            task_keys = rec["tasks"] or []

            if len(task_keys) < min_usages:
                continue

            # 查这些 task 的 SQL 里用的 WHERE 条件
            usages: list[dict] = []
            for tk in task_keys:
                task_rec = s.run(
                    """
                    MATCH (t:Task {key: $tk})
                    OPTIONAL MATCH (t)-[:HAS_PLAN]->(p:Step)
                    RETURN t.question AS question, t.task_signature AS sig,
                           p.sql_template AS sql_tpl
                    LIMIT 1
                    """,
                    tk=tk,
                ).single()
                if not task_rec:
                    continue

                question = task_rec["question"] or ""
                sig = task_rec["sig"] or ""
                sql_tpl = task_rec["sql_tpl"] or ""

                # 轻量分类: 按 question 关键词或 sig 前缀分 context
                context = _classify_query_context(question, sig)
                usages.append({
                    "context": context,
                    "question": question[:80],
                    "meaning_hint": _extract_meaning_hint(question, sql_tpl),
                })

            if len(usages) < min_usages:
                continue

            # 按 context 聚合
            context_groups: dict[str, list[dict]] = {}
            for u in usages:
                ctx = u["context"]
                context_groups.setdefault(ctx, []).append(u)

            if len(context_groups) < 2:
                continue  # 没有歧义

            observed = []
            for ctx, items in context_groups.items():
                meanings = [i["meaning_hint"] for i in items if i["meaning_hint"]]
                observed.append({
                    "context": ctx,
                    "actual_meaning": meanings[0] if meanings else ctx,
                    "count": len(items),
                })

            # Confidence: 基于不同 context 数量 / 总 task 数量
            confidence = min(0.95, len(context_groups) / max(len(usages), 1))
            if confidence < 0.3:
                continue

            proposal_names = [
                f"{metric_name}({o['context']})" for o in observed
            ]
            proposal = (
                f"建议拆分为 {len(observed)} 个子指标: "
                + " / ".join(proposal_names)
            )

            results.append({
                "metric_key": metric_key,
                "metric_name": metric_name,
                "current_definition": definition,
                "observed_usages": observed,
                "proposal": proposal,
                "confidence": round(confidence, 2),
            })

    return results


def _classify_query_context(question: str, sig: str) -> str:
    """轻量 query context 分类 — 从 question/sig 提取场景关键词。"""
    q = (question + " " + sig).lower()
    if any(kw in q for kw in ("cli", "实训", "漏斗")):
        return "CLI漏斗分析"
    if any(kw in q for kw in ("模型", "发布", "上线")):
        return "模型发布效果"
    if any(kw in q for kw in ("opc", "扶持", "专项")):
        return "OPC专项"
    if any(kw in q for kw in ("高校", "学生", "教育")):
        return "高校场景"
    if any(kw in q for kw in ("国际", "overseas", "international")):
        return "国际站"
    if any(kw in q for kw in ("付费", "gaap", "消费")):
        return "商业化分析"
    return "通用分析"


def _extract_meaning_hint(question: str, sql_tpl: str) -> str:
    """从 question/sql 提取语义暗示 — 非常轻量, 后续可接 LLM。"""
    q = question.lower()
    if "注册" in q and "安装" in q:
        return "注册→安装"
    if "注册" in q and "首活" in q:
        return "注册→首活"
    if "注册" in q and ("转化" in q or "率" in q):
        return "着陆页→注册"
    if "活跃" in q:
        return "活跃用户统计"
    if "付费" in q or "gaap" in q.lower():
        return "付费转化"
    return ""


# ── KG↔TG 边: Task -TRIGGERED-> Policy|ComplianceRule ─────────────────────

_POLICY_KEY_PREFIX = "pol:"
_RULE_KEY_PREFIX = "rule:"


def _extract_governance_keys(
    node_keys: Iterable[str],
) -> tuple[list[str], list[str]]:
    """按前缀从一堆节点 key 里挑出 Policy / ComplianceRule key。

    返回 ``(policy_keys, rule_keys)``。空白 / 不匹配的 key 静默丢弃。
    """
    pol: list[str] = []
    rule: list[str] = []
    for raw in node_keys:
        k = str(raw or "").strip()
        if not k:
            continue
        if k.startswith(_POLICY_KEY_PREFIX):
            pol.append(k)
        elif k.startswith(_RULE_KEY_PREFIX):
            rule.append(k)
    return pol, rule


_TRIGGERED_CYPHER = """
MATCH (t:Task {key: $task_key})
UNWIND $rows AS r
MATCH (target)
  WHERE target.key = r.target_key
    AND (target:Policy OR target:ComplianceRule)
MERGE (t)-[tr:TRIGGERED {target_label: r.target_label}]->(target)
  ON CREATE SET tr.zone = 'knowledge',
                tr.triggered_at = datetime(),
                tr.source_id = $src
  ON MATCH  SET tr.last_triggered_at = datetime(),
                tr.trigger_count = coalesce(tr.trigger_count, 0) + 1
"""


def record_task_governance(
    driver: Driver,
    *,
    task_key: str,
    policy_keys: Iterable[str],
    rule_keys: Iterable[str],
    source_id: str = "runtime:writeback",
) -> int:
    """写 ``(:Task)-[:TRIGGERED]->(:Policy|:ComplianceRule)`` KG↔TG 跨图边。

    返回成功 MERGE 的边数。入参两边全空时完全不打 Neo4j。
    """
    pol_list = [k for k in (str(k).strip() for k in policy_keys) if k]
    rule_list = [k for k in (str(k).strip() for k in rule_keys) if k]
    if not pol_list and not rule_list:
        return 0
    if not (task_key or "").strip():
        log.warning(
            "record_task_governance: task_key empty; skipping %d policies + %d rules",
            len(pol_list), len(rule_list),
        )
        return 0

    rows = (
        [{"target_key": k, "target_label": "Policy"} for k in pol_list]
        + [{"target_key": k, "target_label": "ComplianceRule"} for k in rule_list]
    )
    with neo4j_session(driver) as s:
        s.run(
            _TRIGGERED_CYPHER,
            task_key=task_key.strip(),
            rows=rows,
            src=source_id,
        )
    log.info(
        "TRIGGERED edges: task=%s policies=%d rules=%d",
        task_key, len(pol_list), len(rule_list),
    )
    return len(rows)


__all__ = [
    "calibrate_source_trust",
    "predict_missing_links",
    "detect_metric_drift",
    "record_task_governance",
    "_extract_governance_keys",
]

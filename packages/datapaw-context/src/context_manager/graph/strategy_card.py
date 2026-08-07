"""Strategy (经验策略卡) — ``graph_topology_v4.md``。

提供三个类：

- :class:`StrategyCardWriter`：策略卡的写入（创建 / 命中更新 / supersede）。（类名保留以兼容 import）
- :class:`StrategyCardRetriever`：ANN 检索 + 复合评分重排。
- :class:`MemoryTierScheduler`：每日扫描 ``memory_tier`` 状态机。

设计约定：
- 所有写入均 idempotent（MERGE by key）。
- ANN 检索基于 ``strategy_vec`` 向量索引（``embedding`` 字段）。
- 复合评分在 Python 端后处理（Neo4j ANN 只输出 cos_sim，其余字段拉下来本地计算）。
- ``memory_tier`` 状态机仅在 ``daily_scan()`` 中批量更新，命中时也同步检查晋升。
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import math
from enum import Enum
from typing import Any, Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .keys import card_key as _card_key

log = get_logger("graph.strategy_card")


def _embedding_hash(embedding_text: str, model_name: str = "") -> str:
    """Idempotent hash for embedding text + model (for cache invalidation)."""
    h = hashlib.sha1()
    h.update((model_name or "unknown").encode("utf-8"))
    h.update(b"\x1f")
    h.update(embedding_text.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------- #
# Enums / constants
# ---------------------------------------------------------------------- #

class MemoryTier(str, Enum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


class CardPolarity(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


# Composite score weights (§3.3)
_W_COS = 0.55
_W_HEAT = 0.20
_W_RECENCY = 0.15
_W_TRUST = 0.10

# Recency decay half-lives per tier (days)
_DECAY_HOT = 7.0
_DECAY_WARM = 60.0
_COLD_FLOOR = 0.85  # cold tier keeps this recency floor

# ANN rerank threshold
_ACCEPT_THRESHOLD = 0.85
_ACCEPT_GAP = 0.10

# 最少命中次数：新卡（hit_count < MIN_HITS_FOR_REUSE）哪怕分数再高也不触发 auto_accept，
# Decision LLM 那边也会看到 [unverified] 标注，倾向于自己重新规划而非盲目复用。
MIN_HITS_FOR_REUSE = 2

# 新 apply 卡初始成功率：写入时设为 0.5（不确定）而不是 1.0（过于乐观），
# 首次 record_hit(outcome='success') 后滑动均值才会升到 0.75，
# 避免一张刚写入的错卡被当成高置信经验反复复用。
_INIT_SUCCESS_RATE = 0.5


# ---------------------------------------------------------------------- #
# Scoring helpers
# ---------------------------------------------------------------------- #

def _heat(hit_count: int, success_rate: float) -> float:
    """heat = log(1 + hit_count) × success_rate"""
    return math.log1p(hit_count) * success_rate


def _recency(memory_tier: str, last_hit_at_days_ago: float) -> float:
    """Tiered exponential decay; cold tier has a floor."""
    if memory_tier == MemoryTier.HOT:
        return math.exp(-last_hit_at_days_ago / _DECAY_HOT)
    if memory_tier == MemoryTier.WARM:
        return math.exp(-last_hit_at_days_ago / _DECAY_WARM)
    if memory_tier == MemoryTier.COLD:
        return _COLD_FLOOR
    # archived tier removed in v4; treat as cold floor
    return 0.05


def _trust(source_trust: float, valid_to_is_null: bool) -> float:
    """trust = (1.0 if active else 0.3) × source_trust"""
    multiplier = 1.0 if valid_to_is_null else 0.3
    return multiplier * source_trust


def composite_score(
    cos_sim: float,
    hit_count: int,
    success_rate: float,
    memory_tier: str,
    last_hit_days: float,
    source_trust: float,
    valid_to_is_null: bool,
) -> float:
    h = _heat(hit_count, success_rate)
    r = _recency(memory_tier, last_hit_days)
    t = _trust(source_trust, valid_to_is_null)
    # Normalise heat (log-scale, max ~5 for hit_count=150)
    h_norm = min(h / 5.0, 1.0)
    return _W_COS * cos_sim + _W_HEAT * h_norm + _W_RECENCY * r + _W_TRUST * t


# ---------------------------------------------------------------------- #
# StrategyCardWriter
# ---------------------------------------------------------------------- #

class StrategyCardWriter:
    """经验卡 CRUD。

    用法（Step 6 异步写回）：

    .. code-block:: python

        writer = StrategyCardWriter(driver)
        card_key = writer.write_apply_card(
            task_type="cross_period_compare",
            question_summary="对比 3 月与 2 月 DAU 环比",
            anchor_label_types=["Metric"],
            path_subgraph_keys=["met:ChatApp:DAU", "fml:..."],
            sql_template="SELECT ...",
            task_key="task:...",
        )
        writer.record_hit(card_key, outcome="success")
    """

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def write_apply_card(
        self,
        *,
        task_type: str,
        question_summary: str,
        anchor_label_types: list[str],
        path_subgraph_keys: list[str],
        sql_template: str = "",
        task_key: str = "",
        graph_db_id: str = "",
        strategy_semantics: str = "",
        embedding: Optional[list[float]] = None,
        source_trust: float = 0.7,
        trigger_conditions: Optional[dict] = None,
        entry_anchor_keys: Optional[list[str]] = None,
        task_signature: str = "",
    ) -> str:
        """创建或更新一张 positive 策略卡（成功经验）。返回 card key。"""
        sig_basis = (task_signature or "").strip() or (task_type or "").strip()
        key = _card_key(
            sig_basis,
            anchor_label_types,
            question_summary,
            graph_db_id=graph_db_id,
        )
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        gdb = (graph_db_id or "").strip()
        tc = (
            dict(trigger_conditions)
            if trigger_conditions is not None
            else {
                "anchor_entity_types": anchor_label_types,
                "question_summary": question_summary[:120],
                "graph_db_id": gdb,
            }
        )
        tc["task_type"] = task_type
        tc["task_signature"] = sig_basis
        if entry_anchor_keys:
            tc = {
                **tc,
                "entry_anchor_keys": [str(x) for x in entry_anchor_keys if x][:24],
            }
        if (strategy_semantics or "").strip():
            tc = {**tc, "strategy_semantics": (strategy_semantics or "")[:2000]}
        import json
        with neo4j_session(self.driver) as s:
            s.run(
                """
                MERGE (c:Strategy {key: $key})
                ON CREATE SET
                    c.task_signature       = $task_signature,
                    c.polarity             = 'positive',
                    c.trigger_conditions   = $tc_json,
                    c.path_subgraph_keys   = $path_keys,
                    c.sql_template         = $sql_template,
                    c.strategy_semantics   = $strategy_semantics,
                    c.hit_count            = 0,
                    c.success_rate         = $init_rate,
                    c.memory_tier          = 'hot',
                    c.valid_at             = datetime($now),
                    c.ingest_at            = datetime($now),
                    c.source_trust         = $source_trust,
                    c.zone                 = 'trace',
                    c.graph_db_id          = $graph_db_id
                ON MATCH SET
                    c.task_signature       = $task_signature,
                    c.trigger_conditions   = $tc_json,
                    c.path_subgraph_keys   = $path_keys,
                    c.sql_template         = $sql_template,
                    c.strategy_semantics   = $strategy_semantics,
                    c.source_trust         = $source_trust,
                    c.graph_db_id          = $graph_db_id
                """,
                key=key,
                task_signature=sig_basis,
                tc_json=json.dumps(tc, ensure_ascii=False, default=str),
                path_keys=path_subgraph_keys,
                strategy_semantics=(strategy_semantics or "")[:8000],
                sql_template=sql_template,
                init_rate=_INIT_SUCCESS_RATE,
                now=now,
                source_trust=float(source_trust),
                graph_db_id=gdb,
            )
            # Link to source Task if provided
            if task_key:
                s.run(
                    """
                    MATCH (c:Strategy {key: $ckey})
                    OPTIONAL MATCH (t:Task {key: $tkey})
                    FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
                        MERGE (c)-[:GENERALIZES_FROM]->(t)
                    )
                    """,
                    ckey=key,
                    tkey=task_key,
                )
            # Store embedding if provided (batch update outside ANN index build is OK)
            if embedding:
                s.run(
                    "MATCH (c:Strategy {key: $key}) SET c.embedding = $emb, c.embedding_hash = $emb_hash",
                    key=key,
                    emb=embedding,
                    emb_hash=_embedding_hash(embedding),
                )
        log.info("write_apply_card: %s (task_type=%s)", key, task_type)
        return key

    def write_avoid_card(
        self,
        *,
        task_type: str,
        question_summary: str,
        anchor_label_types: list[str],
        lesson: str,
        failed_path_keys: list[str] = [],
        task_key: str = "",
        graph_db_id: str = "",
        strategy_semantics: str = "",
        embedding: Optional[list[float]] = None,
        source_trust: float = 0.5,
        entry_anchor_keys: Optional[list[str]] = None,
        task_signature: str = "",
    ) -> str:
        """创建一张 negative 策略卡（避坑经验）。success_rate 固定为 0。"""
        sig_basis = (task_signature or "").strip() or (task_type or "").strip()
        key = _card_key(
            sig_basis,
            anchor_label_types,
            question_summary,
            graph_db_id=graph_db_id,
            variant_suffix="avoid",
        )
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        gdb = (graph_db_id or "").strip()
        import json
        tc = {
            "anchor_entity_types": anchor_label_types,
            "question_summary": question_summary[:120],
            "lesson": lesson[:500],
            "graph_db_id": gdb,
            "task_type": task_type,
            "task_signature": sig_basis,
        }
        if entry_anchor_keys:
            tc = {
                **tc,
                "entry_anchor_keys": [str(x) for x in entry_anchor_keys if x][:24],
            }
        if (strategy_semantics or "").strip():
            tc = {**tc, "strategy_semantics": (strategy_semantics or "")[:2000]}
        with neo4j_session(self.driver) as s:
            s.run(
                """
                MERGE (c:Strategy {key: $key})
                ON CREATE SET
                    c.task_signature       = $task_signature,
                    c.polarity             = 'negative',
                    c.trigger_conditions   = $tc_json,
                    c.path_subgraph_keys   = $path_keys,
                    c.sql_template         = '',
                    c.strategy_semantics   = $strategy_semantics,
                    c.hit_count            = 0,
                    c.success_rate         = 0.0,
                    c.memory_tier          = 'hot',
                    c.valid_at             = datetime($now),
                    c.ingest_at            = datetime($now),
                    c.source_trust         = $source_trust,
                    c.zone                 = 'trace',
                    c.graph_db_id          = $graph_db_id
                ON MATCH SET
                    c.task_signature       = $task_signature,
                    c.trigger_conditions   = $tc_json,
                    c.path_subgraph_keys   = $path_keys,
                    c.strategy_semantics   = $strategy_semantics,
                    c.graph_db_id          = $graph_db_id
                """,
                key=key,
                task_signature=sig_basis,
                tc_json=json.dumps(tc, ensure_ascii=False, default=str),
                path_keys=failed_path_keys,
                strategy_semantics=(strategy_semantics or "")[:8000],
                now=now,
                source_trust=float(source_trust),
                graph_db_id=gdb,
            )
            if task_key:
                s.run(
                    """
                    MATCH (c:Strategy {key: $ckey})
                    OPTIONAL MATCH (t:Task {key: $tkey})
                    FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
                        MERGE (c)-[:GENERALIZES_FROM]->(t)
                    )
                    """,
                    ckey=key,
                    tkey=task_key,
                )
            if embedding:
                s.run(
                    "MATCH (c:Strategy {key: $key}) SET c.embedding = $emb, c.embedding_hash = $emb_hash",
                    key=key,
                    emb=embedding,
                    emb_hash=_embedding_hash(embedding),
                )
        log.info("write_avoid_card: %s (lesson=%s…)", key, lesson[:60])
        return key

    def record_hit(
        self,
        card_key: str,
        *,
        outcome: str,  # 'success' | 'fail' | 'partial'
        plan_key: str = "",
        window: int = 20,
    ) -> None:
        """更新 hit_count、success_rate（滑动窗口），按需调整 memory_tier。

        - ``avoid`` 卡：hit_count++ 但 success_rate 永久 = 0。
        - 规则：命中后若 tier = cold / archived → 重置为 hot。
        """
        success_delta = 1.0 if outcome == "success" else 0.0
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()

        with neo4j_session(self.driver) as s:
            # Fetch current state
            rec = s.run(
                """
                MATCH (c:Strategy {key: $key})
                RETURN c.hit_count AS hit_count,
                       c.success_rate AS success_rate,
                       c.polarity AS polarity,
                       c.memory_tier AS tier
                """,
                key=card_key,
            ).single()
            if rec is None:
                log.warning("record_hit: card not found: %s", card_key)
                return

            old_hits = int(rec["hit_count"] or 0)
            old_rate = float(rec["success_rate"] or 0.0)
            polarity = str(rec["polarity"] or "positive")
            tier = str(rec["tier"] or "hot")

            new_hits = old_hits + 1
            if polarity == "negative":
                new_rate = 0.0
            else:
                # Sliding average: rate = rate * (w-1)/w + outcome/w
                w = float(min(new_hits, window))
                new_rate = old_rate * (w - 1) / w + success_delta / w

            # Tier promotion: if hit while cold → hot (v4: no archived tier)
            new_tier = "hot" if tier == "cold" else tier

            s.run(
                """
                MATCH (c:Strategy {key: $key})
                SET c.hit_count   = $hits,
                    c.success_rate = $rate,
                    c.memory_tier  = $tier,
                    c.last_hit_at  = datetime($now)
                """,
                key=card_key,
                hits=new_hits,
                rate=new_rate,
                tier=new_tier,
                now=now,
            )

            # Link APPLIED_BY → Step (if provided)
            if plan_key:
                s.run(
                    """
                    MATCH (c:Strategy {key: $ckey})
                    OPTIONAL MATCH (p:Step {key: $pkey})
                    FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [p] END |
                        MERGE (c)-[ab:APPLIED_BY {matched_at: datetime($now)}]->(p)
                          ON CREATE SET ab.outcome = $outcome
                          ON MATCH  SET ab.outcome = $outcome
                    )
                    """,
                    ckey=card_key,
                    pkey=plan_key,
                    outcome=outcome,
                    now=now,
                )

    def supersede(self, *, old_key: str, new_key: str, reason: str = "") -> None:
        """旧卡 valid_to = now()，建 (new)-[:SUPERSEDED_BY]->(old) 边。"""
        now = _dt.datetime.now(_dt.timezone.utc).isoformat()
        with neo4j_session(self.driver) as s:
            s.run(
                """
                MATCH (old:Strategy {key: $old_key})
                MATCH (new:Strategy {key: $new_key})
                SET old.valid_to = datetime($now)
                MERGE (new)-[r:SUPERSEDED_BY]->(old)
                  ON CREATE SET r.reason = $reason, r.superseded_at = datetime($now)
                  ON MATCH  SET r.reason = $reason
                """,
                old_key=old_key,
                new_key=new_key,
                now=now,
                reason=str(reason or "")[:500],
            )
        log.info("supersede: %s → %s", new_key, old_key)


# ---------------------------------------------------------------------- #
# StrategyCardRetriever
# ---------------------------------------------------------------------- #

class StrategyCardRetriever:
    """经验卡 ANN 检索 + 复合评分重排（§3.3 / §3.4）。"""

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def recall_top_k(
        self,
        query_emb: list[float],
        *,
        task_type: Optional[str] = None,
        graph_db_id: str = "",
        k: int = 5,
        allow_avoid: bool = True,
    ) -> list[dict[str, Any]]:
        """ANN top-k，复合评分重排。

        Returns list of dicts with keys:
          key, task_type, polarity, path_subgraph_keys, sql_template, strategy_semantics,
          hit_count, success_rate, memory_tier, source_trust,
          valid_to_is_null, cos_sim, composite_score, trigger_conditions
        """
        if not query_emb:
            return []

        ann_k = min(k * 3, 20)  # oversample for reranking

        # Build WHERE clause
        conditions: list[str] = [
            "(c.valid_to IS NULL OR c.valid_to > datetime())",
        ]
        if task_type and task_type != "multistep":
            conditions.append(
                "(toLower(toString(c.trigger_conditions)) CONTAINS toLower($task_type_snip))"
            )
        gdb = (graph_db_id or "").strip()
        if gdb:
            conditions.append("coalesce(c.graph_db_id, '') = $gdb")
        if not allow_avoid:
            conditions.append("c.polarity = 'positive'")

        where_clause = " AND ".join(conditions)

        cypher = f"""
        CALL db.index.vector.queryNodes('strategy_vec', $k, $emb) YIELD node AS c, score AS cos_sim
        WHERE {where_clause}
        RETURN c.key AS key,
               c.graph_db_id AS graph_db_id,
               c.task_signature AS task_signature,
               c.polarity AS polarity,
               c.trigger_conditions AS trigger_conditions,
               coalesce(c.path_subgraph_keys, []) AS path_subgraph_keys,
               coalesce(c.sql_template, '') AS sql_template,
               coalesce(c.strategy_semantics, '') AS strategy_semantics,
               coalesce(c.hit_count, 0) AS hit_count,
               coalesce(c.success_rate, 0.0) AS success_rate,
               coalesce(c.memory_tier, 'hot') AS memory_tier,
               coalesce(c.source_trust, 0.7) AS source_trust,
               c.valid_to IS NULL AS valid_to_is_null,
               c.last_hit_at AS last_hit_at,
               c.valid_at AS valid_at,
               cos_sim
        LIMIT $k
        """

        params: dict[str, Any] = {"k": ann_k, "emb": query_emb}
        if gdb:
            params["gdb"] = gdb
        if task_type and task_type != "multistep":
            params["task_type_snip"] = f'"task_type": "{task_type}"'
        try:
            with neo4j_session(self.driver) as s:
                rows = s.run(cypher, **params).data()
        except Exception as exc:
            log.warning("card ANN query failed: %s", exc)
            return []

        now = _dt.datetime.now(_dt.timezone.utc)

        def _days_ago(ts: Any) -> float:
            if ts is None:
                return 365.0
            try:
                if hasattr(ts, "to_native"):
                    ts = ts.to_native()
                if isinstance(ts, _dt.datetime):
                    delta = now - ts.replace(tzinfo=_dt.timezone.utc) if ts.tzinfo is None else now - ts
                    return max(delta.total_seconds() / 86400, 0)
            except Exception:
                pass
            return 180.0

        scored: list[dict[str, Any]] = []
        import json as _json

        for row in rows:
            raw_tc = row.get("trigger_conditions")
            try:
                if isinstance(raw_tc, str):
                    d = _json.loads(raw_tc)
                elif isinstance(raw_tc, dict):
                    d = raw_tc
                else:
                    d = {}
                row["task_type"] = str(d.get("task_type") or task_type or "")
            except Exception:
                row["task_type"] = task_type or ""
            last_hit = row.get("last_hit_at") or row.get("valid_at")
            days_ago = _days_ago(last_hit)
            sc = composite_score(
                cos_sim=float(row["cos_sim"]),
                hit_count=int(row["hit_count"]),
                success_rate=float(row["success_rate"]),
                memory_tier=str(row["memory_tier"]),
                last_hit_days=days_ago,
                source_trust=float(row["source_trust"]),
                valid_to_is_null=bool(row["valid_to_is_null"]),
            )
            scored.append({**row, "composite_score": sc})

        scored.sort(key=lambda x: -x["composite_score"])
        return scored[:k]

    def top_card_decision(
        self,
        candidates: list[dict[str, Any]],
        *,
        accept_threshold: Optional[float] = None,
        accept_gap: Optional[float] = None,
    ) -> dict[str, Any]:
        """Given reranked candidates, determine if top-1 can be auto-accepted.

        Returns:
          {
            "auto_accept": bool,
            "top_card": dict | None,
            "avoid_cards": list[dict],
          }
        """
        thr = float(_ACCEPT_THRESHOLD if accept_threshold is None else accept_threshold)
        gap_min = float(_ACCEPT_GAP if accept_gap is None else accept_gap)

        apply_cards = [c for c in candidates if c.get("polarity") in ("positive", "apply")]
        avoid_cards = [c for c in candidates if c.get("polarity") in ("negative", "avoid")]

        if not apply_cards:
            return {"auto_accept": False, "top_card": None, "avoid_cards": avoid_cards}

        top = apply_cards[0]
        top_score = top["composite_score"]
        second_score = apply_cards[1]["composite_score"] if len(apply_cards) > 1 else 0.0
        gap = top_score - second_score

        # 新卡（hit_count < MIN_HITS_FOR_REUSE）不触发 auto_accept：
        # 刚写入的卡还没经过真实 SQL 执行验证，不应绕过 LLM 直接复用。
        hit_count = int(top.get("hit_count", 0))
        auto_accept = (
            top_score >= thr
            and gap >= gap_min
            and hit_count >= MIN_HITS_FOR_REUSE
        )
        return {
            "auto_accept": auto_accept,
            "top_card": top,
            "avoid_cards": avoid_cards,
        }


def gate_strategy_cards_for_llm(
    candidates: list[dict[str, Any]],
    *,
    accept_threshold: float = _ACCEPT_THRESHOLD,
    min_hits: int = MIN_HITS_FOR_REUSE,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split ANN-recalled cards into LLM-visible vs gate-blocked.

  Only cards that meet the same trust bar as ``top_card_decision`` auto-accept
  (verified hit count + composite score) are shown to Decision / SQL LLMs.
  Unverified low-score cards stay in the blocked list for monitor/debug only.
    """
    thr = float(accept_threshold)
    min_h = int(min_hits)
    visible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for c in candidates:
        hits = int(c.get("hit_count", 0))
        score = float(c.get("composite_score", 0))
        pol = str(c.get("polarity") or "positive")
        if pol in ("negative", "avoid"):
            ok = hits >= min_h
        else:
            ok = hits >= min_h and score >= thr
        if ok:
            visible.append(c)
        else:
            blocked.append(c)
    return visible, blocked


# ---------------------------------------------------------------------- #
# MemoryTierScheduler
# ---------------------------------------------------------------------- #

class MemoryTierScheduler:
    """每日定时任务：扫描 ``:Strategy`` 节点 memory_tier 状态机（v4；类名 StrategyCard* 为历史 import）。

    状态机规则：
      hot  → warm     : 30 天无 hit + success_rate ≥ 0.6
      hot  → archived : 30 天无 hit + success_rate < 0.6
      warm → cold     : 180 天无 hit + rate ≥ 0.85 + count ≥ 10
      warm → archived : 180 天无 hit + (rate < 0.6 OR count < 10)
      cold → hot      : 被 hit 命中（record_hit 处理，不在此扫描）
      archived → hot  : 被 hit 命中（record_hit 处理）
    """

    def __init__(self, driver: Driver) -> None:
        self.driver = driver

    def daily_scan(self) -> dict[str, int]:
        """扫描所有 valid_to IS NULL 的卡，按规则更新 memory_tier。

        Returns counts: {"hot→warm": N, "hot→archived": N, "warm→cold": N, "warm→archived": N}
        """
        counts: dict[str, int] = {
            "hot→warm": 0,
            "hot→cold": 0,
            "warm→cold": 0,
        }

        with neo4j_session(self.driver) as s:
            # hot → warm/archived (30 days threshold)
            r = s.run(
                """
                MATCH (c:Strategy)
                WHERE c.valid_to IS NULL
                  AND c.memory_tier = 'hot'
                  AND (
                    c.last_hit_at IS NULL
                    OR duration.between(c.last_hit_at, datetime()).days >= 30
                  )
                WITH c,
                     CASE WHEN c.success_rate >= 0.6 THEN 'warm' ELSE 'cold' END AS new_tier
                SET c.memory_tier = new_tier
                RETURN new_tier, count(c) AS cnt
                """
            ).data()
            for row in r:
                key = f"hot→{row['new_tier']}"
                counts[key] = int(row["cnt"])

            # warm → cold (180 days threshold)
            r = s.run(
                """
                MATCH (c:Strategy)
                WHERE c.valid_to IS NULL
                  AND c.memory_tier = 'warm'
                  AND (
                    c.last_hit_at IS NULL
                    OR duration.between(c.last_hit_at, datetime()).days >= 180
                  )
                WITH c,
                     CASE
                       WHEN c.success_rate >= 0.85 AND c.hit_count >= 10 THEN 'cold'
                       ELSE 'cold'
                     END AS new_tier
                SET c.memory_tier = new_tier
                RETURN new_tier, count(c) AS cnt
                """
            ).data()
            for row in r:
                key = f"warm→{row['new_tier']}"
                counts[key] = int(row["cnt"])

        total_changed = sum(counts.values())
        log.info("MemoryTierScheduler.daily_scan: %d transitions — %s", total_changed, counts)
        return counts


# ═══════════════════════════════════════════════════════════════════════ #
#  Strategy Card admin helpers
# ═══════════════════════════════════════════════════════════════════════ #

def list_strategy_cards(
    driver: Driver,
    *,
    polarity: str | None = None,
    memory_tier: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """Paginated strategy card listing with polarity/tier filters."""
    conditions = ["1=1"]
    params: dict = {}
    if polarity:
        conditions.append("s.polarity = $polarity")
        params["polarity"] = polarity
    if memory_tier:
        conditions.append("s.memory_tier = $memory_tier")
        params["memory_tier"] = memory_tier
    where = " AND ".join(conditions)
    skip = (page - 1) * page_size
    params["skip"] = skip
    params["limit"] = page_size

    count_cypher = f"MATCH (s:Strategy) WHERE {where} RETURN count(s) AS total"
    data_cypher = f"""
    MATCH (s:Strategy)
    WHERE {where}
    RETURN s.key AS key,
           coalesce(s.task_signature, '') AS task_signature,
           coalesce(s.polarity, 'positive') AS polarity,
           coalesce(s.memory_tier, 'warm') AS memory_tier,
           coalesce(s.hit_count, 0) AS hit_count,
           coalesce(s.success_rate, 0) AS success_rate,
           coalesce(s.strategy_semantics, '') AS strategy_semantics,
           coalesce(s.example_query, '') AS example_query,
           toString(s.valid_at) AS valid_at,
           toString(s.last_hit_at) AS last_hit_at
    ORDER BY s.valid_at DESC
    SKIP $skip LIMIT $limit
    """
    with neo4j_session(driver) as s:
        total_rec = s.run(count_cypher, **{k: v for k, v in params.items() if k not in ("skip", "limit")}).single()
        total = int(total_rec["total"]) if total_rec else 0
        rows = s.run(data_cypher, **params).data()
    return rows, total


def get_strategy_card_detail(driver: Driver, card_key: str) -> dict | None:
    """Return full card properties with related tasks and hit records."""
    with neo4j_session(driver) as s:
        rec = s.run(
            """
            MATCH (s:Strategy {key: $key})
            RETURN properties(s) AS props
            """,
            key=card_key,
        ).single()
        if not rec:
            return None
        props = dict(rec["props"] or {})
        for k in ("embedding", "embedding_hash", "signature_emb", "strategy_vec"):
            props.pop(k, None)

        # Generalises-from tasks
        task_rows = s.run(
            """
            MATCH (s:Strategy {key: $key})-[:GENERALIZES_FROM]->(t:Task)
            RETURN t.key AS key, t.goal AS goal, t.status AS status,
                   toString(t.created_at) AS created_at
            ORDER BY t.created_at DESC
            LIMIT 20
            """,
            key=card_key,
        ).data()

        # Hit records (APPLIED_BY)
        hit_rows = s.run(
            """
            MATCH (s:Strategy {key: $key})-[:APPLIED_BY]->(p:Step)
            OPTIONAL MATCH (p)<-[:DECOMPOSES_INTO]-(t:Task)
            RETURN p.key AS plan_key, t.key AS task_key, t.goal AS task_goal,
                   toString(p.ts) AS applied_at
            ORDER BY p.ts DESC
            LIMIT 20
            """,
            key=card_key,
        ).data()

    # Serialize Neo4j types
    from ..api.kg_admin import _to_jsonable
    props = _to_jsonable(props)

    return {
        "card": props,
        "related_tasks": task_rows,
        "hit_records": hit_rows,
    }


def invalidate_strategy_card(driver: Driver, card_key: str, *, reason: str = "") -> dict:
    """Expire a strategy card by setting valid_to to now."""
    with neo4j_session(driver) as s:
        rec = s.run(
            """
            MATCH (s:Strategy {key: $key})
            WHERE s.valid_to IS NULL
            SET s.valid_to = datetime(),
                s.invalidation_reason = $reason
            RETURN s.key AS key
            """,
            key=card_key, reason=reason,
        ).single()
    if not rec:
        raise ValueError(f"Strategy card not found or already invalidated: {card_key}")
    return {"ok": True, "key": rec["key"]}


__all__ = [
    "CardPolarity",
    "MemoryTier",
    "MemoryTierScheduler",
    "MIN_HITS_FOR_REUSE",
    "StrategyCardRetriever",
    "StrategyCardWriter",
    "composite_score",
    "gate_strategy_cards_for_llm",
    "list_strategy_cards",
    "get_strategy_card_detail",
    "invalidate_strategy_card",
]

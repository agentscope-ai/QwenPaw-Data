"""``JOINS_ON`` 推断（Metadata 层，通用版）。

v3.1 通用化：
- 启发规则不再硬编码 appdata 业务列名；改用 ``DatasetProfile`` 注入 join_key_hints /
  join_dim_overlap_hints / manual_overrides。
- 新增通用 id-like 列名正则（``profile.join_id_pattern``），命中时 confidence=0.6。
- ``write_join_inference`` 接受可选 ``profile`` 参数；不传时行为与旧版向后兼容。

三步逻辑（与旧版一致，权重调整）：

1. **profile 白名单 + 通用 id-like 正则**：同名同库同 schema 列对
   - 命中 ``profile.join_key_hints``  → confidence 0.7
   - 命中 ``join_id_pattern`` 正则    → confidence 0.6
   - 两端都是 dws_/dwd_ 等聚合前缀时提升到 0.85（由 profile.layer_prefixes 决定）
2. **ds 维度重叠**：仅当两表都有 ds 列且 ``join_dim_overlap_hints`` 非空时建低权 JOINS_ON
3. **manual_overrides**：完全由 profile 提供，confidence=1.0

只用 MERGE，重复跑幂等。
"""
from __future__ import annotations

import re
from typing import Optional, Sequence

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .profile import DatasetProfile, ManualOverride

log = get_logger("graph.joins")


# ---------------------------------------------------------------------- #
# 向后兼容：旧版全局常量（只读）
# ---------------------------------------------------------------------- #
JOIN_KEY_WHITELIST: tuple[str, ...] = (
    "user_id",
    "task_id",
    "chat_id",
    "query_id",
    "answer_id",
    "visit_id",
    "request_id",
    "dashscope_request_id",
    "device_id",
    "ds",
)

DS_DIMENSIONAL_OVERLAP: tuple[str, ...] = (
    "terminal_type",
    "region",
    "country_name",
    "model_code",
)

DEFAULT_MANUAL_OVERRIDES: tuple[ManualOverride, ...] = (
    (
        "public.dws_ty_chatapp_overview_1d.terminal_type",
        "public.dws_ty_chatapp_multidim_chatindex_1d.terminal_type",
        "terminal_type",
    ),
    (
        "public.dwd_ty_qwen_chat_msg_fusion_di.user_id",
        "public.dim_ty_qwen_chat_user.user_id",
        "user_id",
    ),
)


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def write_join_inference(
    driver: Driver,
    *,
    db_id: str,
    schema: str,
    profile: Optional[DatasetProfile] = None,
    # 向后兼容参数（profile 为 None 时使用）
    join_key_whitelist: Optional[Sequence[str]] = None,
    ds_overlap_keys: Optional[Sequence[str]] = None,
    manual_overrides: Optional[Sequence[ManualOverride]] = None,
) -> None:
    """三步建 ``JOINS_ON`` 边，并对已有 sample_values 列跑 Jaccard 升权。

    优先使用 ``profile`` 中的配置；未传 profile 且也未传旧参数时使用内置 appdata 默认值。
    """
    if profile is not None:
        hint_keys = list(profile.join_key_hints)
        dim_overlap = list(profile.join_dim_overlap_hints)
        overrides = list(profile.manual_overrides)
        id_pattern = profile.join_id_pattern
        layer_pfxs = list(profile.layer_prefixes)
    else:
        # 向后兼容：旧版默认值
        hint_keys = list(join_key_whitelist) if join_key_whitelist is not None else list(JOIN_KEY_WHITELIST)
        dim_overlap = list(ds_overlap_keys) if ds_overlap_keys is not None else list(DS_DIMENSIONAL_OVERLAP)
        overrides = list(manual_overrides) if manual_overrides is not None else list(DEFAULT_MANUAL_OVERRIDES)
        id_pattern = r"^.+_(id|key|uuid|code|no|num)$"
        layer_pfxs = ["dws", "dwd"]

    with neo4j_session(driver) as s:
        # 第 1 步：白名单匹配 + id-like 正则
        n_hint = s.execute_write(
            _write_hint_joins,
            db_id=db_id,
            schema=schema,
            hint_keys=hint_keys,
            id_pattern=id_pattern,
            agg_prefixes=layer_pfxs,
        )

        # 第 2 步：ds 维度重叠（仅当 dim_overlap 非空）
        n_ds = 0
        if dim_overlap:
            n_ds = s.execute_write(
                _write_ds_overlap_joins,
                db_id=db_id,
                schema=schema,
                keys=dim_overlap,
            )

        # 第 3 步：manual override
        n_manual = s.execute_write(
            _write_manual_overrides,
            db_id=db_id,
            overrides=[
                {"left": left, "right": right, "via": via}
                for left, right, via in overrides
            ],
        )

        # 第 4 步：Jaccard 升权（对 sample_values 非空的 JOINS_ON 边）
        n_jaccard = s.execute_write(
            _upgrade_jaccard_confidence,
            db_id=db_id,
            schema=schema,
            threshold=0.3,
        )

    log.info(
        "JOINS_ON: hint=%d, ds_overlap=%d, manual=%d, jaccard_upgraded=%d",
        n_hint,
        n_ds,
        n_manual,
        n_jaccard,
    )


# ---------------------------------------------------------------------- #
# 子写入函数
# ---------------------------------------------------------------------- #
def _write_hint_joins(
    tx,
    *,
    db_id: str,
    schema: str,
    hint_keys: list[str],
    id_pattern: str,
    agg_prefixes: list[str],
) -> int:
    """同名列：在 hint_keys 白名单 或 匹配 id_pattern → JOINS_ON。

    置信度规则：
    - hint_keys 命中 → 0.7；两端都属于 agg_prefixes 中某前缀的表 → 升至 0.85
    - id_pattern 命中（未在 hint_keys）→ 0.6
    """
    # 两步：先跑 hint 白名单，再跑 id 正则（避免 hint 已建的边被低权覆盖）
    total = 0

    # --- step A：hint_keys 白名单 ---
    if hint_keys:
        # agg_prefixes 用于提升 confidence：Cypher 参数只能传 string list
        res = tx.run(
            """
            MATCH (a:Column), (b:Column)
            WHERE a.db = $db AND b.db = $db
              AND a.schema = $schema AND b.schema = $schema
              AND a.name = b.name
              AND a.table < b.table
              AND a.name IN $hint_keys
            WITH a, b,
                 CASE
                   WHEN any(pfx IN $agg_pfxs WHERE a.table STARTS WITH pfx + '_')
                        AND any(pfx IN $agg_pfxs WHERE b.table STARTS WITH pfx + '_')
                   THEN 0.85
                   ELSE 0.7
                 END AS conf
            MERGE (a)-[r:JOINS_ON {via_key: a.name}]->(b)
              ON CREATE SET r.confidence = conf, r.source = 'name_heuristic'
              ON MATCH  SET r.confidence = CASE WHEN coalesce(r.confidence, 0) < conf THEN conf ELSE r.confidence END
            RETURN count(r) AS n
            """,
            db=db_id,
            schema=schema,
            hint_keys=hint_keys,
            agg_pfxs=agg_prefixes,
        )
        total += int(res.single()["n"])

    # --- step B：id_pattern 正则（Neo4j 5 用 apoc.text.regexGroups / WHERE col.name =~ pattern）---
    if id_pattern:
        res = tx.run(
            """
            MATCH (a:Column), (b:Column)
            WHERE a.db = $db AND b.db = $db
              AND a.schema = $schema AND b.schema = $schema
              AND a.name = b.name
              AND a.table < b.table
              AND NOT a.name IN $hint_keys
              AND a.name =~ $pattern
            MERGE (a)-[r:JOINS_ON {via_key: a.name}]->(b)
              ON CREATE SET r.confidence = 0.6, r.source = 'id_pattern'
              ON MATCH  SET r.confidence = CASE WHEN coalesce(r.confidence, 0) < 0.6 THEN 0.6 ELSE r.confidence END
            RETURN count(r) AS n
            """,
            db=db_id,
            schema=schema,
            hint_keys=hint_keys,
            pattern=id_pattern,
        )
        total += int(res.single()["n"])

    return total


def _write_ds_overlap_joins(tx, *, db_id: str, schema: str, keys: list[str]) -> int:
    """两表都有 ds + 在 overlap 列表里 → JOINS_ON (confidence=0.5)。"""
    res = tx.run(
        """
        MATCH (ta:Table {db: $db, schema: $schema})-[:HAS_COLUMN]->(:Column {name: 'ds'})
        MATCH (tb:Table {db: $db, schema: $schema})-[:HAS_COLUMN]->(:Column {name: 'ds'})
        WHERE ta.name < tb.name
        WITH ta, tb
        MATCH (ta)-[:HAS_COLUMN]->(a:Column)
        MATCH (tb)-[:HAS_COLUMN]->(b:Column)
        WHERE a.name = b.name AND a.name IN $keys
        MERGE (a)-[r:JOINS_ON {via_key: a.name}]->(b)
          ON CREATE SET r.confidence = 0.5, r.source = 'ds_overlap'
        RETURN count(r) AS n
        """,
        db=db_id,
        schema=schema,
        keys=keys,
    )
    return int(res.single()["n"])


def _write_manual_overrides(tx, *, db_id: str, overrides: list[dict]) -> int:
    """手动 override：``schema.table.column`` 字符串 → 找到列后建 confidence=1.0 JOINS_ON。"""
    if not overrides:
        return 0
    res = tx.run(
        """
        UNWIND $overrides AS o
        WITH o, split(o.left, '.') AS lp, split(o.right, '.') AS rp
        MATCH (a:Column {db: $db, schema: lp[0], table: lp[1], name: lp[2]})
        MATCH (b:Column {db: $db, schema: rp[0], table: rp[1], name: rp[2]})
        MERGE (a)-[r:JOINS_ON {via_key: o.via}]->(b)
          ON CREATE SET r.confidence = 1.0, r.source = 'manual'
          ON MATCH  SET r.confidence = 1.0, r.source = 'manual'
        RETURN count(r) AS n
        """,
        db=db_id,
        overrides=overrides,
    )
    return int(res.single()["n"])


def _upgrade_jaccard_confidence(tx, *, db_id: str, schema: str, threshold: float = 0.3) -> int:
    """对已有 JOINS_ON 边，若两端列都有 sample_values 且 Jaccard 重叠 > threshold，
    则把 confidence 升至 0.8（高于 id-like 规则的 0.6 但不覆盖手动 1.0）。

    Neo4j Cypher 中用集合交集大小 / 并集大小算 Jaccard。
    只处理 sample_values 非空的列（物理层反射时写入）。
    """
    res = tx.run(
        """
        MATCH (a:Column)-[r:JOINS_ON]->(b:Column)
        WHERE a.db = $db AND b.db = $db
          AND a.schema = $schema AND b.schema = $schema
          AND r.confidence < 0.8
          AND size(coalesce(a.sample_values, [])) > 0
          AND size(coalesce(b.sample_values, [])) > 0
        WITH a, b, r,
             [x IN a.sample_values WHERE x IN b.sample_values] AS intersect,
             [x IN a.sample_values + b.sample_values | x] AS union_raw
        WITH a, b, r, intersect,
             size(apoc.coll.toSet(union_raw)) AS union_size,
             size(intersect) AS inter_size
        WHERE union_size > 0
          AND toFloat(inter_size) / toFloat(union_size) >= $threshold
        SET r.confidence = 0.8,
            r.source     = CASE WHEN r.source IS NULL OR r.source = 'id_pattern'
                                THEN 'jaccard' ELSE r.source + '+jaccard' END
        RETURN count(r) AS n
        """,
        db=db_id,
        schema=schema,
        threshold=threshold,
    )
    return int(res.single()["n"])


__all__ = [
    "DEFAULT_MANUAL_OVERRIDES",
    "DS_DIMENSIONAL_OVERLAP",
    "JOIN_KEY_WHITELIST",
    "ManualOverride",
    "write_join_inference",
]

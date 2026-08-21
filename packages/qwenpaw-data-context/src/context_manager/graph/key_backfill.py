"""为缺失 ``key`` 的 Neo4j 结点批量赋值。

设计文档（§2）要求 ``key`` 为稳定唯一标识，以便 ``MERGE``、Topology UI、
:func:`expand_node_snapshot` 等按 ``{key: ...}`` 定位结点。

存量数据或外部导入可能缺少 ``key``；本模块提供一次性兜底写入（**不改**已有非空 ``key``）。

兜底格式：``<主标签>:<internal_id>``（``id(n)``），在同一库内唯一。
多标签结点取 ``head(labels(n))``（字母序首标签）；建议手工设 ``key`` 避免歧义。
"""
from __future__ import annotations

from typing import Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session

log = get_logger("graph.key_backfill")


def backfill_missing_node_keys(
    driver: Driver,
    *,
    database: Optional[str] = None,
) -> dict[str, int]:
    """将 ``key`` 为空或缺失的结点设为 ``主标签 + ':' + toString(id(n))``。

    Returns:
        各主标签名 → 本次写入结点数（仅统计 SET 命中的行）。
    """
    counts: dict[str, int] = {}
    with neo4j_session(driver, database=database) as s:
        rows = s.run(
            """
            MATCH (n)
            WHERE n.key IS NULL OR trim(toString(n.key)) = ''
            WITH n, head(labels(n)) AS lbl
            SET n.key = lbl + ':' + toString(id(n))
            RETURN lbl AS label, count(*) AS n
            """
        ).data()
    for r in rows:
        lbl = str(r.get("label") or "?")
        counts[lbl] = int(r.get("n") or 0)
    total = sum(counts.values())
    log.info("backfill_missing_node_keys: set key on %s nodes label_counts=%s", total, counts)
    return counts


__all__ = ["backfill_missing_node_keys"]

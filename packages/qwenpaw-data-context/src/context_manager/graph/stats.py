"""图统计辅助函数。"""
from __future__ import annotations

from neo4j import Driver


def count_semantic_nodes(driver: Driver) -> dict[str, int]:
    """统计图中各 status 的语义节点数。"""
    from ..utils import neo4j_session

    try:
        with neo4j_session(driver) as s:
            rows = s.run(
                "MATCH (n) WHERE n:Metric OR n:Dimension OR n:Formula "
                "RETURN coalesce(n.status, 'stable') AS status, count(n) AS cnt"
            ).data()
        return {r["status"]: r["cnt"] for r in rows}
    except Exception:
        return {}

"""Neo4j fulltext/vector helpers used by knowledge conflict screening."""
from __future__ import annotations

from typing import Any

from ..embedder import embed_one
from ..utils import get_logger, neo4j_session

log = get_logger("knowledge.resolve")


def _fulltext_top(
    driver: Any,
    index_name: str,
    query: str,
    *,
    k: int = 8,
) -> list[dict[str, Any]]:
    q = (query or "").strip()
    if not q:
        return []
    cypher = (
        f"CALL db.index.fulltext.queryNodes('{index_name}', $q) "
        "YIELD node, score RETURN node.key AS key, score ORDER BY score DESC LIMIT $k"
    )
    try:
        with neo4j_session(driver) as s:
            return s.run(cypher, q=q, k=k).data()
    except Exception as exc:  # noqa: BLE001
        log.warning("fulltext queryNodes failed index=%s q=%r: %s", index_name, q[:80], exc)
        return []


def _vector_top(
    driver: Any,
    index_name: str,
    text: str,
    *,
    k: int = 5,
) -> list[dict[str, Any]]:
    t = (text or "").strip()
    if not t:
        return []
    try:
        emb = embed_one(t)
    except Exception as exc:  # noqa: BLE001
        log.warning("embed_one failed: %s", exc)
        return []
    cypher = f"""
    CALL db.index.vector.queryNodes('{index_name}', $k, $emb) YIELD node, score
    WHERE (node.valid_to IS NULL OR node.valid_to > datetime())
    RETURN node.key AS key, score ORDER BY score DESC LIMIT $k
    """
    try:
        with neo4j_session(driver) as s:
            return s.run(cypher, k=k, emb=emb).data()
    except Exception as exc:  # noqa: BLE001
        log.warning("vector queryNodes failed index=%s: %s", index_name, exc)
        return []

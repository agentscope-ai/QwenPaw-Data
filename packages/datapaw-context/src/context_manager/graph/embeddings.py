"""为 ``Metric / Dimension / Column / Event / Entity`` 节点写入语义向量。

设计：

- **canonical_text**：每种 label 取固定字段拼一段「描述文本」（见 :func:`_canonical_text`），
  这段文本 + 模型名 hash 一下作为 ``embedding_hash``。
  ``Column`` 即使没有 comment / description / synonyms 也会嵌入（表.列 + SQL 类型等标识信息）。
- **幂等**：节点已有 ``embedding_hash`` 且与当前 hash 一致 → 跳过；不一致或没有 → 重写。
- **批量**：每 ``batch_size`` 条调一次 :func:`context_manager.embedder.embed`，再用 UNWIND 一次性写回。

使用：

    python scripts/setup/index_embeddings.py --scope all
    python scripts/setup/index_embeddings.py --scope metric,dimension,event,entity --reset
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Iterable, Iterator, Optional

from neo4j import Driver

from ..config import CFG
from ..embedder import embed, warmup_embedding_model
from ..utils import get_logger, neo4j_session
from .schema_init import detect_vector_dim_mismatch, init_vector_indexes

log = get_logger("graph.embeddings")


SCOPE_ALL = ("metric", "dimension", "column", "datasetcolumn", "dataset", "event", "entity")


@dataclass
class EmbedStats:
    label: str
    total: int = 0          # 该 label 的全部节点数
    written: int = 0        # 本次新写/更新的节点数
    skipped: int = 0        # hash 命中跳过
    unembedded: int = 0     # 跑完后仍缺少 embedding 的节点数（覆盖率缺口）
    empty_text: int = 0     # _canonical_text 返回空、无法生成 embedding 的节点数
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------- #
# 文本归一化
# ---------------------------------------------------------------------- #
def _norm_list(v) -> list[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [str(v).strip()]


def _canonical_text(label: str, props: dict) -> str:
    """每种 label 的"用于嵌入的文本"。命中率最高的字段堆在前面。"""
    name = (props.get("name") or "").strip()
    if label == "Metric":
        synonyms = "、".join(_norm_list(props.get("aliases")))
        desc = (props.get("description") or "").strip()
        unit = (props.get("unit") or "").strip()
        domain = (props.get("domain") or "").strip()
        tags = "、".join(_norm_list(props.get("tags")))
        text = f"{name}"
        if synonyms:
            text += f"，又称 {synonyms}"
        if domain:
            text += f"，属于 {domain} 业务域"
        if unit:
            text += f"，单位 {unit}"
        if tags:
            text += f"，标签 {tags}"
        if desc:
            text += f"。{desc}"
        return text
    if label == "Dimension":
        synonyms = "、".join(_norm_list(props.get("aliases")))
        desc = (props.get("description") or "").strip()
        domain = (props.get("domain") or "").strip()
        text = f"维度：{name}"
        if synonyms:
            text += f"（{synonyms}）"
        if domain:
            text += f"，属于 {domain}"
        if desc:
            text += f"。{desc}"
        return text
    if label == "Column":
        comment = (props.get("comment") or "").strip()
        description = (props.get("description") or "").strip()
        synonyms_list = _norm_list(props.get("aliases"))
        synonyms = "、".join(synonyms_list)
        table = (props.get("table") or "").strip()
        col_type = (props.get("type") or "").strip()
        # 无注释/别名/独立 description 时仍嵌入：表.列 + SQL 类型强化业务标识
        # comment / description 都是语义信号，二者都拼上；aliases 是 ddl.txt
        # view alias 派生的业务别名（如 ``landingpagevisit_usercnt_1d`` → "DAU_1d"），
        # 直接进 embedding 让向量检索也能命中"DAU"等业务名
        text = f"列 {table}.{name}" if table else f"列 {name}"
        bare = not comment and not description and not synonyms_list
        if col_type and bare:
            text += f"，类型 {col_type}"
        if comment:
            text += f"：{comment}"
        if description and description != comment:
            text += f"。{description}"
        if synonyms:
            text += f"（业务别名 {synonyms}）"
        return text
    if label == "Event":
        desc = (props.get("description") or "").strip()
        if desc:
            return desc
        name = (props.get("name") or "").strip()
        et = (props.get("type") or "").strip()
        return f"{name} {et}".strip() or name
    if label == "DatasetColumn":
        display_name = (props.get("display_name") or "").strip()
        aliases = "、".join(_norm_list(props.get("aliases")))
        desc = (props.get("description") or "").strip()
        sv = " ".join(_norm_list(props.get("sample_values"))[:8])
        text = f"{name}"
        if display_name and display_name != name:
            text += f" | {display_name}"
        if aliases:
            text += f" | {aliases}"
        if desc:
            text += f"。{desc}"
        if sv:
            text += f"。取值如 {sv}"
        return text
    if label == "Entity":
        desc = (props.get("description") or "").strip()
        if desc:
            return desc
        name = (props.get("name") or "").strip()
        aliases = " ".join(_norm_list(props.get("aliases")))[:500]
        et = (props.get("type") or "").strip()
        parts = [name]
        if aliases:
            parts.append(aliases)
        if et:
            parts.append(et)
        return " ".join(p for p in parts if p).strip()
    if label == "Dataset":
        desc = (props.get("description") or "").strip()
        ds_type = (props.get("dataset_type") or "").strip()
        filter_sum = (props.get("filter_summary") or "").strip()
        text = name
        if ds_type:
            text += f"，类型 {ds_type}"
        if filter_sum:
            text += f"，筛选条件 {filter_sum}"
        if desc:
            text += f"。{desc}"
        return text
    return name


def _hash(model_name: str, text: str) -> str:
    h = hashlib.sha1()
    h.update(model_name.encode("utf-8"))
    h.update(b"\x1f")
    h.update(text.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------- #
# Driver/Cypher
# ---------------------------------------------------------------------- #
_FETCH_CYPHER = {
    "Metric": """
    MATCH (n:Metric)
    WHERE (n.valid_to IS NULL OR n.valid_to > datetime())
    RETURN n.key AS key, n.name AS name, coalesce(n.aliases, []) AS aliases,
           n.description AS description, n.unit AS unit, n.domain AS domain,
           n.tags AS tags, n.embedding_hash AS embedding_hash
    """,
    "Dimension": """
    MATCH (n:Dimension)
    RETURN n.key AS key, n.name AS name, coalesce(n.aliases, []) AS aliases,
           n.description AS description, n.domain AS domain,
           n.embedding_hash AS embedding_hash
    """,
    "Column": """
    MATCH (n:Column)
    RETURN n.key AS key, n.name AS name, n.comment AS comment,
           n.description AS description, coalesce(n.aliases, []) AS aliases,
           n.table AS table, n.type AS type, n.embedding_hash AS embedding_hash
    """,
    "Event": """
    MATCH (n:Event)
    WHERE (n.valid_to IS NULL OR n.valid_to > datetime())
      AND (coalesce(n.description, '') <> '' OR coalesce(n.name, '') <> '')
    RETURN n.key AS key, n.name AS name, n.description AS description, n.type AS type,
           n.embedding_hash AS embedding_hash
    """,
    "Entity": """
    MATCH (n:Entity)
    WHERE coalesce(n.description, '') <> '' OR coalesce(n.name, '') <> ''
    RETURN n.key AS key, n.name AS name, n.description AS description, n.type AS type,
           coalesce(n.aliases, []) AS aliases, n.embedding_hash AS embedding_hash
    """,
    "DatasetColumn": """
    MATCH (n:DatasetColumn)
    RETURN n.key AS key, n.name AS name, n.display_name AS display_name,
           coalesce(n.aliases, []) AS aliases, n.description AS description,
           coalesce(n.sample_values, []) AS sample_values,
           n.embedding_hash AS embedding_hash
    """,
    "Dataset": """
    MATCH (n:Dataset)
    RETURN n.key AS key, n.name AS name, n.description AS description,
           n.dataset_type AS dataset_type, n.filter_summary AS filter_summary,
           n.embedding_hash AS embedding_hash
    """,
}

_LABEL_ALIASES = {
    "metric": "Metric",
    "dimension": "Dimension",
    "dim": "Dimension",
    "column": "Column",
    "col": "Column",
    "datasetcolumn": "DatasetColumn",
    "dscol": "DatasetColumn",
    "dataset": "Dataset",
    "event": "Event",
    "entity": "Entity",
}


def _iter_pending(driver: Driver, label: str, model_name: str,
                  *, reset: bool) -> Iterator[tuple[str, str, str]]:
    """yield ``(key, canonical_text, new_hash)``，其中 hash 与当前不一致的才会被产出。"""
    cypher = _FETCH_CYPHER[label]
    with neo4j_session(driver) as s:
        rows = s.run(cypher).data()
    for r in rows:
        text = _canonical_text(label, r)
        if not text:
            continue
        new_hash = _hash(model_name, text)
        if not reset and r.get("embedding_hash") == new_hash:
            continue
        yield r["key"], text, new_hash


def _count_empty_text(driver: Driver, label: str) -> int:
    """Count nodes whose ``_canonical_text`` would be empty → silently skipped."""
    cypher = _FETCH_CYPHER.get(label)
    if not cypher:
        return 0
    with neo4j_session(driver) as s:
        rows = s.run(cypher).data()
    return sum(1 for r in rows if not _canonical_text(label, r))


def _count_unembedded(driver: Driver, label: str) -> int:
    """Count nodes in ``label`` that have no ``embedding`` property (coverage gap)."""
    with neo4j_session(driver) as s:
        rec = s.run(
            f"MATCH (n:{label}) WHERE n.embedding IS NULL RETURN count(n) AS c"
        ).single()
    return int(rec["c"]) if rec else 0


_WRITE_CYPHER = """
UNWIND $rows AS row
MATCH (n {key: row.key})
SET n.embedding = row.vec, n.embedding_hash = row.h
"""


def _write_batch(driver: Driver, rows: list[dict]) -> None:
    """批量回写 ``embedding`` + ``embedding_hash``。"""
    with neo4j_session(driver) as s:
        s.run(_WRITE_CYPHER, rows=rows)


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def index_embeddings(
    driver: Driver,
    *,
    scope: Iterable[str] = SCOPE_ALL,
    batch_size: int = 32,
    reset: bool = False,
    ensure_indexes: bool = True,
) -> list[EmbedStats]:
    """对指定 scope 的节点写 embedding。

    Args:
        scope:           ``("metric", "dimension", "column", "event", "entity")`` 的子集；不区分大小写
        batch_size:      送给 ``embed()`` 的每批文本数
        reset:           True 时忽略 ``embedding_hash``，全量重算（向量维度切换后必须）
        ensure_indexes:  True 时先 :func:`init_vector_indexes`（IF NOT EXISTS，无副作用）
    """
    # 兼容传字符串（如 scope="all" 或 scope="metric"）的情况：
    # 字符串本身是 Iterable[str]，会被逐字符遍历成 'a'/'l'/'l'，全部 miss _LABEL_ALIASES，
    # 导致 embedding 静默跳过（weave 后向量检索不可用）。此处统一规整为单元素列表。
    if isinstance(scope, str):
        scope = [scope]
    norm_scope = []
    for s in scope:
        s = s.strip().lower()
        if s == "all":
            norm_scope = list(SCOPE_ALL)
            break
        norm_scope.append(s)
    labels = []
    for s in norm_scope:
        if s in _LABEL_ALIASES and _LABEL_ALIASES[s] not in labels:
            labels.append(_LABEL_ALIASES[s])

    if ensure_indexes:
        bad = detect_vector_dim_mismatch(driver, expected_dim=CFG.embed_dim)
        if bad:
            log.warning("dropping vector indexes with mismatched dim: %s", bad)
            init_vector_indexes(driver, force_recreate=True)
            # mismatch 时旧 embedding 数据也无意义，强制 reset
            reset = True
        else:
            init_vector_indexes(driver)

    warmup_embedding_model()
    log.info("model=%s dim=%d", CFG.embed_model, CFG.embed_dim)

    out: list[EmbedStats] = []
    for label in labels:
        t0 = time.time()
        # 总数 / 待处理数（先迭代一遍取出 pending，便于打 progress）
        with neo4j_session(driver) as s:
            total = s.run(f"MATCH (n:{label}) RETURN count(n) AS c").single()["c"]

        pending = list(_iter_pending(driver, label, CFG.embed_model, reset=reset))
        if not pending:
            # "no pending" 有两种可能，必须区分：
            #   (a) 所有节点 hash 命中 → 真正 up-to-date
            #   (b) 节点实际缺少 embedding 但被 _canonical_text 空文本过滤掉 → 静默缺口
            # 跑一次覆盖率断言把 (b) 暴露出来（之前这里一律 log "all up-to-date"
            # 导致 appdata Metric 0/151 静默通过了 make setup）。
            unemb = _count_unembedded(driver, label)
            empty_txt = _count_empty_text(driver, label)
            elapsed_ms = (time.time() - t0) * 1000
            stats = EmbedStats(
                label=label, total=total, written=0,
                skipped=total - empty_txt,
                unembedded=unemb, empty_text=empty_txt,
                elapsed_ms=elapsed_ms,
            )
            if unemb > 0:
                log.warning(
                    "[%s] COVERAGE GAP: %d / %d nodes have NO embedding "
                    "(empty_text=%d, hash-skip=%d). "
                    "Run with --reset if hashes are stale.",
                    label, unemb, total, empty_txt, total - empty_txt,
                )
            else:
                log.info("[%s] all up-to-date (%d nodes)", label, total)
            out.append(stats)
            continue

        log.info("[%s] computing embeddings for %d / %d nodes (batch=%d)",
                 label, len(pending), total, batch_size)

        written = 0
        for chunk_start in range(0, len(pending), batch_size):
            chunk = pending[chunk_start: chunk_start + batch_size]
            texts = [t for _, t, _ in chunk]
            vecs = embed(texts)
            rows = [
                {"key": k, "vec": v, "h": h}
                for (k, _, h), v in zip(chunk, vecs)
            ]
            _write_batch(driver, rows)
            written += len(rows)
            if (chunk_start // batch_size) % 5 == 0 or chunk_start + batch_size >= len(pending):
                log.info("[%s] %d / %d written", label, written, len(pending))

        elapsed_ms = (time.time() - t0) * 1000
        # Post-write coverage check: catch partial failures (embed() returned
        # zeros for some texts, write tx partially failed, etc.)
        unemb = _count_unembedded(driver, label)
        empty_txt = _count_empty_text(driver, label)
        stats = EmbedStats(
            label=label, total=total, written=written,
            skipped=total - written,
            unembedded=unemb, empty_text=empty_txt,
            elapsed_ms=elapsed_ms,
        )
        if unemb > 0:
            log.warning(
                "[%s] COVERAGE GAP after write: %d / %d nodes still have no embedding "
                "(written=%d, empty_text=%d)",
                label, unemb, total, written, empty_txt,
            )
        else:
            log.info("[%s] done: written=%d skipped=%d total=%d elapsed=%.0fms",
                     label, stats.written, stats.skipped, stats.total, stats.elapsed_ms)
        out.append(stats)

    return out


__all__ = ["EmbedStats", "SCOPE_ALL", "index_embeddings"]

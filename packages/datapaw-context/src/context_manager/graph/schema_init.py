"""Neo4j 唯一约束 + 索引初始化（``graph_topology_v4.md``）。

把所有 ``CREATE CONSTRAINT`` / ``CREATE INDEX`` 集中到这里，按需调一次即可。

- 唯一约束按 ``key`` 单字段建；
- 全文索引为同义词消歧 / NL 入图准备；
- 向量索引维度从 ``CFG.embed_dim`` 读，与既有 ``embedder`` 对齐；
- 普通 INDEX 是评测/召回热点（task signature、event 日期范围、strategy 等）。

所有语句都加 ``IF NOT EXISTS``，可重复跑。

v4 变更：
- ``MetricFormula`` → ``Formula``；``StrategyCard`` → ``Strategy``；``AnalyticalOperator`` → ``Operator``
- 新增 ``Dataset``、``Turn``、``Experience``；移除 Policy/Document/KnowledgeChunk/AnomalyRule/ConflictQueue 约束
- ``CAN_DRILL_BY`` 在数据层改为 ``ANALYZED_BY``（索引不建关系名）
- 向量索引 ``card_vec`` → ``strategy_vec``（``Strategy`` + ``signature_emb``）
"""
from __future__ import annotations

from typing import Iterable

from neo4j import Driver

from ..config import CFG
from ..utils import get_logger, neo4j_session

log = get_logger("graph.schema_init")


# ---------------------------------------------------------------------- #
# §5 / §13.3：唯一约束（节点 key）
# ---------------------------------------------------------------------- #
_UNIQUE_CONSTRAINTS: tuple[tuple[str, str], ...] = (
    ("db_key", "Database"),
    ("sch_key", "Schema"),
    ("tbl_key", "Table"),
    ("col_key", "Column"),
    ("dom_key", "Domain"),
    ("dsrc_key", "DataSource"),
    ("met_key", "Metric"),
    ("fml_key", "Formula"),
    ("dim_key", "Dimension"),
    ("dv_key", "DimensionValue"),
    ("op_key", "Operator"),
    ("ds_key", "Dataset"),
    ("dscol_key", "DatasetColumn"),
    ("cal_key", "Caliber"),
    # Trace
    ("task_key", "Task"),
    ("step_key", "Step"),
    ("tc_key", "ToolCall"),
    ("claim_key", "Claim"),
    ("turn_key", "Turn"),
    ("exp_key", "Experience"),
    ("card_key", "Strategy"),
    ("tag_name", "Tag"),
    ("session_key", "Session"),
    ("user_key", "User"),
    # Knowledge
    ("ev_key", "Event"),
    ("ent_key", "Entity"),
)


# ---------------------------------------------------------------------- #
# §5：全文索引（同义词消歧 / NL → 图）
# 4-tuple format: (index_name, label, fields, analyzer). analyzer=None → default.
# ``cjk`` analyzer is required for CJK tokenization — without it, queries like
# "发布" fail because the default analyzer treats the whole string as one term.
# ---------------------------------------------------------------------- #
_FULLTEXT_INDEXES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("metric_text", "Metric", ("name", "description", "aliases"), "cjk"),
    ("dim_text", "Dimension", ("name", "description", "aliases"), "cjk"),
    ("col_text", "Column", ("name", "comment", "description", "aliases"), "cjk"),
    ("claim_text", "Claim", ("text", "predicate"), "cjk"),
    ("dscol_text", "DatasetColumn", ("name", "display_name", "aliases", "description"), "cjk"),
    ("ds_text", "Dataset", ("name", "description", "dataset_type", "filter_summary"), "cjk"),
    # Event / Entity：name + description（含 title/正文），CJK 分词
    ("event_text", "Event", ("name", "description"), "cjk"),
    ("strategy_text", "Strategy", ("strategy_semantics", "task_signature"), "cjk"),
    ("entity_text", "Entity", ("description",), "cjk"),
    ("tag_text", "Tag", ("name",), "cjk"),
)


# ---------------------------------------------------------------------- #
# §5：向量索引（第三列为节点上的向量属性名）
# ---------------------------------------------------------------------- #
_VECTOR_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("col_vec", "Column", "embedding"),
    ("dscol_vec", "DatasetColumn", "embedding"),
    ("ds_vec", "Dataset", "embedding"),
    ("met_vec", "Metric", "embedding"),
    ("dim_vec", "Dimension", "embedding"),
    ("strategy_vec", "Strategy", "signature_emb"),
    ("ev_vec", "Event", "embedding"),
    ("ent_vec", "Entity", "embedding"),
    ("claim_vec", "Claim", "embedding"),
)


# ---------------------------------------------------------------------- #
# §13.3：普通索引（评测/召回热点）
# ---------------------------------------------------------------------- #
_BTREE_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("task_sig", "Task", ("task_signature",)),
    ("trace_session_id", "Session", ("session_id",)),
    ("event_date_range", "Event", ("date_from", "date_to")),
    ("table_legacy_db_name", "Table", ("db", "name")),
    ("column_legacy_db_table_name", "Column", ("db", "table", "name")),
    ("card_graph_db", "Strategy", ("graph_db_id",)),
    ("card_tier", "Strategy", ("memory_tier",)),
    ("card_valid", "Strategy", ("valid_to",)),
    ("claim_valid", "Claim", ("valid_to",)),
    ("fml_valid", "Formula", ("valid_to",)),
    ("cal_valid", "Caliber", ("valid_to",)),
    ("ev_valid", "Event", ("valid_to",)),
    # datasource_id 跨图过滤热点：每个承载该属性的 label 各建一个 B-tree 索引，
    # 使 ``WHERE n.datasource_id = $ds`` 走索引扫描而非全表。
    ("dsrc_domain", "Domain", ("datasource_id",)),
    ("dsrc_metric", "Metric", ("datasource_id",)),
    ("dsrc_dimension", "Dimension", ("datasource_id",)),
    ("dsrc_dimensionvalue", "DimensionValue", ("datasource_id",)),
    ("dsrc_formula", "Formula", ("datasource_id",)),
    ("dsrc_caliber", "Caliber", ("datasource_id",)),
    ("dsrc_database", "Database", ("datasource_id",)),
    ("dsrc_schema", "Schema", ("datasource_id",)),
    ("dsrc_table", "Table", ("datasource_id",)),
    ("dsrc_column", "Column", ("datasource_id",)),
    # Trace Graph 节点按 datasource_id 过滤
    ("dsrc_task", "Task", ("datasource_id",)),
    ("dsrc_step", "Step", ("datasource_id",)),
    ("dsrc_toolcall", "ToolCall", ("datasource_id",)),
    ("dsrc_session", "Session", ("datasource_id",)),
)


def init_constraints(driver: Driver) -> None:
    """逐条建唯一约束。所有语句 ``IF NOT EXISTS``。"""
    with neo4j_session(driver) as s:
        for cname, label in _UNIQUE_CONSTRAINTS:
            s.run(
                f"CREATE CONSTRAINT {cname} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.key IS UNIQUE"
            )
    log.info("constraints: %d unique key constraints ensured", len(_UNIQUE_CONSTRAINTS))


def init_fulltext_indexes(driver: Driver) -> None:
    """全文索引（Metric / Dimension / Column / Event / Strategy / Entity）。

    Event / Entity 仅索引 ``description``，便于问句与释义精确匹配。
    """
    with neo4j_session(driver) as s:
        existing: dict[str, tuple[list[str], str]] = {}
        try:
            for r in s.run(
                "SHOW INDEXES YIELD name, type, properties, options WHERE type = 'FULLTEXT' "
                "RETURN name, properties, options"
            ).data():
                opts = r.get("options") or {}
                analyzer = ((opts.get("indexConfig") or {}).get("fulltext.analyzer")) or ""
                existing[str(r.get("name") or "")] = (
                    [str(p) for p in (r.get("properties") or [])],
                    str(analyzer),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("init_fulltext_indexes: SHOW INDEXES failed (%s); skipping diff", exc)

        recreated = 0
        for name, label, fields, analyzer in _FULLTEXT_INDEXES:
            wanted = set(fields)
            cur_fields, cur_analyzer = existing.get(name, ([], ""))
            field_drift = bool(cur_fields) and set(cur_fields) != wanted
            analyzer_drift = bool(cur_fields) and (analyzer or "") and (cur_analyzer or "") != analyzer
            if field_drift or analyzer_drift:
                s.run(f"DROP INDEX {name} IF EXISTS")
                recreated += 1
                log.info(
                    "fulltext index %s: drifted (fields %s→%s, analyzer %s→%s); rebuilt",
                    name, sorted(cur_fields), sorted(wanted), cur_analyzer or "default", analyzer or "default",
                )
            field_list = ", ".join(f"n.{f}" for f in fields)
            if analyzer:
                s.run(
                    f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS "
                    f"FOR (n:{label}) ON EACH [{field_list}] "
                    f"OPTIONS {{ indexConfig: {{ `fulltext.analyzer`: '{analyzer}' }} }}"
                )
            else:
                s.run(
                    f"CREATE FULLTEXT INDEX {name} IF NOT EXISTS "
                    f"FOR (n:{label}) ON EACH [{field_list}]"
                )
        # Drop v3-only indexes if names changed
        for legacy in ("policy_text",):
            s.run(f"DROP INDEX {legacy} IF EXISTS")
    log.info(
        "fulltext indexes: %d ensured (%d rebuilt to match new field sets)",
        len(_FULLTEXT_INDEXES), recreated,
    )


def init_vector_indexes(
    driver: Driver,
    *,
    embed_dim: int | None = None,
    force_recreate: bool = False,
) -> None:
    """向量索引：Column / Metric / Dimension / Event / Entity 使用 ``embedding``；Strategy 使用 ``signature_emb``。"""
    dim = embed_dim or CFG.embed_dim
    indexes = list(_VECTOR_INDEXES)
    with neo4j_session(driver) as s:
        if force_recreate:
            for name, _, _ in _VECTOR_INDEXES:
                s.run(f"DROP INDEX {name} IF EXISTS")
            s.run("DROP INDEX card_vec IF EXISTS")
            log.info("vector indexes: %d dropped (force_recreate=True)", len(_VECTOR_INDEXES))
        else:
            s.run("DROP INDEX card_vec IF EXISTS")
        for name, label, vec_prop in indexes:
            s.run(
                f"""
                CREATE VECTOR INDEX {name} IF NOT EXISTS
                FOR (n:{label}) ON (n.{vec_prop})
                OPTIONS {{ indexConfig: {{
                    `vector.dimensions`: {dim},
                    `vector.similarity_function`: 'cosine'
                }} }}
                """
            )
    log.info(
        "vector indexes: %d ensured (dim=%d)",
        len(indexes), dim,
    )


def detect_vector_dim_mismatch(driver: Driver, *, expected_dim: int | None = None) -> list[str]:
    """返回所有 ``vector.dimensions`` 与 ``expected_dim`` 不一致的索引名。"""
    dim = expected_dim or CFG.embed_dim
    cypher = """
    SHOW INDEXES YIELD name, type, options
    WHERE type = 'VECTOR'
    RETURN name, options
    """
    bad: list[str] = []
    with neo4j_session(driver) as s:
        for r in s.run(cypher).data():
            opts = r.get("options") or {}
            cfg = opts.get("indexConfig") or {}
            d = cfg.get("vector.dimensions")
            if d is not None and int(d) != int(dim):
                bad.append(r["name"])
    return bad


def init_btree_indexes(driver: Driver) -> None:
    """普通索引（evaluation/retrieval hot path）。"""
    with neo4j_session(driver) as s:
        for name, label, fields in _BTREE_INDEXES:
            cols = ", ".join(f"n.{f}" for f in fields)
            s.run(
                f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON ({cols})"
            )
        # Drop v3 btree indexes that no longer apply
        for legacy in ("card_task_type", "pol_valid", "policy_target"):
            s.run(f"DROP INDEX {legacy} IF EXISTS")
    log.info("btree indexes: %d ensured", len(_BTREE_INDEXES))


def init_zone_index(driver: Driver) -> None:
    """``zone`` 是跨图过滤兜底字段。"""
    labels = (
        "DataSource",
        "Database",
        "Schema",
        "Table",
        "Column",
        "Domain",
        "Metric",
        "Formula",
        "Dimension",
        "DimensionValue",
        "Operator",
        "Dataset",
        "DatasetColumn",
        "Caliber",
        "Task",
        "Step",
        "ToolCall",
        "Claim",
        "Turn",
        "Experience",
        "Strategy",
        "Tag",
        "Session",
        "User",
        "Event",
        "Entity",
    )
    with neo4j_session(driver) as s:
        for label in labels:
            s.run(
                f"CREATE INDEX zone_{label.lower()} IF NOT EXISTS "
                f"FOR (n:{label}) ON (n.zone)"
            )
    log.info("zone indexes: %d labels", len(labels))


def drop_old_experience_indexes(driver: Driver) -> None:
    """迁移用：删掉 v2 Experience 相关约束与索引（IF EXISTS 安全）。"""
    with neo4j_session(driver) as s:
        s.run("DROP CONSTRAINT exp_key IF EXISTS")
        s.run("DROP INDEX zone_experience IF EXISTS")
    log.info("v2 Experience constraints/indexes dropped (if they existed)")


def init_all(driver: Driver, *, embed_dim: int | None = None) -> None:
    """建全部约束/索引。重复运行无副作用。"""
    init_constraints(driver)
    init_fulltext_indexes(driver)
    init_vector_indexes(driver, embed_dim=embed_dim)
    init_btree_indexes(driver)
    init_zone_index(driver)


# 物理层与 legacy ``GraphWriter`` / ``ingest_physical`` 共用；``drop_topology`` 默认不删，
# 否则 ``make setup DROP_TOPOLOGY=1`` 会在 build_graph 之后清空 Database/Table/Column。
_PHYSICAL_GRAPH_LABELS = frozenset({"Database", "Schema", "Table", "Column"})

_TOPOLOGY_SEMANTIC_LABELS: tuple[str, ...] = (
            "DataSource",
            "Domain",
            "Metric",
            "Formula",
            "Dimension",
            "DimensionValue",
            "Operator",
            "Dataset",
            "DatasetColumn",
            "Caliber",
            "Task",
            "Step",
            "ToolCall",
            "Claim",
            "Turn",
            "Experience",
            "Strategy",
            "Event",
            "Entity",
            # v3 legacy labels (clean rebuild)
            "MetricFormula",
            "StrategyCard",
            "AnalyticalOperator",
            "AnomalyRule",
            "Policy",
            "Document",
            "KnowledgeChunk",
            "ConflictQueue",
        )


def drop_topology(
    driver: Driver,
    *,
    labels: Iterable[str] | None = None,
    include_physical: bool = False,
) -> None:
    """按 label 删节点；不写在生产 CLI 里，仅作为开发期 reset 用。

    默认 **不** 删除 ``Database`` / ``Schema`` / ``Table`` / ``Column``，
    以便 ``setup --no-physical`` 在 ``DROP_TOPOLOGY=1`` 时保留 ``build_graph`` 灌入的物理层。
    需要连物理层一起清空时传 ``include_physical=True``（或自定义 ``labels``）。
    """
    targets = list(labels or _TOPOLOGY_SEMANTIC_LABELS)
    if not include_physical:
        targets = [lb for lb in targets if lb not in _PHYSICAL_GRAPH_LABELS]
    with neo4j_session(driver) as s:
        for label in targets:
            s.run(f"MATCH (n:{label}) DETACH DELETE n")
    log.info("dropped topology nodes for %d labels", len(targets))


__all__ = [
    "detect_vector_dim_mismatch",
    "drop_old_experience_indexes",
    "drop_topology",
    "init_all",
    "init_btree_indexes",
    "init_constraints",
    "init_fulltext_indexes",
    "init_vector_indexes",
    "init_zone_index",
]

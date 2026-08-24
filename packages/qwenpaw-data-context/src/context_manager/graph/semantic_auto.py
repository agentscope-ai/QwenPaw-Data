"""schema_auto semantic provider（通用化扩展）。

纯 DDL / schema-driven 语义派生：
- 零 LLM、零外部网络请求、完全离线
- 产出节点统一打 ``source='schema_auto'``, ``source_trust=0.4``, ``status='auto'``
- 优先级低于 ``metrics_dict`` provider（0.9）；ON MATCH 时只升级低 trust 节点

派生规则
--------

1. **Domain**：
   - 按 ``profile.domain_namer(table_name)`` 聚合表 → Domain

2. **Dimension**（候选列）：
   - 列类型为 text/varchar/char/enum 且列名不像 id/key/hash
   - 列名匹配时间词（date/day/month/year/week/dt/ds）→ 时间维度，``type='time'``
   - 其余 → ``dimension_type='OLAP维度'``，cardinality hint 从 ``Column.sample_values`` 估计（字段可选）

3. **Metric proxy**（候选列）：
   - 列类型为数值型（int/float/bigint/numeric/double/decimal/real）
   - 列名匹配 ``_cnt$|_count$|_num$|_total$|_sum$|_amt$|_amount$|usercnt|pv|uv|revenue``
     → ``type='quantity'``，``formula='SUM(<col>)'``
   - 列名匹配 ``_rate$|_ratio$|_pct$|_percent$|_share$``
     → ``type='ratio'``（不自动拼分子分母，formula 留空）
   - 列名匹配 ``_avg$|_mean$|average``
     → ``type='ratio'``，``formula='AVG(<col>)'``

4. **Formula + Bridge**：
   - quantity/avg metric 且（表名命中 ``layer_prefixes`` **或** ``profile.schema_auto_bridge_metrics``）
     时写 Formula 桥
   - ``Metric-[:HAS_FORMULA]->Formula-[:OF_VIEW]->Dataset-[:CONTAINS_TABLE]->Table``、``-[:USES_COLUMN]->Column``
   - 只在 Table / Column 节点已存在时才建桥（OPTIONAL MATCH 保护）

5. **ANALYZED_BY 边**（新）：
   - Stage A（``schema_auto_analyzed_by=True``，默认开启）：
     同表派生的 Metric / Dimension 对建 ``ANALYZED_BY``，confidence=0.8。
   - Stage B（``schema_auto_analyzed_by_cross_table=True``，默认开启）：
     通过已有 ``JOINS_ON`` 边可达的跨表 Metric/Dimension 对建 ``ANALYZED_BY``，confidence=0.5。
   - Stage C（``schema_auto_analyzed_by_same_domain_fallback=False``，默认关闭）：
     同 Domain 内尚未关联的 Metric/Dimension 对建低置信 ``ANALYZED_BY``，confidence=0.3。
   - 均使用 ``MERGE``，幂等。不覆盖已有 ``metrics_dict``（trust 0.9）写下的 ``ANALYZED_BY``。

6. **DimensionValue 节点**（``schema_auto_dimension_values=False``，默认关闭）：
   - 从 ``Column.sample_values`` 反填 ``DimensionValue`` 节点及 ``HAS_VALUE`` 边。

7. **冲突保护**：
   - ON MATCH 时检查 ``source_trust``：已有节点 trust ≥ 0.4 + 1e-9 时不覆盖 description/formula
   - 让 ``metrics_dict`` 写下的节点永远赢
"""
from __future__ import annotations

import re
from typing import Optional

from neo4j import Driver

from ..utils import get_logger, neo4j_session
from .keys import (
    DEFAULT_DB_ID,
    DEFAULT_SCHEMA,
    column_key,
    dataset_key,
    dim_key,
    domain_key,
    formula_key,
    metric_key,
    table_key,
)
from .profile import DatasetProfile

log = get_logger("graph.semantic_auto")

SOURCE = "schema_auto"
SOURCE_TRUST = 0.4

# ---------------------------------------------------------------------- #
# 列名 → metric/dimension 匹配正则
# ---------------------------------------------------------------------- #
_METRIC_QUANTITY_RE = re.compile(
    r"(_cnt|_count|_num|_total|_sum|_amt|_amount|cnt_\d|count_\d|"
    r"usercnt|answercnt|pvcount|uvcount|_pv$|_uv$|revenue|sales|profit|cost|"
    r"cnt$|count$|num$|total$|amount$)$",
    re.IGNORECASE,
)
# 宽匹配：列名中任意位置含这些词缀（数量类型的 1d/di 粒度列）
_METRIC_QUANTITY_WIDE_RE = re.compile(
    r"(usercnt|answercnt|_cnt_|visitcnt|newcnt|activecnt|pvcount|uvcount)",
    re.IGNORECASE,
)
_METRIC_RATIO_RE = re.compile(
    r"(_rate|_ratio|_pct|_percent|_share|_proportion)$",
    re.IGNORECASE,
)
_METRIC_AVG_RE = re.compile(
    r"(_avg|_average|_mean)$",
    re.IGNORECASE,
)
_DIM_TIME_RE = re.compile(
    r"(^ds$|^dt$|_date$|_day$|_month$|_year$|_week$|^date$|^day$|^month$|^year$|^week$|^hour$|stat_date|data_date)",
    re.IGNORECASE,
)
_DIM_ID_EXCLUDE_RE = re.compile(
    r"(_id$|_key$|_hash$|_uuid$|_token$|_code$|rowid|^id$)",
    re.IGNORECASE,
)
_NUMERIC_TYPES = frozenset({
    "int", "integer", "bigint", "smallint", "tinyint",
    "float", "double", "real", "numeric", "decimal",
    "float4", "float8", "int2", "int4", "int8",
    "number",  # Oracle / Snowflake
    "bignumeric",  # BigQuery
})
_TEXT_TYPES = frozenset({
    "text", "varchar", "char", "character varying", "string",
    "nvarchar", "clob", "mediumtext", "longtext",
    "bpchar",  # PG internal
})


def _is_numeric(col_type: str) -> bool:
    t = (col_type or "").lower().split("(")[0].strip()
    return t in _NUMERIC_TYPES


def _is_text(col_type: str) -> bool:
    t = (col_type or "").lower().split("(")[0].strip()
    return t in _TEXT_TYPES or t.startswith("varchar") or t.startswith("nvarchar")


def _list_database_names(driver: Driver) -> list[str]:
    """图中每个 SQLite/DuckDB 逻辑库对应一个 ``Database`` 节点。"""
    from .physical import list_database_names

    return list_database_names(driver)


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #
def ingest_semantic_auto(
    driver: Driver,
    *,
    db_id: str = DEFAULT_DB_ID,
    schema: str = DEFAULT_SCHEMA,
    profile: Optional[DatasetProfile] = None,
) -> None:
    """从 Neo4j 现有物理层节点派生语义层（纯规则，零 LLM）。

    需在 profile 中开启 ``semantic_auto_sqlite_style_tables`` 并用
    ``semantic_auto_all_databases`` 遍历多库。
    """
    from .profile import get_profile

    p = profile or get_profile("generic")

    if getattr(p, "semantic_auto_all_databases", False):
        db_ids = _list_database_names(driver)
        if not db_ids:
            log.warning("schema_auto: no :Database nodes — skipping")
            return
        log.info(
            "schema_auto: multi-database mode (%d logical DBs, profile=%s)",
            len(db_ids),
            p.name,
        )
        for bid in db_ids:
            _ingest_semantic_auto_one_db(driver, db_id=bid, schema=schema, profile=p)
        return

    _ingest_semantic_auto_one_db(driver, db_id=db_id, schema=schema, profile=p)


def _ingest_semantic_auto_one_db(
    driver: Driver,
    *,
    db_id: str,
    schema: str,
    profile: DatasetProfile,
) -> None:
    sqlite_style = getattr(profile, "semantic_auto_sqlite_style_tables", False)
    k_schema = profile.semantic_auto_key_schema if sqlite_style else schema
    qualify_dom = getattr(profile, "semantic_auto_qualify_domain_with_db", False)

    log.info(
        "schema_auto: scanning db_id=%s (sqlite_style=%s, key_schema=%r)",
        db_id,
        sqlite_style,
        k_schema,
    )

    with neo4j_session(driver) as s:
        if sqlite_style:
            tables = s.run(
                """
                MATCH (t:Table {db: $db})
                RETURN t.name AS name,
                       coalesce(t.key, '') AS key,
                       coalesce(t.layer, '') AS layer
                ORDER BY t.name
                """,
                db=db_id,
            ).data()
            columns = s.run(
                """
                MATCH (t:Table {db: $db})-[:HAS_COLUMN]->(c:Column)
                RETURN t.name AS table_name, c.name AS col_name,
                       c.type AS col_type,
                       coalesce(c.description, c.comment, '') AS comment,
                       coalesce(c.key, '') AS col_key
                ORDER BY t.name, c.name
                """,
                db=db_id,
            ).data()
        else:
            tables = s.run(
                """
                MATCH (t:Table {db: $db, schema: $schema})
                RETURN t.name AS name,
                       coalesce(t.key, '') AS key,
                       coalesce(t.layer, '') AS layer
                ORDER BY t.name
                """,
                db=db_id,
                schema=schema,
            ).data()
            columns = s.run(
                """
                MATCH (t:Table {db: $db, schema: $schema})-[:HAS_COLUMN]->(c:Column)
                RETURN t.name AS table_name, c.name AS col_name,
                       c.type AS col_type,
                       coalesce(c.description, c.comment, '') AS comment,
                       coalesce(c.key, '') AS col_key
                ORDER BY t.name, c.name
                """,
                db=db_id,
                schema=schema,
            ).data()

    if not tables:
        log.warning("schema_auto: no tables for db_id=%s — skipping this db", db_id)
        return

    log.info("schema_auto: db_id=%s → %d tables / %d columns", db_id, len(tables), len(columns))

    table_to_domain: dict[str, str] = {}
    for t in tables:
        tnm = t["name"]
        base = profile.domain_namer(tnm)
        dom_name = f"{db_id}__{base}" if qualify_dom else base
        table_to_domain[tnm] = dom_name

    col_by_table: dict[str, list[dict]] = {}
    for c in columns:
        col_by_table.setdefault(c["table_name"], []).append(c)

    domain_rows: list[dict] = []
    dimension_rows: list[dict] = []
    metric_rows: list[dict] = []
    formula_rows: list[dict] = []

    # per-table tracking for ANALYZED_BY inference
    table_to_metric_keys: dict[str, list[str]] = {}
    table_to_dim_keys: dict[str, list[str]] = {}

    dom_keys_seen: set[str] = set()

    for t in tables:
        t_name = t["name"]
        raw_key = (t.get("key") or "").strip()
        t_key = raw_key or table_key(db_id, k_schema, t_name)
        dom_name = table_to_domain[t_name]
        dom_k = domain_key(dom_name)

        if dom_k not in dom_keys_seen:
            dom_keys_seen.add(dom_k)
            domain_rows.append({
                "key": dom_k,
                "name": dom_name,
                "description": f"Auto-derived domain for db_id={db_id}",
                "source": SOURCE,
                "source_trust": SOURCE_TRUST,
            })

        cols = col_by_table.get(t_name, [])
        is_agg_layer = any(
            t_name.lower().startswith(pfx + "_") or t_name.lower() == pfx
            for pfx in profile.layer_prefixes
        ) if profile.layer_prefixes else False

        for c in cols:
            col_name = c["col_name"]
            col_type = c["col_type"] or ""
            raw_ck = (c.get("col_key") or "").strip()
            col_k = raw_ck or column_key(db_id, k_schema, t_name, col_name)
            comment = c["comment"] or ""

            if _is_text(col_type) and not _DIM_ID_EXCLUDE_RE.search(col_name):
                is_time = bool(_DIM_TIME_RE.search(col_name))
                d_key = dim_key(dom_name, col_name)
                dimension_rows.append({
                    "key": d_key,
                    "name": col_name,
                    "display_name": col_name.replace("_", " ").title(),
                    "domain": dom_name,
                    "domain_key": dom_k,
                    "dimension_type": "时间维度" if is_time else "OLAP维度",
                    "data_type": "text",
                    "description": comment,
                    "col_key": col_k,
                    "source": SOURCE,
                    "source_trust": SOURCE_TRUST,
                })
                table_to_dim_keys.setdefault(t_name, []).append(d_key)
                continue

            if _is_numeric(col_type):
                m_type = None
                formula_expr = ""
                if _METRIC_QUANTITY_RE.search(col_name) or _METRIC_QUANTITY_WIDE_RE.search(col_name):
                    m_type = "quantity"
                    formula_expr = f"SUM({col_name})"
                elif _METRIC_AVG_RE.search(col_name):
                    m_type = "ratio"
                    formula_expr = f"AVG({col_name})"
                elif _METRIC_RATIO_RE.search(col_name):
                    m_type = "ratio"
                    formula_expr = ""

                if m_type is None:
                    continue

                m_key = metric_key(dom_name, col_name)
                metric_rows.append({
                    "key": m_key,
                    "name": col_name,
                    "display_name": col_name.replace("_", " ").title(),
                    "domain": dom_name,
                    "domain_key": dom_k,
                    "type": m_type,
                    "description": comment,
                    "source": SOURCE,
                    "source_trust": SOURCE_TRUST,
                })
                table_to_metric_keys.setdefault(t_name, []).append(m_key)

                dataset_label = f"{db_id}.{t_name}" if sqlite_style else f"{schema}.{t_name}"

                bridge_formula = bool(formula_expr) and (
                    is_agg_layer or getattr(profile, "schema_auto_bridge_metrics", False)
                )
                if bridge_formula:
                    short = t_name
                    f_key = formula_key(dom_name, col_name, short)
                    formula_rows.append({
                        "key": f_key,
                        "metric_key": m_key,
                        "domain": dom_name,
                        "dataset": dataset_label,
                        "ds_key": dataset_key(dom_name, dataset_label),
                        "formula": formula_expr,
                        "table_key": t_key,
                        "col_key": col_k,
                        "source": SOURCE,
                        "source_trust": SOURCE_TRUST,
                    })

    # ---------------------------------------------------------------------- #
    # Stage A: same-table ANALYZED_BY pairs (in-memory, confidence=0.8)
    # ---------------------------------------------------------------------- #
    do_analyzed_by = getattr(profile, "schema_auto_analyzed_by", True)
    analyzed_by_rows: list[dict] = []
    if do_analyzed_by:
        for t_name in tables:
            tname = t_name["name"]
            m_keys = table_to_metric_keys.get(tname, [])
            d_keys = table_to_dim_keys.get(tname, [])
            for mk in m_keys:
                for dk in d_keys:
                    analyzed_by_rows.append({
                        "metric_key": mk,
                        "dim_key": dk,
                        "confidence": 0.8,
                        "source": SOURCE,
                    })

    dom_names_list = list(set(table_to_domain.values()))

    log.info(
        "schema_auto [%s]: domains=%d dims=%d metrics=%d formulas=%d analyzed_by_same_table=%d",
        db_id,
        len(domain_rows),
        len(dimension_rows),
        len(metric_rows),
        len(formula_rows),
        len(analyzed_by_rows),
    )

    with neo4j_session(driver) as s:
        if domain_rows:
            s.execute_write(_write_domains, rows=domain_rows)
        if dimension_rows:
            _batched_write(s, _write_dimensions, dimension_rows, batch=200)
        if metric_rows:
            _batched_write(s, _write_metrics, metric_rows, batch=200)
        if formula_rows:
            _batched_write(s, _write_formulas, formula_rows, batch=200)

        # ---- ANALYZED_BY edges ----
        if do_analyzed_by:
            if analyzed_by_rows:
                _batched_write(s, _write_analyzed_by_edges, analyzed_by_rows, batch=500)

            # Stage B: cross-table via JOINS_ON
            if getattr(profile, "schema_auto_analyzed_by_cross_table", True) and dom_names_list:
                n = s.execute_write(
                    _write_analyzed_by_cross_table,
                    domain_names=dom_names_list,
                    source=SOURCE,
                )
                log.info("schema_auto [%s]: analyzed_by_cross_table=%s", db_id, n)

            # Stage C: same-domain fallback (optional, default off)
            if getattr(profile, "schema_auto_analyzed_by_same_domain_fallback", False) and dom_names_list:
                n = s.execute_write(
                    _write_analyzed_by_same_domain_fallback,
                    domain_names=dom_names_list,
                    source=SOURCE,
                )
                log.info("schema_auto [%s]: analyzed_by_same_domain_fallback=%s", db_id, n)

        # ---- DimensionValue nodes from sample_values ----
        if getattr(profile, "schema_auto_dimension_values", False) and dom_names_list:
            n = s.execute_write(
                _write_dimension_values_from_columns,
                domain_names=dom_names_list,
                source=SOURCE,
            )
            log.info("schema_auto [%s]: dimension_values=%s", db_id, n)


def _batched_write(session, fn, rows: list[dict], batch: int = 200) -> None:
    for i in range(0, len(rows), batch):
        session.execute_write(fn, rows=rows[i: i + batch])


# ---------------------------------------------------------------------- #
# Cypher 写入函数（均保护高 trust 节点）
# ---------------------------------------------------------------------- #
def _write_domains(tx, *, rows: list[dict]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (d:Domain {key: r.key})
          ON CREATE SET d.name = r.name, d.description = r.description,
                        d.status = 'auto', d.source = r.source,
                        d.source_trust = r.source_trust, d.zone = 'metadata'
          ON MATCH  SET d.name = CASE WHEN coalesce(d.source_trust, 0) < r.source_trust
                                        THEN r.name ELSE d.name END,
                        d.source = CASE WHEN coalesce(d.source_trust, 0) < r.source_trust
                                          THEN r.source ELSE d.source END
        """,
        rows=rows,
    )


def _write_dimensions(tx, *, rows: list[dict]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (d:Dimension {key: r.key})
          ON CREATE SET d.name = r.name, d.domain = r.domain,
                        d.dimension_type = r.dimension_type, d.data_type = r.data_type,
                        d.description = r.description,
                        d.status = 'auto', d.source = r.source,
                        d.source_trust = r.source_trust, d.zone = 'metadata'
          ON MATCH  SET d.dimension_type = CASE WHEN coalesce(d.source_trust, 0) < r.source_trust
                                              THEN r.dimension_type ELSE d.dimension_type END,
                        d.data_type = CASE WHEN coalesce(d.source_trust, 0) < r.source_trust
                                         THEN r.data_type ELSE d.data_type END
        WITH d, r
        MATCH (dom:Domain {key: r.domain_key})
        MERGE (dom)-[:HAS_DIMENSION]->(d)
        WITH d, r
        OPTIONAL MATCH (col:Column {key: r.col_key})
        FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [col] END |
            MERGE (d)-[:MAPS_TO_COLUMN]->(col)
        )
        """,
        rows=rows,
    )


def _write_metrics(tx, *, rows: list[dict]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MERGE (m:Metric {key: r.key})
          ON CREATE SET m.name = r.name, m.domain = r.domain,
                        m.type = r.type, m.description = r.description,
                        m.status = 'auto', m.source = r.source,
                        m.source_trust = r.source_trust, m.zone = 'metadata',
                        m.valid_to = null
          ON MATCH  SET m.type = CASE WHEN coalesce(m.source_trust, 0) < r.source_trust
                                        THEN r.type ELSE m.type END,
                        m.source = CASE WHEN coalesce(m.source_trust, 0) < r.source_trust
                                          THEN r.source ELSE m.source END
        WITH m, r
        MATCH (dom:Domain {key: r.domain_key})
        MERGE (dom)-[:HAS_METRIC]->(m)
        """,
        rows=rows,
    )


def _write_formulas(tx, *, rows: list[dict]) -> None:
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (m:Metric {key: r.metric_key})
        MERGE (f:Formula {key: r.key})
          ON CREATE SET f.domain = r.domain, f.metric_key = m.key,
                        f.dataset = r.dataset, f.formula = r.formula,
                        f.status = 'auto', f.source = r.source,
                        f.source_trust = r.source_trust, f.zone = 'metadata',
                        f.valid_to = null
          ON MATCH  SET f.formula = CASE WHEN coalesce(f.source_trust, 0) < r.source_trust
                                           THEN r.formula ELSE f.formula END
        MERGE (m)-[:HAS_FORMULA]->(f)
        WITH f, r
        OPTIONAL MATCH (t:Table {key: r.table_key})
        FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [t] END |
            MERGE (ds:Dataset {key: r.ds_key})
              ON CREATE SET ds.name = r.dataset, ds.domain = r.domain, ds.zone = 'metadata'
            MERGE (f)-[:OF_VIEW]->(ds)
            MERGE (ds)-[:CONTAINS_TABLE]->(t)
        )
        WITH f, r
        OPTIONAL MATCH (col:Column {key: r.col_key})
        FOREACH (_ IN CASE WHEN col IS NULL THEN [] ELSE [col] END |
            MERGE (f)-[rc:USES_COLUMN]->(col)
              ON CREATE SET rc.role = 'numerator'
        )
        """,
        rows=rows,
    )


# ---------------------------------------------------------------------- #
# ANALYZED_BY write functions
# ---------------------------------------------------------------------- #

def _write_analyzed_by_edges(tx, *, rows: list[dict]) -> None:
    """Stage A: same-table Metric→Dimension ANALYZED_BY (confidence=0.8).

    Uses MERGE so repeated runs are idempotent.  ON CREATE only sets properties;
    an existing higher-quality ANALYZED_BY from ``metrics_dict`` is left intact.
    """
    tx.run(
        """
        UNWIND $rows AS r
        MATCH (m:Metric {key: r.metric_key})
        MATCH (d:Dimension {key: r.dim_key})
        MERGE (m)-[rel:ANALYZED_BY]->(d)
          ON CREATE SET rel.confidence = r.confidence, rel.source = r.source
        """,
        rows=rows,
    )


def _write_analyzed_by_cross_table(tx, *, domain_names: list[str], source: str) -> int:
    """Stage B: cross-table ANALYZED_BY via existing JOINS_ON edges (confidence=0.5).

    Scoped to the domains derived in this run to avoid full-graph scans.
    """
    result = tx.run(
        """
        MATCH (m:Metric)-[:HAS_FORMULA]->(f:Formula)-[:OF_VIEW]->(:Dataset)-[:CONTAINS_TABLE]->(t1:Table)
        WHERE m.domain IN $domain_names AND m.valid_to IS NULL
        MATCH (t1)-[:HAS_COLUMN]->(c1:Column)-[:JOINS_ON]-(c2:Column)<-[:HAS_COLUMN]-(t2:Table)
        MATCH (t2)-[:HAS_COLUMN]->(dc:Column)<-[:MAPS_TO_COLUMN]-(d:Dimension)
        WHERE m.domain = d.domain
        MERGE (m)-[rel:ANALYZED_BY]->(d)
          ON CREATE SET rel.confidence = 0.5, rel.source = $source
        RETURN count(*) AS n
        """,
        domain_names=domain_names,
        source=source,
    )
    record = result.single()
    return record["n"] if record else 0


def _write_analyzed_by_same_domain_fallback(tx, *, domain_names: list[str], source: str) -> int:
    """Stage C: same-domain fallback — connect all Metric/Dimension pairs in the
    same Domain that are not yet linked (confidence=0.3, low-trust).

    Disabled by default (``schema_auto_analyzed_by_same_domain_fallback=False``).
    Useful for datasets with no numeric columns on the same table as text columns.
    """
    result = tx.run(
        """
        MATCH (m:Metric)
        WHERE m.domain IN $domain_names AND m.valid_to IS NULL
        MATCH (d:Dimension {domain: m.domain})
        WHERE NOT (m)-[:ANALYZED_BY]->(d)
        MERGE (m)-[rel:ANALYZED_BY]->(d)
          ON CREATE SET rel.confidence = 0.3, rel.source = $source
        RETURN count(*) AS n
        """,
        domain_names=domain_names,
        source=source,
    )
    record = result.single()
    return record["n"] if record else 0


# ---------------------------------------------------------------------- #
# DimensionValue from Column.sample_values
# ---------------------------------------------------------------------- #

def _write_dimension_values_from_columns(tx, *, domain_names: list[str], source: str) -> int:
    """Back-fill DimensionValue nodes from Column.sample_values.

    Only creates values for Dimensions that already have a MAPS_TO_COLUMN edge
    and whose target Column has non-empty sample_values.  Skips values longer
    than 200 characters to avoid bloating the graph with raw text blobs.
    """
    result = tx.run(
        """
        MATCH (d:Dimension)-[:MAPS_TO_COLUMN]->(c:Column)
        WHERE d.domain IN $domain_names
          AND c.sample_values IS NOT NULL
          AND size(c.sample_values) > 0
        UNWIND c.sample_values AS raw_val
        WITH d, toString(raw_val) AS val
        WHERE size(val) <= 200 AND val <> ''
        MERGE (dv:DimensionValue {key: 'dimv:' + d.domain + ':' + d.name + ':' + val})
          ON CREATE SET dv.value      = val,
                        dv.domain     = d.domain,
                        dv.zone       = 'metadata',
                        dv.source     = $source,
                        dv.status     = 'auto'
        MERGE (d)-[:HAS_VALUE]->(dv)
        RETURN count(*) AS n
        """,
        domain_names=domain_names,
        source=source,
    )
    record = result.single()
    return record["n"] if record else 0


__all__ = ["ingest_semantic_auto"]

"""物理层 ingester（物理 / Metadata 层）。

把 PostgreSQL 的元数据反射成 Neo4j 节点：

::

    Database --[:HAS_SCHEMA]--> Schema --[:HAS_TABLE]--> Table --[:HAS_COLUMN]--> Column

特点：

- 复用 :mod:`context_manager.ingest` 已有的 ``reflect_postgres`` / ``load_column_descriptions_postgres``，
  避免重复写 ``information_schema`` 查询。
- 节点 key 严格遵循 §2 规范（``db:`` / ``sch:`` / ``tbl:`` / ``col:``）。
- ``Table.layer`` 用 ``derive_layer`` 从表名推断；``Table.partition_key`` 默认探测 profile 指定的候选列。
- 给 Column 补 ``zone='metadata'``，与 §13 兼容；旧的 ``(db,table,name)`` 三元组属性也保留，
  方便存量代码（``retrieve.py``）继续匹配。
- v3.1：接受 ``DatasetProfile`` 注入，用 profile 的 ``layer_prefixes`` 和 ``partition_key_candidates``；
  不传 profile 时行为与旧版完全一致。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from neo4j import Driver

from ..ingest import ColumnInfo, FKInfo, reflect_postgres, load_column_descriptions_postgres
from ..utils import get_logger, neo4j_session
from .keys import (
    DEFAULT_SCHEMA,
    METADATA_ZONE,
    column_key,
    database_key,
    derive_layer,
    schema_key,
    table_key,
)
from .profile import DatasetProfile

log = get_logger("graph.physical")


# ---------------------------------------------------------------------- #
# 中间数据结构
# ---------------------------------------------------------------------- #
@dataclass
class ColumnRecord:
    """图节点 ``Column`` 的一行属性（已附 key / zone）。"""

    key: str
    db: str
    schema: str
    table: str
    name: str
    type: str
    pk: bool
    nullable: bool
    is_partition: bool
    comment: str
    description: str  # = comment + 任何额外 doc
    text: str  # 检索短文本，沿用既有 ingest.py 的规则
    sample_values: list = None  # ≤20 distinct values（id-like 列），用于 JOINS_ON Jaccard 推断
    aliases: list = None  # 业务别名（如 column_name_cn 中文列名），fulltext / vector 召回用
    zone: str = METADATA_ZONE

    def __post_init__(self):
        if self.sample_values is None:
            self.sample_values = []
        if self.aliases is None:
            self.aliases = []


@dataclass
class TableRecord:
    """图节点 ``Table`` 的一行属性。"""

    key: str
    db: str
    schema: str
    name: str
    layer: str
    partition_key: Optional[str]
    comment: str
    ddl: str
    zone: str = METADATA_ZONE


# ---------------------------------------------------------------------- #
# 反射：PG → 中间结构
# ---------------------------------------------------------------------- #
def _detect_partition_key(
    cols: Sequence[ColumnInfo],
    candidates: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """在候选列名里挑第一个分区键；找不到返回 ``None``。

    ``candidates`` 为 None 时使用内置默认（向后兼容）。
    """
    if candidates is None:
        candidates = ("ds", "dt", "stat_date", "data_date")
    by_name = {c.name.lower(): c.name for c in cols}
    for cand in candidates:
        if cand.lower() in by_name:
            return by_name[cand.lower()]
    return None


def _sample_pg_column_values(
    schema: str,
    table_col_pairs: list[tuple[str, str]],
    limit: int = 20,
    timeout_secs: int = 3,
    datasource_id: str = "",
) -> dict[tuple[str, str], list[str]]:
    """对 id-like 列批量抽样 ≤20 个 distinct 非空值，存入 ``Column.sample_values``。

    用于 JOINS_ON Jaccard 重叠评分。超时或失败时静默返回空列表。
    ``table_col_pairs`` 是 ``[(table, col), ...]``。
    """
    if not table_col_pairs:
        return {}

    out: dict[tuple[str, str], list[str]] = {}
    try:
        import psycopg
        from ..ingest import _pg_conn_kwargs

        conn_kw = _pg_conn_kwargs(schema=schema, datasource_id=datasource_id or None)
        with psycopg.connect(**conn_kw, connect_timeout=timeout_secs) as conn:
            conn.autocommit = True
            for tbl, col in table_col_pairs:
                try:
                    sql = (
                        f'SELECT DISTINCT "{col}" FROM "{schema}"."{tbl}" '
                        f'WHERE "{col}" IS NOT NULL LIMIT {int(limit)}'
                    )
                    cur = conn.cursor()
                    cur.execute(f"SET statement_timeout = '{timeout_secs * 1000}'")
                    cur.execute(sql)
                    rows = cur.fetchall()
                    out[(tbl, col)] = [str(r[0]) for r in rows if r[0] is not None]
                except Exception:
                    out[(tbl, col)] = []
    except Exception as exc:
        log.debug("sample_pg_column_values failed: %s (using empty samples)", exc)
    return out


def reflect_postgres_records(
    *,
    db_id: Optional[str] = None,
    schema: Optional[str] = None,
    profile: Optional[DatasetProfile] = None,
    sample_id_columns: bool = True,
    datasource_id: str = "",
) -> tuple[str, str, list[TableRecord], list[ColumnRecord], list[FKInfo]]:
    """从 PG 拉元数据并构造好 ``TableRecord`` / ``ColumnRecord``。

    返回 ``(db_id, schema, tables, columns, fks)``。``schema`` 默认 ``public``，
    ``db_id`` 未传时需能从 datasource 或 caller 解析，与 §2 例子里 ``app_db.public.*`` 对齐。

    ``profile`` 不为 None 时，使用 profile 的 ``layer_prefixes`` / ``partition_key_candidates``。

    ``sample_id_columns=True`` 时对 id-like 列额外抽样 ≤20 distinct values，
    写入 ``ColumnRecord.sample_values``，供 JOINS_ON Jaccard 推断使用。
    失败时静默跳过（不影响主流程）。
    """
    import re as _re

    sch = schema or DEFAULT_SCHEMA
    db = db_id
    if not db:
        raise RuntimeError("db_id is empty; cannot build physical layer keys — pass db_id explicitly")

    ds_id = (datasource_id or "").strip()
    if not ds_id:
        from .datasource_registry import db_id_to_datasource
        _ds = db_id_to_datasource(db)
        ds_id = _ds.datasource_id if _ds else ""

    partition_candidates = (
        list(profile.partition_key_candidates) if profile is not None else None
    )
    id_pattern_str = (profile.join_id_pattern if profile is not None
                      else r"^.+_(id|key|uuid|code|no|num)$")
    id_pattern = _re.compile(id_pattern_str, _re.IGNORECASE)

    log.info("Reflecting PostgreSQL metadata: db=%s schema=%s datasource=%s", db, sch, ds_id or "(active)")
    tables, ddl_map, fks = reflect_postgres(schema=sch, datasource_id=ds_id or None)
    col_desc = load_column_descriptions_postgres(schema=sch, datasource_id=ds_id or None)

    # 收集需要采样的 (table, col) 对
    id_like_pairs: list[tuple[str, str]] = []
    for t_name, cols in tables.items():
        for c in cols:
            if id_pattern.search(c.name):
                id_like_pairs.append((t_name, c.name))

    # 批量抽样（失败时返回空 dict）
    sample_map: dict[tuple[str, str], list[str]] = {}
    if sample_id_columns and id_like_pairs:
        log.debug("sampling %d id-like columns for Jaccard JOINS_ON …", len(id_like_pairs))
        sample_map = _sample_pg_column_values(sch, id_like_pairs, datasource_id=ds_id)
        sampled_cnt = sum(1 for v in sample_map.values() if v)
        log.debug("sampled %d / %d id-like columns", sampled_cnt, len(id_like_pairs))

    table_recs: list[TableRecord] = []
    col_recs: list[ColumnRecord] = []
    for t_name, cols in tables.items():
        pk_col = _detect_partition_key(cols, partition_candidates)
        table_recs.append(
            TableRecord(
                key=table_key(db, sch, t_name, ds_id),
                db=db,
                schema=sch,
                name=t_name,
                layer=derive_layer(t_name, profile),
                partition_key=pk_col,
                comment="",  # Hologres 的 COMMENT ON TABLE 多是 NULL，不强求
                ddl=ddl_map.get(t_name, "") or "",
            )
        )
        for c in cols:
            comment = (col_desc.get(t_name, {}) or {}).get(c.name, "") or ""
            text = f"{db}.{t_name}.{c.name} ({c.type})"
            if comment:
                text += f" — {comment}"
            col_recs.append(
                ColumnRecord(
                    key=column_key(db, sch, t_name, c.name, ds_id),
                    db=db,
                    schema=sch,
                    table=t_name,
                    name=c.name,
                    type=c.type,
                    pk=c.pk,
                    nullable=c.nullable,
                    is_partition=(c.name == pk_col),
                    comment=comment,
                    description=comment,
                    text=text,
                    sample_values=sample_map.get((t_name, c.name), []),
                )
            )
    log.info(
        "reflected: %d tables / %d columns / %d FKs",
        len(table_recs),
        len(col_recs),
        len(fks),
    )
    return db, sch, table_recs, col_recs, fks


# ---------------------------------------------------------------------- #
# 写入 Neo4j
# ---------------------------------------------------------------------- #
def write_physical(
    driver: Driver,
    *,
    db_id: str,
    schema: str,
    tables: Sequence[TableRecord],
    columns: Sequence[ColumnRecord],
    fks: Iterable[FKInfo],
    datasource_id: str = "",
) -> None:
    """把 ``TableRecord`` / ``ColumnRecord`` / ``FKInfo`` 全部 MERGE 到 Neo4j。

    使用单事务批写：
      1. ``Database`` + ``Schema`` 单点；
      2. ``UNWIND`` 批量建 ``Table``；
      3. ``UNWIND`` 批量建 ``Column`` + ``HAS_COLUMN``；
      4. FK → ``Column-[:REFERENCES]->Column`` + ``Table-[:JOINS]->Table``（与既有 ingest.py 兼容）。

    ``datasource_id`` 标识数据源归属；未传时从 registry 自动解析。
    """
    # 自动解析 datasource_id
    ds_id = (datasource_id or "").strip()
    if not ds_id:
        from .datasource_registry import db_id_to_datasource
        ds = db_id_to_datasource(db_id)
        ds_id = ds.datasource_id if ds else ""

    db_k = database_key(db_id, ds_id)
    sch_k = schema_key(db_id, schema, ds_id)
    fk_list = [fk.__dict__ for fk in fks]

    with neo4j_session(driver) as s:
        s.execute_write(
            _write_physical_tx,
            db_id=db_id,
            schema=schema,
            db_key=db_k,
            sch_key=sch_k,
            tables=[t.__dict__ for t in tables],
            columns=[c.__dict__ for c in columns],
            fks=fk_list,
            datasource_id=ds_id,
        )
    log.info(
        "physical layer written: %s / %s — %d tables, %d columns, %d FKs (datasource=%s)",
        db_k,
        sch_k,
        len(tables),
        len(columns),
        len(fk_list),
        ds_id,
    )


def _write_physical_tx(
    tx,
    *,
    db_id: str,
    schema: str,
    db_key: str,
    sch_key: str,
    tables: list[dict],
    columns: list[dict],
    fks: list[dict],
    datasource_id: str = "",
) -> None:
    tx.run(
        """
        MERGE (db:Database {key: $db_key})
          ON CREATE SET db.name = $db_id, db.zone = 'metadata', db.datasource_id = $datasource_id
          ON MATCH  SET db.name = $db_id, db.zone = 'metadata', db.datasource_id = $datasource_id
        MERGE (sch:Schema {key: $sch_key})
          ON CREATE SET sch.db = $db_id, sch.name = $schema, sch.zone = 'metadata', sch.datasource_id = $datasource_id
          ON MATCH  SET sch.db = $db_id, sch.name = $schema, sch.zone = 'metadata', sch.datasource_id = $datasource_id
        MERGE (db)-[:HAS_SCHEMA]->(sch)
        """,
        db_key=db_key,
        sch_key=sch_key,
        db_id=db_id,
        schema=schema,
        datasource_id=datasource_id,
    )

    tx.run(
        """
        UNWIND $tables AS row
        MERGE (t:Table {key: row.key})
          ON CREATE SET t.db = row.db, t.schema = row.schema, t.name = row.name,
                        t.layer = row.layer, t.partition_key = row.partition_key,
                        t.comment = row.comment, t.ddl = row.ddl, t.zone = row.zone
          ON MATCH  SET t.layer = row.layer, t.partition_key = row.partition_key,
                        t.comment = row.comment, t.ddl = row.ddl, t.zone = row.zone
        WITH t
        MATCH (sch:Schema {key: $sch_key})
        MERGE (sch)-[:HAS_TABLE]->(t)
        """,
        tables=tables,
        sch_key=sch_key,
    )

    tx.run(
        """
        UNWIND $columns AS row
        MERGE (c:Column {key: row.key})
          ON CREATE SET c.db = row.db, c.schema = row.schema, c.table = row.table,
                        c.name = row.name, c.type = row.type, c.pk = row.pk,
                        c.nullable = row.nullable, c.is_partition = row.is_partition,
                        c.comment = row.comment, c.description = row.description,
                        c.text = row.text, c.zone = row.zone,
                        c.sample_values = row.sample_values,
                        c.aliases = row.aliases
          ON MATCH  SET c.type = row.type, c.pk = row.pk, c.nullable = row.nullable,
                        c.is_partition = row.is_partition,
                        c.comment = row.comment, c.description = row.description,
                        c.text = row.text, c.zone = row.zone,
                        c.sample_values = CASE WHEN size(row.sample_values) > 0
                                               THEN row.sample_values
                                               ELSE coalesce(c.sample_values, []) END,
                        c.aliases = CASE WHEN size(row.aliases) > 0
                                         THEN row.aliases
                                         ELSE coalesce(c.aliases, []) END
        WITH c, row
        MATCH (t:Table {db: row.db, schema: row.schema, name: row.table})
        MERGE (t)-[:HAS_COLUMN]->(c)
        """,
        columns=columns,
    )

    if fks:
        tx.run(
            """
            UNWIND $fks AS fk
            MATCH (src:Column {db: $db, schema: $schema, table: fk.src_table, name: fk.src_col})
            MATCH (dst:Column {db: $db, schema: $schema, table: fk.dst_table, name: fk.dst_col})
            MERGE (src)-[r:REFERENCES]->(dst)
              ON CREATE SET r.confidence = 1.0, r.source = 'fk'
            """,
            fks=fks,
            db=db_id,
            schema=schema,
        )

        # 兼容旧 retrieve.py 期望的表级 :JOINS 关系
        tx.run(
            """
            UNWIND $fks AS fk
            MATCH (a:Table {db: $db, schema: $schema, name: fk.src_table})
            MATCH (b:Table {db: $db, schema: $schema, name: fk.dst_table})
            MERGE (a)-[j:JOINS {on_src: fk.src_col, on_dst: fk.dst_col, dst: fk.dst_table}]->(b)
            """,
            fks=fks,
            db=db_id,
            schema=schema,
        )


def list_database_names(driver: Driver) -> list[str]:
    """图中每个逻辑库对应一个 ``:Database`` 节点（多库场景）。"""
    from ..utils import neo4j_session

    with neo4j_session(driver) as s:
        rows = s.run("MATCH (d:Database) RETURN d.name AS name ORDER BY name").data()
    return [str(r["name"]) for r in rows]


def migrate_legacy_keys(driver: Driver, *, db_id: str, schema: str = DEFAULT_SCHEMA, datasource_id: str = "") -> None:
    """给「旧版 ingest 写下的 Database/Table/Column」补 ``key`` + ``schema`` + ``zone`` 字段。

    旧版 :mod:`context_manager.ingest` 只用 ``(db, name)`` / ``(db, table, name)`` 标识节点，没有 ``key``。
    本函数：
      - ``MATCH (d:Database {name: $db_id}) WHERE d.key IS NULL`` → ``SET d.key = 'db:' + name``;
      - 同理给 Table / Column 补 ``key`` / ``schema`` / ``zone``。
    这样后续按 ``{key: ...}`` MERGE 时会**命中**已有节点，不会重复建。
    """
    ds = (datasource_id or "").strip()
    db_k = database_key(db_id, ds)
    sch_k = schema_key(db_id, schema, ds)
    tbl_prefix = f"tbl:{ds}:" if ds else "tbl:"
    col_prefix = f"col:{ds}:" if ds else "col:"
    with neo4j_session(driver) as s:
        s.run(
            """
            MATCH (d:Database {name: $db_id})
            SET d.key = coalesce(d.key, $db_key),
                d.zone = coalesce(d.zone, 'metadata')
            """,
            db_id=db_id,
            db_key=db_k,
        )
        s.run(
            """
            MATCH (t:Table {db: $db_id})
            WHERE t.key IS NULL
            SET t.schema = coalesce(t.schema, $schema),
                t.key    = $tbl_prefix + $db_id + '.' + coalesce(t.schema, $schema) + '.' + t.name,
                t.zone   = coalesce(t.zone, 'metadata')
            """,
            db_id=db_id,
            schema=schema,
            tbl_prefix=tbl_prefix,
        )
        s.run(
            """
            MATCH (c:Column {db: $db_id})
            WHERE c.key IS NULL
            SET c.schema = coalesce(c.schema, $schema),
                c.key    = $col_prefix + $db_id + '.' + coalesce(c.schema, $schema) + '.' + c.table + '.' + c.name,
                c.zone   = coalesce(c.zone, 'metadata')
            """,
            db_id=db_id,
            schema=schema,
            col_prefix=col_prefix,
        )
        # 把存量 Database 直接挂到 Schema(public) 下（如还没建）
        s.run(
            """
            MATCH (d:Database {name: $db_id})
            MERGE (sch:Schema {key: $sch_key})
              ON CREATE SET sch.db = $db_id, sch.name = $schema, sch.zone = 'metadata'
            MERGE (d)-[:HAS_SCHEMA]->(sch)
            WITH sch
            MATCH (t:Table {db: $db_id})
            MERGE (sch)-[:HAS_TABLE]->(t)
            """,
            db_id=db_id,
            schema=schema,
            sch_key=sch_k,
        )
    log.info("migrated legacy nodes for db_id=%s schema=%s", db_id, schema)


def ingest_physical(
    driver: Driver,
    *,
    db_id: Optional[str] = None,
    schema: Optional[str] = None,
    profile: Optional[DatasetProfile] = None,
    datasource_id: str = "",
) -> tuple[str, str]:
    """一站式：先 migrate legacy → 反射 PG → 写入 Neo4j。返回 ``(db_id, schema)``。"""
    db, sch, tables, cols, fks = reflect_postgres_records(
        db_id=db_id, schema=schema, profile=profile, datasource_id=datasource_id,
    )
    migrate_legacy_keys(driver, db_id=db, schema=sch, datasource_id=datasource_id)
    write_physical(driver, db_id=db, schema=sch, tables=tables, columns=cols, fks=fks,
                   datasource_id=datasource_id)
    return db, sch


def ingest_from_manifest(
    driver: Driver,
    *,
    manifest,
    profile: Optional[DatasetProfile] = None,
) -> tuple[str, str]:
    """从 Adapter 产出的 ``PhysicalManifest`` 写入 Neo4j。

    跳过 PG 反射步骤，直接使用 manifest 中的 ``tables`` / ``columns`` / ``fks``。
    返回 ``(db_id, schema)``。
    """
    db_id = manifest.db_id
    sch = manifest.schema
    migrate_legacy_keys(driver, db_id=db_id, schema=sch)
    write_physical(
        driver,
        db_id=db_id,
        schema=sch,
        tables=manifest.tables,
        columns=manifest.columns,
        fks=manifest.fks,
    )
    log.info(
        "ingest from manifest: %s / %s — %d tables, %d columns, %d FKs",
        db_id, sch, len(manifest.tables), len(manifest.columns), len(manifest.fks),
    )
    return db_id, sch


__all__ = [
    "ColumnRecord",
    "DatasetProfile",
    "TableRecord",
    "ingest_from_manifest",
    "ingest_physical",
    "list_database_names",
    "migrate_legacy_keys",
    "reflect_postgres_records",
    "write_physical",
]

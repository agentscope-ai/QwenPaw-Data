"""将 SQLite 或 PostgreSQL 元数据反射为 Neo4j 图，并为列向量建原生 vector index。

图模型与约束见模块顶部英文注释块。
"""
from __future__ import annotations

import os as _os
import sqlite3
import time as _time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from neo4j import Driver, GraphDatabase
from tqdm import tqdm

from .config import CFG
from .utils import get_logger, neo4j_session

log = get_logger("ingest")

VECTOR_INDEX = "column_embedding_idx"  # Neo4j 中向量索引名称，retrieve 里 Cypher 会引用


# --------------------------------------------------------------------------- #
# SQLite 反射：表结构、DDL、外键
# --------------------------------------------------------------------------- #
@dataclass
class ColumnInfo:
    """一列：名称、类型、是否主键、是否可空。"""

    name: str
    type: str
    pk: bool
    nullable: bool


@dataclass
class FKInfo:
    """一条外键：源表列指向目标表列。"""

    src_table: str
    src_col: str
    dst_table: str
    dst_col: str


def reflect_sqlite(db_path: Path) -> Tuple[Dict[str, List[ColumnInfo]], Dict[str, str], List[FKInfo]]:
    """连接 SQLite，读 sqlite_master / PRAGMA，返回 tables、每表 DDL、外键列表。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row  # 按列名索引
    try:
        cur = conn.cursor()
        tables: Dict[str, List[ColumnInfo]] = {}
        ddl: Dict[str, str] = {}
        fks: List[FKInfo] = []

        cur.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"  # 排除系统表
        )
        for row in cur.fetchall():
            t_name = row["name"]
            ddl[t_name] = row["sql"] or ""  # 可能为 VIEW 等无 sql

            cols: List[ColumnInfo] = []
            # PRAGMA table_info: cid, name, type, notnull, dflt_value, pk
            for c in cur.execute(f'PRAGMA table_info("{t_name}")').fetchall():
                cols.append(
                    ColumnInfo(
                        name=c["name"],
                        type=(c["type"] or "").upper() or "TEXT",
                        pk=bool(c["pk"]),
                        nullable=not bool(c["notnull"]),
                    )
                )
            tables[t_name] = cols

            for fk in cur.execute(f'PRAGMA foreign_key_list("{t_name}")').fetchall():
                dst_table = fk["table"]
                dst_col = fk["to"]
                # SQLite：若 to 为空则默认指向目标表主键
                if not dst_col and dst_table in tables:
                    pk_cols = [c.name for c in tables[dst_table] if c.pk]
                    if pk_cols:
                        dst_col = pk_cols[0]
                if not dst_col:
                    continue
                fks.append(
                    FKInfo(
                        src_table=t_name,
                        src_col=fk["from"],
                        dst_table=dst_table,
                        dst_col=dst_col,
                    )
                )
        return tables, ddl, fks
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# PostgreSQL 反射（本地 app_db 等业务库）
# --------------------------------------------------------------------------- #
def _pg_conn_kwargs(
    schema: Optional[str] = None,
    datasource_id: Optional[str] = None,
) -> dict:
    """psycopg.connect 参数 —— 只从 semantic_config.db 读连接凭证。

    无 active datasource 或 config 缺失直接报错，不回退 CFG / 环境变量。
    """
    from .api.datasource_active_api import resolve_pg_connect_kwargs

    code = (datasource_id or "").strip()
    if not code:
        from .api.datasource_active_api import get_synced_default_datasource_id
        code = get_synced_default_datasource_id()
    if not code:
        raise RuntimeError(
            "未选择数据源：请先通过 PUT /api/datasources/active 切换数据源"
        )
    return resolve_pg_connect_kwargs(datasource_id=code, schema=schema)


def load_column_descriptions_postgres(
    schema: Optional[str] = None,
    datasource_id: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """从 ``pg_description`` 读列注释，返回 ``{表名: {列名: 描述}}``。"""
    import psycopg

    sch = schema or "public"
    with psycopg.connect(
        **_pg_conn_kwargs(schema=sch, datasource_id=datasource_id)
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.relname::text AS table_name,
                       a.attname::text AS column_name,
                       col_description(a.attrelid, a.attnum) AS descr
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND col_description(a.attrelid, a.attnum) IS NOT NULL
                """,
                (sch,),
            )
            out: Dict[str, Dict[str, str]] = {}
            for table_name, column_name, descr in cur.fetchall():
                if not descr:
                    continue
                out.setdefault(str(table_name), {})[str(column_name)] = str(descr).strip()
            return out


def reflect_postgres(
    schema: Optional[str] = None,
    only_tables: Optional[Iterable[str]] = None,
    datasource_id: Optional[str] = None,
) -> Tuple[Dict[str, List[ColumnInfo]], Dict[str, str], List[FKInfo]]:
    """连接 PostgreSQL，读 ``information_schema`` / ``pg_views``，返回 tables / ddl / fks。

    ``only_tables`` 传入白名单时只反射这些表(按表名精确匹配),其余跳过——
    避免全量反射整个 schema 产生大量孤立物理节点。

    连接凭证来自 semantic_config.db（``datasource_id`` 或 active datasource）。"""
    import psycopg

    sch = schema or "public"
    tables: Dict[str, List[ColumnInfo]] = {}
    ddl: Dict[str, str] = {}
    fks: List[FKInfo] = []

    only_set = set(only_tables) if only_tables else None

    with psycopg.connect(
        **_pg_conn_kwargs(schema=sch, datasource_id=datasource_id)
    ) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type IN ('BASE TABLE', 'VIEW', 'FOREIGN TABLE')
                ORDER BY table_name
                """,
                (sch,),
            )
            table_names = [r[0] for r in cur.fetchall()]
            if only_set is not None:
                table_names = [t for t in table_names if t in only_set]

            cur.execute(
                """
                SELECT table_name, column_name, data_type, is_nullable, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position
                """,
                (sch,),
            )
            cols_by_table: Dict[str, List[Tuple[str, str, str]]] = {}
            for t, col, dtype, is_null, _ord in cur.fetchall():
                cols_by_table.setdefault(t, []).append((col, dtype or "", is_null or "YES"))

            cur.execute(
                """
                SELECT tc.table_name, kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                  ON tc.constraint_catalog = kcu.constraint_catalog
                 AND tc.constraint_schema = kcu.constraint_schema
                 AND tc.constraint_name = kcu.constraint_name
                WHERE tc.table_schema = %s
                  AND tc.constraint_type = 'PRIMARY KEY'
                """,
                (sch,),
            )
            pk_set = {(r[0], r[1]) for r in cur.fetchall()}

            for t_name in table_names:
                cols: List[ColumnInfo] = []
                for col, dtype_raw, is_null in cols_by_table.get(t_name, []):
                    dt = (dtype_raw or "").upper() or "TEXT"
                    nullable = str(is_null).upper() == "YES"
                    pk = (t_name, col) in pk_set
                    cols.append(ColumnInfo(name=col, type=dt, pk=pk, nullable=nullable))
                tables[t_name] = cols

            cur.execute(
                "SELECT viewname, definition FROM pg_views WHERE schemaname = %s",
                (sch,),
            )
            for vname, definition in cur.fetchall():
                body = (definition or "").strip()
                ddl[vname] = (body.rstrip(";") + ";") if body else ""

            for t_name in table_names:
                if t_name in ddl:
                    continue
                ci_list = tables.get(t_name, [])
                if not ci_list:
                    ddl[t_name] = ""
                    continue
                lines = [f"CREATE TABLE {sch}.{t_name} ("]
                for i, c in enumerate(ci_list):
                    suf = "," if i < len(ci_list) - 1 else ""
                    nn = "" if c.nullable else " NOT NULL"
                    pk = " PRIMARY KEY" if c.pk else ""
                    q = '"' if any(ch in c.name for ch in ('"', " ")) else ""
                    col_sql = f'{q}{c.name}{q}'
                    lines.append(f"  {col_sql} {c.type}{nn}{pk}{suf}")
                lines.append(");")
                ddl[t_name] = "\n".join(lines)

            cur.execute(
                """
                SELECT
                    kcu1.table_name AS src_table,
                    kcu1.column_name AS src_col,
                    kcu2.table_name AS dst_table,
                    kcu2.column_name AS dst_col
                FROM information_schema.referential_constraints rc
                JOIN information_schema.key_column_usage kcu1
                  ON kcu1.constraint_catalog = rc.constraint_catalog
                 AND kcu1.constraint_schema = rc.constraint_schema
                 AND kcu1.constraint_name = rc.constraint_name
                JOIN information_schema.key_column_usage kcu2
                  ON kcu2.constraint_catalog = rc.unique_constraint_catalog
                 AND kcu2.constraint_schema = rc.unique_constraint_schema
                 AND kcu2.constraint_name = rc.unique_constraint_name
                 AND kcu2.ordinal_position = kcu1.ordinal_position
                WHERE kcu1.table_schema = %s
                """,
                (sch,),
            )
            for src_t, src_c, dst_t, dst_c in cur.fetchall():
                fks.append(
                    FKInfo(
                        src_table=str(src_t),
                        src_col=str(src_c),
                        dst_table=str(dst_t),
                        dst_col=str(dst_c),
                    )
                )

    return tables, ddl, fks


def load_column_descriptions_holo(
    schema: Optional[str] = None,
    datasource_id: Optional[str] = None,
) -> Dict[str, Dict[str, str]]:
    """Hologres 列注释 —— PG 协议直连，复用 ``load_column_descriptions_postgres``。

    远端不支持 ``col_description`` 等 pg catalog 函数时返回 ``{}``（不抛错）。
    """
    return load_column_descriptions_postgres(schema=schema, datasource_id=datasource_id)


def _physical_layer_exists(driver: Driver, db_id: str) -> bool:
    """检查 Neo4j 中是否已有该 db_id 的物理层节点。"""
    with neo4j_session(driver) as s:
        result = s.run(
            "MATCH (t:Table {db: $db_id}) RETURN count(t) > 0 AS exists",
            db_id=db_id,
        ).single()
        return result["exists"] if result else False


def reflect_holo(
    schema: Optional[str] = None,
    only_tables: Optional[Iterable[str]] = None,
    datasource_id: Optional[str] = None,
) -> Tuple[Dict[str, List[ColumnInfo]], Dict[str, str], List[FKInfo]]:
    """Hologres 反射 —— PG 协议直连，复用 ``reflect_postgres``。

    凭证来自 semantic_config.db（``datasource_id`` 或 active datasource）。
    """
    return reflect_postgres(
        schema=schema, only_tables=only_tables, datasource_id=datasource_id,
    )


def ingest_holo(
    db_id: Optional[str] = None,
    wipe: bool = False,
    schema: Optional[str] = None,
    skip_if_exists: bool = True,
    only_tables: Optional[Iterable[str]] = None,
    datasource_id: Optional[str] = None,
) -> None:
    """把 Hologres 元数据写入 Neo4j —— PG 协议直连。

    图上的 ``:Database {name}`` 使用 ``db_id``（默认用 active datasource 的 dbname）。
    """
    from .api.datasource_active_api import load_datasource_config, get_synced_default_datasource_id

    code = (datasource_id or "").strip() or get_synced_default_datasource_id()
    if not code:
        raise RuntimeError("未选择数据源：请先通过 PUT /api/datasources/active 切换数据源")
    cfg = load_datasource_config(code)
    if cfg is None:
        raise RuntimeError(f"datasource_id={code!r} 无 config")
    sch = schema or "public"
    ns = db_id or cfg.get("dbname") or cfg.get("database") or code

    writer = GraphWriter()
    try:
        if skip_if_exists and not wipe and _physical_layer_exists(writer.driver, ns):
            log.info("Physical layer for db_id=%r already exists, skipping (use --force to re-reflect)", ns)
            return

        tables, ddl, fks = reflect_holo(schema=sch, only_tables=only_tables, datasource_id=code)
        col_desc = load_column_descriptions_holo(schema=sch, datasource_id=code)

        if wipe:
            log.info("Wiping existing Neo4j graph...")
            writer.wipe()
        writer.init_schema()
        log.info("Ingesting Holo → Neo4j as db_id=%r (%d tables/views)", ns, len(tables))
        writer.ingest_database(ns, tables, ddl, fks, col_desc, schema=sch)
    finally:
        writer.close()
    log.info("Done (Holo → Neo4j).")


def ingest_postgres(
    db_id: Optional[str] = None,
    wipe: bool = False,
    schema: Optional[str] = None,
    skip_if_exists: bool = True,
    only_tables: Optional[Iterable[str]] = None,
    datasource_id: Optional[str] = None,
) -> None:
    """把单个 Postgres 库（一个 ``schema``）的元数据写入 Neo4j。

    图上的 ``:Database {name}`` 使用 ``db_id``（默认用 active datasource 的 dbname）。
    凭证来自 semantic_config.db（``datasource_id`` 或 active datasource）。
    """
    from .api.datasource_active_api import load_datasource_config, get_synced_default_datasource_id

    code = (datasource_id or "").strip() or get_synced_default_datasource_id()
    if not code:
        raise RuntimeError("未选择数据源：请先通过 PUT /api/datasources/active 切换数据源")
    cfg = load_datasource_config(code)
    if cfg is None:
        raise RuntimeError(f"datasource_id={code!r} 无 config")
    sch = schema or "public"
    ns = db_id or cfg.get("dbname") or cfg.get("database") or code

    writer = GraphWriter()
    try:
        if skip_if_exists and not wipe and _physical_layer_exists(writer.driver, ns):
            log.info("Physical layer for db_id=%r already exists, skipping (use --force to re-reflect)", ns)
            return

        tables, ddl, fks = reflect_postgres(schema=sch, only_tables=only_tables, datasource_id=code)
        col_desc = load_column_descriptions_postgres(schema=sch, datasource_id=code)

        if wipe:
            log.info("Wiping existing Neo4j graph...")
            writer.wipe()
        writer.init_schema()
        log.info("Ingesting PostgreSQL → Neo4j as db_id=%r (%d tables/views)", ns, len(tables))
        writer.ingest_database(ns, tables, ddl, fks, col_desc, schema=sch)
    finally:
        writer.close()
    log.info("Done (PostgreSQL → Neo4j).")


# --------------------------------------------------------------------------- #
# ODPS (MaxCompute) 反射
# --------------------------------------------------------------------------- #
def reflect_odps(
    project: Optional[str] = None,
    table_prefix: Optional[str] = None,
    only_tables: Optional[Iterable[str]] = None,
    *,
    datasource_id: Optional[str] = None,
) -> Tuple[Dict[str, List[ColumnInfo]], Dict[str, str], List[FKInfo]]:
    """连接 ODPS，读 project 的表元数据，返回 tables / ddl / fks。

    ``table_prefix`` 可限制只反射前缀匹配的表(ODPS project 可能上万张表)。
    ``only_tables`` 传入白名单时只反射这些表(按表名精确匹配),优先级高于 prefix。
    ``datasource_id`` 指定 semantic_config.db 里的数据源 id；为空则用当前 active。
    """
    from .graph.odps_akless import get_odps_client

    o = get_odps_client(datasource_id=datasource_id)
    # project 优先用参数；否则从 ODPS client 自带的项目取
    proj = project or getattr(o, "project", None) or ""
    if not proj:
        raise RuntimeError("reflect_odps: project 未指定且 ODPS client 无默认 project")
    only_set = set(only_tables) if only_tables else None

    tables: Dict[str, List[ColumnInfo]] = {}
    ddl: Dict[str, str] = {}

    log.info("Listing tables in ODPS project %s ...", proj)
    table_list = list(o.list_tables(project=proj))
    if only_set is not None:
        table_list = [t for t in table_list if t.name in only_set]
    elif table_prefix:
        table_list = [t for t in table_list if t.name.startswith(table_prefix)]
    log.info("Found %d tables (only_tables=%s, prefix=%r), reading schemas ...",
             len(table_list), bool(only_set), table_prefix if not only_set else "(ignored)")

    skipped = 0
    for t in tqdm(table_list, desc="ODPS reflect", unit="table"):
        try:
            t.reload()
        except Exception as exc:
            log.warning("ODPS: skip table %s (no permission or error: %s)", t.name, exc)
            skipped += 1
            continue
        cols: List[ColumnInfo] = []
        ddl_lines = [f"CREATE TABLE {t.name} ("]
        col_defs = []
        for c in t.table_schema.columns:
            cols.append(ColumnInfo(
                name=c.name,
                type=str(c.type).upper(),
                pk=False,  # ODPS 无传统 PK
                nullable=True,
            ))
            comment = f"  -- {c.comment}" if c.comment else ""
            col_defs.append(f"  {c.name} {str(c.type).upper()}{comment}")
        ddl_lines.append(",\n".join(col_defs))
        ddl_lines.append(");")
        tables[t.name] = cols
        ddl[t.name] = "\n".join(ddl_lines)

    if skipped:
        log.warning("ODPS: skipped %d table(s) due to permission errors", skipped)

    return tables, ddl, []  # ODPS 无 FK


def ingest_odps(
    db_id: Optional[str] = None,
    wipe: bool = False,
    project: Optional[str] = None,
    table_prefix: Optional[str] = None,
    schema: str = "public",
    skip_if_exists: bool = True,
    only_tables: Optional[Iterable[str]] = None,
    datasource_id: Optional[str] = None,
) -> None:
    """把 ODPS project 的元数据写入 Neo4j。

    图上的 ``:Database {name}`` 使用 ``db_id``（默认用 active datasource 的 project）。
    凭证来自 semantic_config.db（``datasource_id`` 或 active datasource）。
    """
    from .api.datasource_active_api import resolve_odps_connect_config, get_synced_default_datasource_id

    code = (datasource_id or "").strip() or get_synced_default_datasource_id()
    if not code:
        raise RuntimeError("未选择数据源：请先通过 PUT /api/datasources/active 切换数据源")
    odps_cfg = resolve_odps_connect_config(datasource_id=code)
    proj = project or odps_cfg["project"]
    ns = db_id or proj

    writer = GraphWriter()
    try:
        if skip_if_exists and not wipe and _physical_layer_exists(writer.driver, ns):
            log.info("Physical layer for db_id=%r already exists, skipping (use --force to re-reflect)", ns)
            return

        tables, ddl, fks = reflect_odps(
            project=proj, table_prefix=table_prefix, only_tables=only_tables,
            datasource_id=code,
        )

        # column descriptions 从 table.comment 提取，已在 reflect_odps 里拼到 DDL
        col_desc: Dict[str, Dict[str, str]] = {}

        if wipe:
            log.info("Wiping existing Neo4j graph...")
            writer.wipe()
        writer.init_schema()
        log.info(
            "Ingesting ODPS %s → Neo4j as db_id=%r (%d tables)",
            proj,
            ns,
            len(tables),
        )
        writer.ingest_database(ns, tables, ddl, fks, col_desc, schema=schema)
    finally:
        writer.close()
    log.info("Done (ODPS → Neo4j).")


# --------------------------------------------------------------------------- #
# Neo4j 写入与约束
# --------------------------------------------------------------------------- #
class GraphWriter:
    """封装 driver：初始化约束/索引、整库灌入、清空图。"""

    def __init__(self, driver: Optional[Driver] = None) -> None:
        self.driver = driver or GraphDatabase.driver(
            CFG.neo4j_uri, auth=(CFG.neo4j_user, CFG.neo4j_password)
        )

    def close(self) -> None:
        self.driver.close()

    def init_schema(self) -> None:
        """创建唯一约束 + Column.embedding 的 vector index（维度与 CFG.embed_dim 一致）。"""
        with neo4j_session(self.driver) as s:
            s.run("CREATE CONSTRAINT db_name IF NOT EXISTS FOR (d:Database) REQUIRE d.name IS UNIQUE")
            s.run(
                "CREATE CONSTRAINT table_uniq IF NOT EXISTS "
                "FOR (t:Table) REQUIRE (t.db, t.name) IS UNIQUE"
            )
            s.run(
                "CREATE CONSTRAINT column_uniq IF NOT EXISTS "
                "FOR (c:Column) REQUIRE (c.db, c.table, c.name) IS UNIQUE"
            )
            s.run(
                f"""
                CREATE VECTOR INDEX {VECTOR_INDEX} IF NOT EXISTS
                FOR (c:Column) ON (c.embedding)
                OPTIONS {{ indexConfig: {{
                    `vector.dimensions`: {CFG.embed_dim},
                    `vector.similarity_function`: 'cosine'
                }} }}
                """
            )

    def wipe(self) -> None:
        """删除图中所有节点与关系（慎用）。"""
        with neo4j_session(self.driver) as s:
            s.run("MATCH (n) DETACH DELETE n")

    TABLE_CHUNK_SIZE = int(_os.getenv("INGEST_TABLE_CHUNK", "2000"))
    COLUMN_CHUNK_SIZE = int(_os.getenv("INGEST_COLUMN_CHUNK", "2000"))
    FK_CHUNK_SIZE = int(_os.getenv("INGEST_FK_CHUNK", "2000"))

    def ingest_database(
        self,
        db_id: str,
        tables: Dict[str, List[ColumnInfo]],
        ddl: Dict[str, str],
        fks: Iterable[FKInfo],
        column_desc: Dict[str, Dict[str, str]],
        schema: str = "public",
    ) -> None:
        """单库：分批 embed + 分批写入 (Database / Table / Column / FK).

        Why: 整库单事务对超大库会撑爆 Neo4j heap.
        Pattern: build col_records first (no embeddings), then iterate chunks —
        each chunk embeds → writes Cypher tx → drops embeddings from RAM.
        """
        # 1. 构造 col_records 元数据 (不含 embedding) + table 行
        col_records: List[dict] = []
        for t_name, cols in tables.items():
            for c in cols:
                desc = column_desc.get(t_name, {}).get(c.name, "")
                text = f"{db_id}.{t_name}.{c.name} ({c.type})"  # 检索用短文本
                if desc:
                    text += f" — {desc}"
                col_records.append(
                    {
                        "db": db_id,
                        "table": t_name,
                        "name": c.name,
                        "type": c.type,
                        "pk": c.pk,
                        "nullable": c.nullable,
                        "description": desc,
                        "text": text,
                    }
                )

        table_rows = [{"name": n, "ddl": ddl.get(n, "")} for n in tables.keys()]
        fk_list = list(fks)
        n_tables = len(table_rows)
        n_cols = len(col_records)
        n_fks = len(fk_list)
        log.info(
            "ingest_database db=%s tables=%d cols=%d fks=%d (chunked: T=%d C=%d F=%d)",
            db_id, n_tables, n_cols, n_fks,
            self.TABLE_CHUNK_SIZE, self.COLUMN_CHUNK_SIZE, self.FK_CHUNK_SIZE,
        )

        with neo4j_session(self.driver) as s:
            # 2. Database root (1 tx) — resolve datasource_id from registry
            from .graph.datasource_registry import db_id_to_datasource as _ds_lookup
            _ds = _ds_lookup(db_id)
            _ds_id = _ds.datasource_id if _ds else ""
            s.execute_write(self._write_db_root, db_id, _ds_id, schema)

            # 3. Tables in chunks
            for i in range(0, n_tables, self.TABLE_CHUNK_SIZE):
                s.execute_write(
                    self._write_tables_chunk, db_id,
                    table_rows[i: i + self.TABLE_CHUNK_SIZE],
                    schema,
                )

            # 4. Columns: write metadata to Neo4j (embeddings via index_embeddings.py).
            t_neo4j_total = 0.0
            for i in range(0, n_cols, self.COLUMN_CHUNK_SIZE):
                chunk = col_records[i: i + self.COLUMN_CHUNK_SIZE]
                t0 = _time.perf_counter()
                s.execute_write(self._write_columns_chunk, chunk, schema)
                t_neo4j_total += _time.perf_counter() - t0
            if n_cols > 0:
                log.info(
                    "ingest_database db=%s cols=%d timing: neo4j=%.1fs (%.0f/s)",
                    db_id, n_cols, t_neo4j_total,
                    n_cols / t_neo4j_total if t_neo4j_total > 0 else 0,
                )

            # 5. FKs in chunks (often empty for large schemas)
            fk_dicts = [fk.__dict__ for fk in fk_list]
            for i in range(0, n_fks, self.FK_CHUNK_SIZE):
                s.execute_write(
                    self._write_fks_chunk, db_id,
                    fk_dicts[i: i + self.FK_CHUNK_SIZE],
                )

    @staticmethod
    def _write_db_root(tx, db_id, datasource_id="", schema="public"):
        tx.run(
            "MERGE (d:Database {name: $db}) "
            "SET d.datasource_id = $datasource_id, d.key = 'db:' + $db "
            "WITH d "
            "MERGE (s:Schema {key: 'sch:' + $db + '.' + $schema}) "
            "SET s.name = $schema "
            "WITH d, s "
            "MERGE (d)-[:HAS_SCHEMA]->(s)",
            db=db_id, datasource_id=datasource_id, schema=schema,
        )

    @staticmethod
    def _write_tables_chunk(tx, db_id, table_rows, schema="public"):
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (t:Table {db: $db, name: row.name})
            SET t.ddl = row.ddl, t.key = 'tbl:' + $db + '.' + $schema + '.' + row.name
            WITH t
            MATCH (s:Schema {key: 'sch:' + $db + '.' + $schema})
            MERGE (s)-[:HAS_TABLE]->(t)
            """,
            db=db_id,
            rows=table_rows,
            schema=schema,
        )

    @staticmethod
    def _write_columns_chunk(tx, col_records, schema="public"):
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (c:Column {db: row.db, table: row.table, name: row.name})
            SET c.type = row.type,
                c.pk = row.pk,
                c.nullable = row.nullable,
                c.description = row.description,
                c.text = row.text,
                c.key = 'col:' + row.db + '.' + $schema + '.' + row.table + '.' + row.name
            WITH c, row
            MATCH (t:Table {db: row.db, name: row.table})
            MERGE (t)-[:HAS_COLUMN]->(c)
            """,
            rows=col_records,
            schema=schema,
        )

    @staticmethod
    def _write_fks_chunk(tx, db_id, fk_dicts):
        tx.run(
            """
            UNWIND $fks AS fk
            MATCH (src:Column {db: $db, table: fk.src_table, name: fk.src_col})
            MATCH (dst:Column {db: $db, table: fk.dst_table, name: fk.dst_col})
            MERGE (src)-[:REFERENCES]->(dst)
            """,
            db=db_id,
            fks=fk_dicts,
        )
        tx.run(
            """
            UNWIND $fks AS fk
            MATCH (a:Table {db: $db, name: fk.src_table})
            MATCH (b:Table {db: $db, name: fk.dst_table})
            MERGE (a)-[j:JOINS {on_src: fk.src_col, on_dst: fk.dst_col, dst: fk.dst_table}]->(b)
            """,
            db=db_id,
            fks=fk_dicts,
        )

    # ------------------------------------------------------------------ #
    # Subfield ingest (BQ STRUCT/RECORD nested columns)
    # ------------------------------------------------------------------ #
    def ingest_subfields(
        self,
        db_id: str,
        subfields: Dict[str, list],
        column_desc: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> int:
        """Create Column nodes + HAS_SUBFIELD edges for nested STRUCT fields.

        ``subfields`` maps table_name → List[SubfieldInfo].
        Returns total subfield Column nodes written.
        """
        col_desc = column_desc or {}
        records: List[dict] = []
        for t_name, sfs in subfields.items():
            for sf in sfs:
                desc = sf.description
                if not desc:
                    desc = col_desc.get(t_name, {}).get(sf.full_path, "")
                text = f"{db_id}.{t_name}.{sf.full_path} ({sf.type})"
                if desc:
                    text += f" — {desc}"
                records.append({
                    "db": db_id,
                    "table": t_name,
                    "name": sf.full_path,
                    "parent": sf.parent_path,
                    "type": sf.type,
                    "description": desc,
                    "text": text,
                })

        if not records:
            return 0

        for i in range(0, len(records), self.COLUMN_CHUNK_SIZE):
            chunk = records[i: i + self.COLUMN_CHUNK_SIZE]
            with neo4j_session(self.driver) as s:
                s.run(
                    """
                    UNWIND $rows AS row
                    MERGE (c:Column {db: row.db, table: row.table, name: row.name})
                    SET c.type = row.type,
                        c.description = row.description,
                        c.text = row.text
                    WITH c, row
                    MATCH (p:Column {db: row.db, table: row.table, name: row.parent})
                    MERGE (p)-[:HAS_SUBFIELD]->(c)
                    """,
                    rows=chunk,
                ).consume()

        log.info("ingest_subfields db=%s subfields=%d", db_id, len(records))
        return len(records)
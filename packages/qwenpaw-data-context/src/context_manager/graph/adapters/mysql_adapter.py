"""MySQL Adapter — 通过 PyMySQL 反射 information_schema，自包含返回 PhysicalManifest。

与 PostgresAdapter 不同：MySQL 的 ``schema`` 概念等同于 ``database``，
因此 ``schemas[0]`` 优先作为目标 database；缺省时退回 ``connection.database``。
"""
from __future__ import annotations

import os
from typing import Optional, Sequence

from ...contracts.import_models import SourceConfig
from ...ingest import ColumnInfo, FKInfo
from ...secrets.schemas import MySQLConnection
from ...utils import get_logger
from ..keys import (
    column_key,
    derive_layer,
    table_key,
)
from ..physical import ColumnRecord, TableRecord
from ..profile import DatasetProfile
from .base import ConnectionTestResult, PhysicalManifest

log = get_logger("graph.adapters.mysql")


class MySQLAdapter:
    """通过 pymysql 连接 MySQL，抽取表/列/FK 元数据。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        db_id: str,
        profile: Optional[DatasetProfile] = None,
    ):
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password
        self._db_id = db_id
        self._profile = profile

    @classmethod
    def from_config(cls, config: SourceConfig, db_id: str) -> "MySQLAdapter":
        conn = config.connection
        assert isinstance(conn, MySQLConnection), (
            f"mysql adapter requires MySQLConnection, got {type(conn).__name__}"
        )
        password = conn.password.get_secret_value()

        port = conn.port or 3306
        return cls(
            host=conn.host,
            port=port,
            database=conn.database,
            user=conn.user,
            password=password,
            db_id=db_id,
        )

    def _connect(self, *, connect_timeout: int = 5):
        import pymysql

        return pymysql.connect(
            host=self._host,
            port=self._port,
            user=self._user,
            password=self._password,
            database=self._database,
            charset="utf8mb4",
            connect_timeout=connect_timeout,
        )

    def test_connection(self) -> ConnectionTestResult:
        try:
            with self._connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema = %s",
                        (self._database,),
                    )
                    n = cur.fetchone()[0]
            return ConnectionTestResult(
                success=True,
                message=f"connected to {self._host}:{self._port}/{self._database}",
                tables_found=int(n),
            )
        except Exception as exc:
            return ConnectionTestResult(success=False, message=str(exc))

    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        # MySQL: schema == database；schemas[0] 优先，否则用 connection.database
        target_db = (schemas[0] if schemas else self._database) or self._database
        if not target_db:
            raise RuntimeError("mysql adapter: database is empty (set source.connection.database or schemas[0])")

        sch = target_db  # 在图里作为 schema 名
        db = self._db_id

        partition_candidates = (
            list(self._profile.partition_key_candidates) if self._profile is not None
            else None
        )

        tables_cols, ddl_map, fks, col_comments = _reflect_mysql(self._connect, target_db)

        table_recs: list[TableRecord] = []
        col_recs: list[ColumnRecord] = []
        for t_name, cols in tables_cols.items():
            pk_col = _detect_partition_key(cols, partition_candidates)
            table_recs.append(
                TableRecord(
                    key=table_key(db, sch, t_name),
                    db=db,
                    schema=sch,
                    name=t_name,
                    layer=derive_layer(t_name, self._profile),
                    partition_key=pk_col,
                    comment="",
                    ddl=ddl_map.get(t_name, "") or "",
                )
            )
            for c in cols:
                comment = (col_comments.get(t_name, {}) or {}).get(c.name, "") or ""
                text = f"{db}.{t_name}.{c.name} ({c.type})"
                if comment:
                    text += f" — {comment}"
                col_recs.append(
                    ColumnRecord(
                        key=column_key(db, sch, t_name, c.name),
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
                    )
                )

        log.info(
            "MySQL reflected: db=%s schema=%s tables=%d columns=%d fks=%d",
            db, sch, len(table_recs), len(col_recs), len(fks),
        )
        return PhysicalManifest(
            db_id=db, schema=sch, tables=table_recs, columns=col_recs, fks=fks,
        )


# --------------------------------------------------------------------- #
# 内部辅助
# --------------------------------------------------------------------- #
def _detect_partition_key(
    cols: Sequence[ColumnInfo],
    candidates: Optional[Sequence[str]] = None,
) -> Optional[str]:
    if candidates is None:
        candidates = ("ds", "dt", "stat_date", "data_date")
    by_name = {c.name.lower(): c.name for c in cols}
    for cand in candidates:
        if cand.lower() in by_name:
            return by_name[cand.lower()]
    return None


def _reflect_mysql(
    connect_fn,
    database: str,
) -> tuple[dict[str, list[ColumnInfo]], dict[str, str], list[FKInfo], dict[str, dict[str, str]]]:
    """反射 MySQL 元数据，返回 (tables_cols, ddl_map, fks, col_comments)。"""
    tables_cols: dict[str, list[ColumnInfo]] = {}
    ddl_map: dict[str, str] = {}
    fks: list[FKInfo] = []
    col_comments: dict[str, dict[str, str]] = {}

    with connect_fn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_type IN ('BASE TABLE', 'VIEW')
                ORDER BY table_name
                """,
                (database,),
            )
            table_names = [r[0] for r in cur.fetchall()]
            for t in table_names:
                tables_cols[t] = []

            cur.execute(
                """
                SELECT table_name, column_name, column_type, is_nullable,
                       column_key, column_comment, ordinal_position
                FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position
                """,
                (database,),
            )
            for t, col, dtype, is_null, col_key, comment, _ord in cur.fetchall():
                if t not in tables_cols:
                    continue
                tables_cols[t].append(
                    ColumnInfo(
                        name=str(col),
                        type=(str(dtype) or "").upper() or "TEXT",
                        pk=(str(col_key).upper() == "PRI"),
                        nullable=(str(is_null).upper() == "YES"),
                    )
                )
                if comment:
                    col_comments.setdefault(str(t), {})[str(col)] = str(comment).strip()

            # View definition → DDL
            cur.execute(
                """
                SELECT table_name, view_definition
                FROM information_schema.views
                WHERE table_schema = %s
                """,
                (database,),
            )
            for vname, definition in cur.fetchall():
                body = (definition or "").strip()
                if body:
                    ddl_map[str(vname)] = body.rstrip(";") + ";"

            # 普通表合成 CREATE TABLE
            for t_name in table_names:
                if t_name in ddl_map:
                    continue
                cols = tables_cols.get(t_name, [])
                if not cols:
                    ddl_map[t_name] = ""
                    continue
                lines = [f"CREATE TABLE `{database}`.`{t_name}` ("]
                for i, c in enumerate(cols):
                    suf = "," if i < len(cols) - 1 else ""
                    nn = "" if c.nullable else " NOT NULL"
                    pk = " PRIMARY KEY" if c.pk else ""
                    lines.append(f"  `{c.name}` {c.type}{nn}{pk}{suf}")
                lines.append(");")
                ddl_map[t_name] = "\n".join(lines)

            # FK
            cur.execute(
                """
                SELECT kcu.table_name      AS src_table,
                       kcu.column_name     AS src_col,
                       kcu.referenced_table_name  AS dst_table,
                       kcu.referenced_column_name AS dst_col
                FROM information_schema.key_column_usage kcu
                WHERE kcu.table_schema = %s
                  AND kcu.referenced_table_name IS NOT NULL
                """,
                (database,),
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

    return tables_cols, ddl_map, fks, col_comments

"""DDL 文本解析 Adapter — 使用 sqlglot 从 CREATE TABLE 语句抽取元数据。"""
from __future__ import annotations

import re
from typing import Optional, Sequence

import sqlglot
from sqlglot import exp

from ...contracts.import_models import SourceConfig
from ...ingest import FKInfo
from ...utils import get_logger
from ..keys import (
    DEFAULT_SCHEMA,
    column_key,
    derive_layer,
    table_key,
)
from ..physical import ColumnRecord, TableRecord
from .base import ConnectionTestResult, PhysicalManifest

log = get_logger("graph.adapters.ddl")

_DDL_SPLIT_RE = re.compile(r";\s*\n")


class DDLAdapter:
    """从 DDL 文本（CREATE TABLE / CREATE VIEW）抽取表结构。"""

    def __init__(self, ddl_text: str, *, db_id: str, schema: str = DEFAULT_SCHEMA):
        self._ddl_text = ddl_text
        self._db_id = db_id
        self._schema = schema

    @classmethod
    def from_config(cls, config: SourceConfig, db_id: str) -> "DDLAdapter":
        ddl = config.ddl_text or ""
        if not ddl.strip():
            raise ValueError("DDL adapter requires ddl_text in source config")
        schema = config.schemas[0] if config.schemas else DEFAULT_SCHEMA
        return cls(ddl, db_id=db_id, schema=schema)

    def test_connection(self) -> ConnectionTestResult:
        try:
            stmts = _parse_statements(self._ddl_text)
            tables = [s for s in stmts if isinstance(s, exp.Create)]
            return ConnectionTestResult(
                success=True,
                message=f"parsed {len(tables)} CREATE statements",
                tables_found=len(tables),
            )
        except Exception as exc:
            return ConnectionTestResult(success=False, message=str(exc))

    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        db = self._db_id
        sch = schemas[0] if schemas else self._schema

        stmts = _parse_statements(self._ddl_text)
        ddl_by_table: dict[str, str] = {}
        for s in stmts:
            if isinstance(s, exp.Create):
                tname = _extract_table_name(s)
                if tname:
                    ddl_by_table[tname] = s.sql(dialect="postgres")

        table_recs: list[TableRecord] = []
        col_recs: list[ColumnRecord] = []
        fks: list[FKInfo] = []

        for s in stmts:
            if not isinstance(s, exp.Create):
                continue
            tname = _extract_table_name(s)
            if not tname:
                continue

            cols, pk_cols, inline_fks = _extract_columns(s, tname)
            table_recs.append(
                TableRecord(
                    key=table_key(db, sch, tname),
                    db=db,
                    schema=sch,
                    name=tname,
                    layer=derive_layer(tname),
                    partition_key=None,
                    comment=_extract_table_comment(s),
                    ddl=ddl_by_table.get(tname, ""),
                )
            )
            for col_name, col_type, is_pk, is_nullable in cols:
                text = f"{db}.{tname}.{col_name} ({col_type})"
                col_recs.append(
                    ColumnRecord(
                        key=column_key(db, sch, tname, col_name),
                        db=db,
                        schema=sch,
                        table=tname,
                        name=col_name,
                        type=col_type,
                        pk=is_pk,
                        nullable=is_nullable,
                        is_partition=False,
                        comment="",
                        description="",
                        text=text,
                    )
                )
            for fk in inline_fks:
                fks.append(fk)

        log.info("DDL parsed: %d tables, %d columns, %d FKs", len(table_recs), len(col_recs), len(fks))
        return PhysicalManifest(
            db_id=db, schema=sch, tables=table_recs, columns=col_recs, fks=fks,
        )


def _parse_statements(ddl_text: str) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    for dialect in ("postgres", "mysql", None):
        try:
            parsed = sqlglot.parse(ddl_text, dialect=dialect)
            out = [s for s in parsed if s is not None]
            if out:
                return out
        except Exception:
            continue
    return out


def _extract_table_name(create_stmt: exp.Create) -> Optional[str]:
    try:
        table_expr = create_stmt.find(exp.Table)
        if table_expr:
            return table_expr.name
    except Exception:
        pass
    return None


def _extract_table_comment(create_stmt: exp.Create) -> str:
    try:
        comment = create_stmt.args.get("comment")
        if comment:
            return str(comment).strip("'\"")
    except Exception:
        pass
    return ""


def _extract_columns(
    create_stmt: exp.Create,
    table_name: str,
) -> tuple[list[tuple[str, str, bool, bool]], list[str], list[FKInfo]]:
    cols: list[tuple[str, str, bool, bool]] = []
    pk_cols: list[str] = []
    fks: list[FKInfo] = []

    schema_expr = create_stmt.find(exp.Schema)
    if not schema_expr:
        return cols, pk_cols, fks

    for col_def in schema_expr.expressions:
        if isinstance(col_def, exp.ColumnDef):
            col_name = col_def.name
            col_type = col_def.args.get("kind")
            type_str = col_type.sql() if col_type else "TEXT"

            is_pk = False
            is_nullable = True
            for constraint in col_def.args.get("constraints", []):
                if isinstance(constraint, exp.ColumnConstraint):
                    kind = constraint.args.get("kind")
                    if isinstance(kind, exp.PrimaryKeyColumnConstraint):
                        is_pk = True
                        is_nullable = False
                        pk_cols.append(col_name)
                    elif isinstance(kind, exp.NotNullColumnConstraint):
                        is_nullable = False
                    elif isinstance(kind, exp.Reference):
                        ref_schema = kind.this
                        ref_table_node = ref_schema.find(exp.Table) if ref_schema else None
                        ref_table = ref_table_node.name if ref_table_node else ""
                        ref_col_ids = (
                            [e.name for e in ref_schema.expressions if isinstance(e, exp.Identifier)]
                            if isinstance(ref_schema, exp.Schema) else []
                        )
                        if ref_table and ref_col_ids:
                            fks.append(FKInfo(
                                src_table=table_name,
                                src_col=col_name,
                                dst_table=ref_table,
                                dst_col=ref_col_ids[0],
                            ))

            cols.append((col_name, type_str.upper(), is_pk, is_nullable))

        elif isinstance(col_def, exp.PrimaryKey):
            for c in col_def.expressions:
                pk_cols.append(c.name)
                for i, (cn, ct, _, cnl) in enumerate(cols):
                    if cn == c.name:
                        cols[i] = (cn, ct, True, False)

    return cols, pk_cols, fks

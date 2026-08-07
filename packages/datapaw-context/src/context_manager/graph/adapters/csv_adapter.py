"""CSV Adapter — 从 CSV 文件头推断表结构（每文件一张表）。"""
from __future__ import annotations

import base64
import csv
import io
from typing import Sequence

from ...contracts.import_models import SourceConfig
from ...utils import get_logger
from ..keys import DEFAULT_SCHEMA, column_key, table_key
from ..physical import ColumnRecord, TableRecord
from .base import ConnectionTestResult, PhysicalManifest

log = get_logger("graph.adapters.csv")

_MAX_SAMPLE_ROWS = 50


class CSVAdapter:
    """从 CSV 文本推断单表结构。"""

    def __init__(self, csv_text: str, *, db_id: str, table_name: str, schema: str = DEFAULT_SCHEMA):
        self._csv_text = csv_text
        self._db_id = db_id
        self._table_name = table_name
        self._schema = schema

    @classmethod
    def from_config(cls, config: SourceConfig, db_id: str) -> "CSVAdapter":
        raw = config.file_content or ""
        try:
            csv_text = base64.b64decode(raw).decode("utf-8")
        except Exception:
            csv_text = raw
        table_name = config.file_name or "csv_table"
        if table_name.endswith(".csv"):
            table_name = table_name[:-4]
        schema = config.schemas[0] if config.schemas else DEFAULT_SCHEMA
        return cls(csv_text, db_id=db_id, table_name=table_name, schema=schema)

    def test_connection(self) -> ConnectionTestResult:
        try:
            reader = csv.reader(io.StringIO(self._csv_text))
            header = next(reader)
            return ConnectionTestResult(
                success=True,
                message=f"CSV has {len(header)} columns",
                tables_found=1,
            )
        except Exception as exc:
            return ConnectionTestResult(success=False, message=str(exc))

    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest:
        db = self._db_id
        sch = schemas[0] if schemas else self._schema
        tname = self._table_name

        reader = csv.reader(io.StringIO(self._csv_text))
        header = next(reader)

        sample_rows: list[list[str]] = []
        for i, row in enumerate(reader):
            if i >= _MAX_SAMPLE_ROWS:
                break
            sample_rows.append(row)

        col_types = _infer_types(header, sample_rows)

        col_recs: list[ColumnRecord] = []
        for col_name, col_type in col_types:
            text = f"{db}.{tname}.{col_name} ({col_type})"
            col_recs.append(
                ColumnRecord(
                    key=column_key(db, sch, tname, col_name),
                    db=db,
                    schema=sch,
                    table=tname,
                    name=col_name,
                    type=col_type,
                    pk=False,
                    nullable=True,
                    is_partition=False,
                    comment="",
                    description="",
                    text=text,
                )
            )

        table_recs = [
            TableRecord(
                key=table_key(db, sch, tname),
                db=db,
                schema=sch,
                name=tname,
                layer="other",
                partition_key=None,
                comment="",
                ddl="",
            )
        ]

        log.info("CSV parsed: %d columns from %s", len(col_recs), tname)
        return PhysicalManifest(
            db_id=db, schema=sch, tables=table_recs, columns=col_recs, fks=[],
        )


def _infer_types(
    header: list[str],
    sample_rows: list[list[str]],
) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for i, col_name in enumerate(header):
        values = [
            row[i].strip()
            for row in sample_rows
            if i < len(row) and row[i].strip()
        ]
        col_type = _guess_type(values)
        out.append((col_name.strip(), col_type))
    return out


def _guess_type(values: list[str]) -> str:
    if not values:
        return "TEXT"
    all_int = True
    all_float = True
    for v in values[:20]:
        try:
            int(v)
        except ValueError:
            all_int = False
        try:
            float(v)
        except ValueError:
            all_float = False
    if all_int:
        return "INTEGER"
    if all_float:
        return "NUMERIC"
    return "TEXT"

"""DuckDB adapter — minimal overrides on UniversalConnector (single local
file, schema == ``main``; reflection via duckdb-engine)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from pydantic import BaseModel

from ...secrets.schemas import DuckdbConnection
from ...utils import get_logger
from .base import ConnectorError
from .universal import UniversalConnector

log = get_logger("graph.adapters.duckdb")

#: DuckDB 默认 schema 名（写进图节点 ``schema`` 字段）。
_DUCKDB_SCHEMA = "main"


class DuckDBConnector(UniversalConnector):
    """Single-file analytical source: no host / credentials; construction
    validates the file exists because duckdb silently creates missing
    databases."""

    connection_model = DuckdbConnection

    def __init__(self, conn: DuckdbConnection, *, db_id: str):
        path = Path(conn.path).expanduser()
        if not path.is_file():
            raise ConnectorError(f"DuckDB 数据库文件不存在: {path}")
        super().__init__(conn, db_id=db_id)

    def _engine_url(self, conn: BaseModel):
        from sqlalchemy import URL

        return URL.create(
            "duckdb",
            database=str(Path(conn.path).expanduser()),
        )

    def _connect_args(self) -> dict:
        read_only = getattr(self._conn, "read_only", True)
        return {"read_only": bool(read_only)}

    def _resolve_schema(self, schemas: Sequence[str]) -> Optional[str]:
        return None  # 单文件库走 inspector 默认 schema（main）

    def _schema_label(self, resolved: Optional[str]) -> str:
        return _DUCKDB_SCHEMA


__all__ = ["DuckDBConnector"]

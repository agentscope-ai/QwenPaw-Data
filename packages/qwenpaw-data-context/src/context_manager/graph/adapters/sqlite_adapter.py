"""SQLite adapter — minimal overrides on UniversalConnector (single local
file, schema == ``main``)."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from pydantic import BaseModel

from ...secrets.schemas import SqliteConnection
from ...utils import get_logger
from .base import ConnectorError
from .universal import UniversalConnector

log = get_logger("graph.adapters.sqlite")

#: SQLite 唯一的隐式 schema 名（写进图节点 ``schema`` 字段）。
_SQLITE_SCHEMA = "main"


class SQLiteConnector(UniversalConnector):
    """Single-file source: no host / credentials; construction validates the
    file exists because sqlite3 silently creates missing databases."""

    connection_model = SqliteConnection

    def __init__(self, conn: SqliteConnection, *, db_id: str):
        path = Path(conn.path).expanduser()
        if not path.is_file():
            raise ConnectorError(f"SQLite 数据库文件不存在: {path}")
        super().__init__(conn, db_id=db_id)

    def _engine_url(self, conn: BaseModel):
        from sqlalchemy import URL

        path = str(Path(conn.path).expanduser())
        if getattr(conn, "read_only", True):
            # sqlite:///file:<path>?mode=ro&uri=true — 只读 URI 打开
            return URL.create(
                "sqlite",
                database=f"file:{path}",
                query={"mode": "ro", "uri": "true"},
            )
        return URL.create("sqlite", database=path)

    def _connect_args(self) -> dict:
        # Engine 会被跨线程复用，必须关掉 sqlite3 的同线程检查。
        return {"check_same_thread": False, "timeout": 5}

    def _resolve_schema(self, schemas: Sequence[str]) -> Optional[str]:
        return None  # 单库无 schema 概念；inspector 走默认

    def _schema_label(self, resolved: Optional[str]) -> str:
        return _SQLITE_SCHEMA


__all__ = ["SQLiteConnector"]

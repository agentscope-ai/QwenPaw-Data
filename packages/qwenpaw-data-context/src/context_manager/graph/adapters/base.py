"""Source Adapter 协议与通用数据结构。

所有 Adapter 实现 :class:`SourceAdapter` 协议，产出 :class:`PhysicalManifest`。
``PhysicalManifest`` 内嵌的 ``TableRecord`` / ``ColumnRecord`` / ``FKInfo``
复用 :mod:`context_manager.graph.physical` 已有结构，确保 ``write_physical`` 可直接消费。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence, runtime_checkable

from ..physical import ColumnRecord, TableRecord
from ...ingest import FKInfo


@dataclass
class PhysicalManifest:
    """Adapter 抽取的物理层元数据，供 ``write_physical`` 写入 Neo4j。"""

    db_id: str
    schema: str
    tables: list[TableRecord] = field(default_factory=list)
    columns: list[ColumnRecord] = field(default_factory=list)
    fks: list[FKInfo] = field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        return [t.name for t in self.tables]


@dataclass
class ConnectionTestResult:
    """连接测试结果。"""

    success: bool
    message: str = ""
    tables_found: int = 0


@runtime_checkable
class SourceAdapter(Protocol):
    """数据源 Adapter 协议。

    每个实现负责：
    1. ``test_connection`` — 验证连接可用性
    2. ``extract_metadata`` — 抽取表/列/FK 元数据为 ``PhysicalManifest``
    """

    def test_connection(self) -> ConnectionTestResult: ...

    def extract_metadata(self, schemas: Sequence[str]) -> PhysicalManifest: ...


# ---------------------------------------------------------------------- #
# Connector framework extensions
# ---------------------------------------------------------------------- #
class ConnectorError(Exception):
    """The single exception type of the access layer; callers need one
    ``except ConnectorError``."""

    def __init__(self, message: str, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause

    def __str__(self) -> str:  # keep logs readable: append the cause summary
        if self.cause is not None:
            return f"{self.message} (cause: {self.cause})"
        return self.message


@dataclass
class ExecResult:
    """SQL execution result; ``logview_url``/``instance_id``/``task_status``
    are ODPS-only."""

    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[list] = field(default_factory=list)
    row_count: int = 0
    truncated: bool = False
    elapsed_ms: float = 0.0
    error: str | None = None
    logview_url: str | None = None
    instance_id: str | None = None
    task_status: str | None = None

    def to_dict(self) -> dict:
        d = {
            "sql": self.sql,
            "columns": self.columns,
            "rows": [[_jsonable(v) for v in r] for r in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "error": self.error,
        }
        if self.logview_url:
            d["logview_url"] = self.logview_url
        if self.instance_id:
            d["instance_id"] = self.instance_id
        if self.task_status:
            d["task_status"] = self.task_status
        return d


@runtime_checkable
class SqlExecutable(Protocol):
    """Capability protocol: connectable sources that can run read queries."""

    def execute_sql(self, sql: str, *, max_rows: int = 200) -> ExecResult: ...


class BaseConnector:
    """Common adapter base: holds ``db_id`` and a ``close()`` cleanup hook."""

    #: Pooling semantics marker; ``False`` opts an adapter out of pooling
    #: and metadata caching.
    poolable: bool = True

    def __init__(self, *, db_id: str):
        self._db_id = db_id

    @property
    def db_id(self) -> str:
        return self._db_id

    def close(self) -> None:
        """Release resources held by the adapter (no-op by default)."""
        return None


def _jsonable(v):
    """Convert datetime / Decimal and friends into JSON-friendly types."""
    import datetime as _dt
    from decimal import Decimal

    if v is None or isinstance(v, (str, int, float, bool, list, dict)):
        return v
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (_dt.date, _dt.datetime, _dt.time)):
        return v.isoformat()
    return str(v)

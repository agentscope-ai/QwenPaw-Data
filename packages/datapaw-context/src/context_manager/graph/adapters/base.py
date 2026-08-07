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

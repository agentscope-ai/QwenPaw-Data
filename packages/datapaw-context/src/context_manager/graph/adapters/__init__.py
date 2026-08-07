"""Source Adapter 抽象层：统一各数据源的元数据抽取接口。

每个 Adapter 实现 :class:`SourceAdapter` 协议，产出 :class:`PhysicalManifest`，
下游的 ``write_physical`` 可直接消费。

使用 ``get_adapter`` 按 :class:`SourceType` 获取对应实现。
"""
from .base import ConnectionTestResult, PhysicalManifest, SourceAdapter
from .registry import get_adapter, register_adapter

__all__ = [
    "ConnectionTestResult",
    "PhysicalManifest",
    "SourceAdapter",
    "get_adapter",
    "register_adapter",
]

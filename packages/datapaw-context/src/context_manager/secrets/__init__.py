"""凭证日志脱敏 + TypedConnection 类型定义。

- ``redact``: 全局 logging.Filter + ``_redact_str`` helper
- ``schemas``: ``TypedConnection`` discriminated union（import API 的连接类型）
"""
from __future__ import annotations

from .redact import CredentialRedactFilter, _redact_str
from .schemas import (
    TypedConnection,
    PostgresConnection, MySQLConnection, HologresConnection,
    BigQueryConnection, SnowflakeConnection, OdpsConnection,
    DDLConnection, CSVConnection, SqliteConnection,
)

__all__ = [
    "CredentialRedactFilter", "_redact_str",
    "TypedConnection",
    "PostgresConnection", "MySQLConnection", "HologresConnection",
    "BigQueryConnection", "SnowflakeConnection", "OdpsConnection",
    "DDLConnection", "CSVConnection", "SqliteConnection",
]

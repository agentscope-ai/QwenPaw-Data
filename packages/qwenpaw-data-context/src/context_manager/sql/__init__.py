"""Scoped datasource access, database connections, and typed SQL execution."""

from .config import (
    BigQueryConfig,
    ConnectionConfig,
    DuckDbConfig,
    MySqlConfig,
    MySqlSslMode,
    OdpsConfig,
    PostgresConfig,
    PostgresSslMode,
    RdbBackend,
    RdbTarget,
    SqliteConfig,
)
from .connectors import RdbConnection, RdbConnector, RdbError, RdbProbe
from .datasource import (
    Datasource,
    DatasourceError,
    DatasourceErrorCode,
    DatasourceRepository,
)
from .query import (
    SqlAdmissionError,
    SqlExecutionError,
    SqlQueryService,
    SqlResult,
)

__all__ = [
    "BigQueryConfig",
    "ConnectionConfig",
    "Datasource",
    "DatasourceError",
    "DatasourceErrorCode",
    "DatasourceRepository",
    "DuckDbConfig",
    "MySqlConfig",
    "MySqlSslMode",
    "OdpsConfig",
    "PostgresConfig",
    "PostgresSslMode",
    "RdbBackend",
    "RdbConnection",
    "RdbConnector",
    "RdbError",
    "RdbProbe",
    "RdbTarget",
    "SqlAdmissionError",
    "SqlExecutionError",
    "SqlQueryService",
    "SqlResult",
    "SqliteConfig",
]

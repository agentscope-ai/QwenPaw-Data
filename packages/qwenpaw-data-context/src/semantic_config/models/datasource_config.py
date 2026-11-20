"""Data-source connection configs (the ``config`` column) — one model per protocol with ``extra="forbid"``."""

from __future__ import annotations

from enum import Enum

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError

from semantic_config.errors import BadRequestError


class DatasourceType(str, Enum):
    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    ODPS = "odps"
    # MySQL 线协议兼容引擎：注册/校验/连测/执行全部复用 MySQL 通道。
    STARROCKS = "starrocks"
    DORIS = "doris"
    TIDB = "tidb"
    # 单文件分析库：无 host/凭证，只需本地路径。
    DUCKDB = "duckdb"
    SQLITE = "sqlite"
    BIGQUERY = "bigquery"


#: MySQL 线协议兼容类型（PyMySQL 直连 + information_schema 反射均可用）。
MYSQL_COMPATIBLE_TYPES: frozenset[DatasourceType] = frozenset(
    {
        DatasourceType.MYSQL,
        DatasourceType.STARROCKS,
        DatasourceType.DORIS,
        DatasourceType.TIDB,
    }
)

#: 单文件类型（连接配置只有 ``path``）。
FILE_DATABASE_TYPES: frozenset[DatasourceType] = frozenset(
    {DatasourceType.DUCKDB, DatasourceType.SQLITE}
)


class PostgresConfig(BaseModel):
    """PostgreSQL / Hologres connection."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 5432
    # 0.3 PostgreSQL configs stored the psycopg key "dbname".
    database: str = Field(validation_alias=AliasChoices("database", "dbname"))
    user: str
    password: str


class MysqlConfig(BaseModel):
    """MySQL connection (in MySQL, schema and database are equivalent)."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 3306
    database: str
    user: str
    password: str


class OdpsConfig(BaseModel):
    """ODPS / MaxCompute public connection (PyODPS); STS when ``sts_token`` is set."""

    model_config = ConfigDict(extra="forbid")

    access_key_id: str
    access_key_secret: str
    project: str
    endpoint: str
    sts_token: str | None = None


class DuckdbConfig(BaseModel):
    """DuckDB 单文件连接（duckdb 原生驱动，只读打开）。"""

    model_config = ConfigDict(extra="forbid")

    path: str


class SqliteConfig(BaseModel):
    """SQLite connection (server-local database file); field names mirror SQLiteConnection."""

    model_config = ConfigDict(extra="forbid")

    path: str
    read_only: bool = True


class BigQueryConfig(BaseModel):
    """BigQuery connection (billing project + SA JSON); field names mirror BigQueryConnection."""

    model_config = ConfigDict(extra="forbid")

    project: str
    credentials_json: str
    location: str | None = None
    maximum_bytes_billed: int | None = None


CONFIG_MODEL_BY_TYPE: dict[DatasourceType, type[BaseModel]] = {
    DatasourceType.POSTGRESQL: PostgresConfig,
    DatasourceType.MYSQL: MysqlConfig,
    DatasourceType.ODPS: OdpsConfig,
    # MySQL 兼容引擎共用同一份连接模型。
    DatasourceType.STARROCKS: MysqlConfig,
    DatasourceType.DORIS: MysqlConfig,
    DatasourceType.TIDB: MysqlConfig,
    DatasourceType.DUCKDB: DuckdbConfig,
    DatasourceType.SQLITE: SqliteConfig,
    DatasourceType.BIGQUERY: BigQueryConfig,
}


#: Legacy type alias folding (write path); see the read-path fallback in graph.connectors.registry.
_LEGACY_TYPE_ALIASES = {
    "postgres": "postgresql",
    "hologres": "postgresql",
    "holo": "postgresql",
}


def resolve_type(datasource_type: str | None) -> DatasourceType:
    """Normalize an incoming type string to :class:`DatasourceType` (case-insensitive, alias-aware)."""
    key = (datasource_type or "").strip().lower()
    key = _LEGACY_TYPE_ALIASES.get(key, key)
    try:
        return DatasourceType(key)
    except ValueError as exc:
        raise BadRequestError(f"数据源类型不支持: {datasource_type}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(x) for x in err.get("loc", ()))
        parts.append(f"{loc}: {err.get('msg', '')}".strip(": "))
    return "; ".join(parts) or "config 校验失败"


def validate_config(datasource_type: str | None, config: dict | None) -> dict:
    """Validate *config* against the model of *datasource_type*; return a normalized dict (failures raise ``BadRequestError`` → 400)."""
    if config is None:
        raise BadRequestError("config 不能为空")
    dt = resolve_type(datasource_type)
    model = CONFIG_MODEL_BY_TYPE[dt]
    try:
        obj = model.model_validate(config)
    except ValidationError as exc:
        raise BadRequestError(
            f"config 校验失败: {_format_validation_error(exc)}"
        ) from exc
    return obj.model_dump()


__all__ = [
    "DatasourceType",
    "MYSQL_COMPATIBLE_TYPES",
    "FILE_DATABASE_TYPES",
    "PostgresConfig",
    "MysqlConfig",
    "OdpsConfig",
    "DuckdbConfig",
    "SqliteConfig",
    "BigQueryConfig",
    "CONFIG_MODEL_BY_TYPE",
    "resolve_type",
    "validate_config",
]

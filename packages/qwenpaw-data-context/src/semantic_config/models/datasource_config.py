"""数据源连接配置（config 字段）—— 每个协议一套 key，用独立对象封装。

约定：
- ``config`` 在 SQLite 里存 TEXT（JSON 字符串）；出入参在 API 层是 dict。
- 每个 ``datasource_type`` 对应一个 pydantic 配置模型（key/类型/默认值单点约定），
  前端可据此渲染连接表单。
- ``extra="forbid"``：多传未知 key 直接校验失败，避免拼错字段静默通过。
- key 命名贴合各自原生驱动/协议：PG 用 ``dbname``（psycopg），MySQL 用 ``database``（JDBC）。
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, ValidationError

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
    """PostgreSQL / Hologres 连接（psycopg 风格）。"""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 5432
    dbname: str
    user: str
    password: str


class MysqlConfig(BaseModel):
    """MySQL 连接（JDBC ``jdbc:mysql://host:port/database`` 风格）。"""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = 3306
    database: str
    user: str
    password: str


class OdpsConfig(BaseModel):
    """ODPS / MaxCompute 连接（PyODPS）。``sts_token`` 存在时走 STS 鉴权。"""

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
    """SQLite 单文件连接（sqlite3，``mode=ro`` 只读 URI 打开）。"""

    model_config = ConfigDict(extra="forbid")

    path: str


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
}

#: 展示名（``/datasource/types`` 端点使用；前端下拉据此渲染）。
TYPE_LABELS: dict[DatasourceType, str] = {
    DatasourceType.MYSQL: "MySQL",
    DatasourceType.POSTGRESQL: "PostgreSQL",
    DatasourceType.ODPS: "ODPS",
    DatasourceType.STARROCKS: "StarRocks",
    DatasourceType.DORIS: "Apache Doris",
    DatasourceType.TIDB: "TiDB",
    DatasourceType.DUCKDB: "DuckDB",
    DatasourceType.SQLITE: "SQLite",
}


def resolve_type(datasource_type: str | None) -> DatasourceType:
    """把外部传入的类型字符串归一为 :class:`DatasourceType`（大小写不敏感）。"""
    key = (datasource_type or "").strip().lower()
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
    """按 ``datasource_type`` 选对应模型校验 ``config``，返回规范化后的 dict（供落库）。

    校验失败（缺 key / 多余 key / 类型不符）抛 :class:`BadRequestError` → 400。
    """
    if config is None:
        raise BadRequestError("config 不能为空")
    dt = resolve_type(datasource_type)
    model = CONFIG_MODEL_BY_TYPE[dt]
    try:
        obj = model.model_validate(config)
    except ValidationError as exc:
        raise BadRequestError(f"config 校验失败: {_format_validation_error(exc)}") from exc
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
    "CONFIG_MODEL_BY_TYPE",
    "TYPE_LABELS",
    "resolve_type",
    "validate_config",
]

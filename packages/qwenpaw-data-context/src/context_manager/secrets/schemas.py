"""TypedConnection discriminated union。

``connection.type`` 是唯一 discriminator。所有 sensitive 字段类型为
``SecretStr``（pydantic），由 import API 直接接收明文。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional, Union

from pydantic import BaseModel, Field, SecretStr


class PostgresConnection(BaseModel):
    type: Literal["postgres"]
    host: str
    port: int = 5432
    database: str
    user: str
    password: SecretStr


class MySQLConnection(BaseModel):
    # MySQL 线协议兼容引擎（StarRocks/Doris/TiDB）共用同一连接形态。
    type: Literal["mysql", "starrocks", "doris", "tidb"]
    host: str
    port: int = 3306
    database: str
    user: str
    password: SecretStr


class HologresConnection(BaseModel):
    type: Literal["hologres"]
    host: str
    port: int = 80
    database: str
    user: str                                          # 通常是 AccessKeyId
    password: SecretStr                               # 通常是 AccessKeySecret
    access_key_id: Optional[SecretStr] = None
    access_key_secret: Optional[SecretStr] = None


class BigQueryConnection(BaseModel):
    type: Literal["bigquery"]
    project_id: str
    location: str = "US"
    service_account_json: SecretStr
    maximum_bytes_billed: Optional[int] = Field(
        default=None, ge=1, description="单条查询计费字节上限（费用护栏）"
    )


class SnowflakeConnection(BaseModel):
    type: Literal["snowflake"]
    account: str
    warehouse: str
    database: str
    schema_: str = Field(default="PUBLIC", alias="schema")
    user: str
    password: Optional[SecretStr] = None
    private_key_pem: Optional[SecretStr] = None
    private_key_passphrase: Optional[SecretStr] = None

    model_config = {"populate_by_name": True}


class OdpsConnection(BaseModel):
    type: Literal["odps"]
    access_key_id: SecretStr
    access_key_secret: SecretStr
    project: str = Field(description="MaxCompute project 名称")
    endpoint: str = Field(
        default="http://service.cn-hangzhou.maxcompute.aliyun.com/api",
        description="MaxCompute 服务 endpoint",
    )


class DDLConnection(BaseModel):
    type: Literal["ddl"]


class CSVConnection(BaseModel):
    type: Literal["csv"]


class SqliteConnection(BaseModel):
    type: Literal["sqlite"]
    path: str
    read_only: bool = Field(
        default=True, description="只读模式打开（mode=ro，防止误写）"
    )


class DuckdbConnection(BaseModel):
    type: Literal["duckdb"]
    path: str
    read_only: bool = Field(
        default=True, description="只读模式打开（防止误写）"
    )


TypedConnection = Annotated[
    Union[
        PostgresConnection,
        MySQLConnection,
        HologresConnection,
        BigQueryConnection,
        SnowflakeConnection,
        OdpsConnection,
        DDLConnection,
        CSVConnection,
        SqliteConnection,
        DuckdbConnection,
    ],
    Field(discriminator="type"),
]

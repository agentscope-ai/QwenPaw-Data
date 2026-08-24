"""Import request and result contracts shared across application layers.

The API layer owns HTTP transport only; graph adapters and background services
depend on these neutral contracts instead of importing from the API package.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from ..secrets.schemas import TypedConnection

__all__ = [
    "ColumnPayload",
    "ConnectionTestRequest",
    "ConnectionTestResult",
    "DatasetPayload",
    "DimensionBinding",
    "DimensionPayload",
    "DimensionValuePayload",
    "DomainPayload",
    "FormulaPayload",
    "ImportErrorItem",
    "ImportErrorLevel",
    "ImportOptions",
    "ImportRequest",
    "ImportResult",
    "ImportStats",
    "ImportStatus",
    "ManifestSummary",
    "MetricDerivedFrom",
    "MetricPayload",
    "SemanticImportRequest",
    "SemanticImportResult",
    "SemanticPayload",
    "SourceConfig",
    "SourceType",
]


# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #
class SourceType(str, Enum):
    postgres = "postgres"
    mysql = "mysql"
    hologres = "hologres"
    hive = "hive"
    clickhouse = "clickhouse"
    bigquery = "bigquery"
    snowflake = "snowflake"
    odps = "odps"
    ddl = "ddl"
    csv = "csv"
    sqlite = "sqlite"
    duckdb = "duckdb"


class ImportErrorLevel(str, Enum):
    fatal = "fatal"
    degrade = "degrade"
    warn = "warn"


class ImportStatus(str, Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    degraded = "degraded"


# ---------------------------------------------------------------------- #
# 数据源配置
# ---------------------------------------------------------------------- #
class SourceConfig(BaseModel):
    """import 数据源配置。

    Breaking change(2026-06-04):删除 ``type`` 字段;``connection.type`` 是唯一 discriminator。
    ``ddl`` / ``csv`` 形态仍然要带 ``connection: {type: ...}``,以统一前端表单 discriminator。
    """
    connection: TypedConnection = Field(
        description="按 connection.type 区分的 discriminated union;见 secrets.schemas",
    )
    schemas: list[str] = Field(
        default_factory=lambda: ["public"],
        description="要导入的 schema 列表,支持通配符如 dws_*",
    )
    ddl_text: Optional[str] = Field(default=None, description="DDL 文本(connection.type=ddl 时)")
    file_content: Optional[str] = Field(
        default=None,
        description="文件内容 Base64(csv / sqlite / duckdb 时)",
    )
    file_name: Optional[str] = Field(default=None, description="文件名(csv 等)")


# ---------------------------------------------------------------------- #
# 构建选项
# ---------------------------------------------------------------------- #
class ImportOptions(BaseModel):
    semantic_providers: list[str] = Field(
        default_factory=lambda: ["schema_auto"],
        description="Connect 导入时的语义层 provider 列表，默认 ['schema_auto']",
    )
    do_join_inference: bool = True
    do_knowledge: bool = False
    do_trace: bool = False
    drop_topology_first: bool = False
    dry_run: bool = Field(default=True, description="只抽取 manifest 不写入 Neo4j")


# ---------------------------------------------------------------------- #
# 嵌套 Semantic Payload（前端配置模型）
# ---------------------------------------------------------------------- #
class ColumnPayload(BaseModel):
    """Dataset 投影后的列。

    物理反射拿得到的字段（name/data_type/is_primary/is_nullable）以反射结果为准；
    本模型只用于补"业务增强字段"（name_cn / enums / samples 等）。
    """
    name: str
    data_type: str = ""
    is_primary: bool = False
    is_nullable: bool = True
    comment: str = ""
    name_cn: str = ""
    column_type: str = ""
    enums: Optional[list[str]] = None
    enums_description: Optional[list[str]] = None
    samples: Optional[list[Any]] = None
    dimension_type: str = ""

    @field_validator("enums", "enums_description", mode="before")
    @classmethod
    def _split_dollar_separated(cls, v: Any) -> Any:
        """自动将 ``"a$$$b$$$c"`` 拆成 ``["a", "b", "c"]``。"""
        if isinstance(v, str):
            return [s for s in v.split("$$$") if s]
        return v


class DatasetPayload(BaseModel):
    name: str = Field(description="数据集名，全局唯一")
    description: str = ""
    dataset_type: str = "OLAP"
    sql: str = Field(default="*", description="`*` 或空 = 直通父表；否则是切片 SQL")
    parents: list[str] = Field(
        default_factory=list,
        description="父级物理表，至少 1 张；多张表示 union",
    )
    columns: list[ColumnPayload] = Field(default_factory=list)


class DimensionBinding(BaseModel):
    dataset: str = Field(description="必须在本 payload datasets[] 里能找到")
    calculate_expr: str = ""
    binding_type: str = "OLAP维度"
    data_type: str = "text"
    aliases: list[str] = Field(default_factory=list)


class DimensionValuePayload(BaseModel):
    value: str
    aliases: list[str] = Field(default_factory=list)


class DimensionPayload(BaseModel):
    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    parent_dimension: str = ""
    hierarchy_level: int = 0
    is_display_dimension: bool = True
    is_contribution_dimension: bool = True
    bindings: list[DimensionBinding] = Field(default_factory=list)
    values: list[DimensionValuePayload] = Field(default_factory=list)


class FormulaPayload(BaseModel):
    dataset: str = Field(description="必须在本 payload datasets[] 里能找到")
    formula: str = Field(default="", description="可直接拼 SELECT 的表达式")
    formula_evidence: str = ""
    date_range: str = ""
    derived_from: str = ""
    is_primary: bool = False

    @model_validator(mode="after")
    def _fill_formula_from_evidence(self) -> "FormulaPayload":
        """formula 未传时自动取 formula_evidence。"""
        if not self.formula and self.formula_evidence:
            self.formula = self.formula_evidence
        return self


class MetricDerivedFrom(BaseModel):
    metric_name: str
    relation_type: str = "ratio_decompose"
    role: str = ""


class MetricPayload(BaseModel):
    name: str
    description: str = ""
    unit: str = ""
    is_north_star: bool = False
    is_display_distribution: bool = True
    is_display: bool = True
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    formulas: list[FormulaPayload] = Field(default_factory=list)
    analyzed_by: list[str] = Field(default_factory=list)
    derived_from: list[MetricDerivedFrom] = Field(default_factory=list)


class DomainPayload(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    datasets: list[DatasetPayload] = Field(default_factory=list)
    dimensions: list[DimensionPayload] = Field(default_factory=list)
    metrics: list[MetricPayload] = Field(default_factory=list)


class SemanticPayload(BaseModel):
    """嵌套语义层 payload，对应配置管理前端的数据模型。

    domain → datasets/dimensions/metrics 的嵌套树。dimensions[].bindings[].dataset 和
    metrics[].formulas[].dataset 必须引用本 payload `datasets[]` 中已有的 name。
    """
    domains: list[DomainPayload] = Field(default_factory=list)


# ---------------------------------------------------------------------- #
# Import 请求
# ---------------------------------------------------------------------- #
class ImportRequest(BaseModel):
    datasource_id: str = Field(description="数据源标识，须与数据源注册时一致")
    datasource_name: Optional[str] = Field(
        default=None,
        description="[deprecated] 字段名保留向后兼容；未传时由 datasource_id 兜底",
    )
    mode: Literal["full"] = Field(default="full", description="构建模式；第一阶段仅 full")
    credential_ref: Optional[str] = Field(
        default=None,
        description="风格 B(spec §4.4):引用 vault 中已注册的凭证;若提供则 connection 的 "
                    "sensitive 字段可省略,后端合并 vault 中明文",
    )
    source: SourceConfig
    callback_url: Optional[str] = Field(
        default=None,
        description="import 完成后回调通知的 URL，CM 会 POST {task_id, status, error_msg}",
    )
    options: ImportOptions = Field(default_factory=ImportOptions)

    @model_validator(mode="after")
    def _fill_datasource_name_from_id(self) -> "ImportRequest":
        """datasource_name 未传时用 datasource_id 兜底，保持向后兼容。"""
        if not self.datasource_name:
            self.datasource_name = self.datasource_id
        return self


# ---------------------------------------------------------------------- #
# Import 结果
# ---------------------------------------------------------------------- #
class ImportErrorItem(BaseModel):
    level: ImportErrorLevel
    message: str
    context: str = ""


class ImportStats(BaseModel):
    tables: int = 0
    columns: int = 0
    fks: int = 0
    semantic_nodes: dict[str, int] = Field(
        default_factory=dict,
        description="语义节点统计，如 {'stable': 10, 'review': 3, 'pending': 1}",
    )


class ManifestSummary(BaseModel):
    db_id: str = ""
    db_schema: str = Field(default="", description="数据库 schema，如 'public'")
    source_type: str = ""
    table_names: list[str] = Field(default_factory=list)


class ImportResult(BaseModel):
    task_id: str
    status: ImportStatus
    errors: list[ImportErrorItem] = Field(default_factory=list)
    stats: ImportStats = Field(default_factory=ImportStats)
    manifest_summary: ManifestSummary = Field(default_factory=ManifestSummary)
    credential_ref: Optional[str] = Field(
        default=None,
        description="vault 存入后的引用 id(P1);风格 B 复用 / 后续 refresh 用",
    )
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------- #
# 连接测试
# ---------------------------------------------------------------------- #
class ConnectionTestRequest(BaseModel):
    source: SourceConfig


class ConnectionTestResult(BaseModel):
    success: bool
    message: str = ""
    tables_found: int = 0


# ---------------------------------------------------------------------- #
# Semantic-only Import
# ---------------------------------------------------------------------- #
class SemanticImportRequest(BaseModel):
    """POST /api/v1/semantic/import — 纯语义层导入，不涉及物理连接。

    适用于配置管理前端：物理数据源已注册，
    config 只推送语义配置（域/数据集/维度/指标）。

    调用方可传 ``task_id`` + ``callback_url``，CM 写入完成后
    POST ``{task_id, status, error_msg}`` 到回调地址通知调用方。
    """
    datasource_id: str = Field(
        description="数据源标识，须与数据源注册时一致；用于语义层节点 scope 打标"
    )
    datasource_name: Optional[str] = Field(
        default=None,
        description="[deprecated] 字段名保留向后兼容；未传 datasource_name 时由 "
                    "datasource_id 兜底",
    )
    db_id: str = Field(
        default="", description="数据库标识；不传时从 datasource_id 自动推导"
    )
    schema_name: str = Field(default="public", description="schema 名")
    semantic: SemanticPayload
    drop_semantic_first: bool = Field(
        default=False,
        description="是否先清空该 datasource 下的语义节点再写入",
    )
    task_id: Optional[str] = Field(
        default=None,
        description="调用方的任务 ID（如 TASK_20260625103000_AB12），回调时原样返回",
    )
    callback_url: Optional[str] = Field(
        default="",
        description="回调地址，CM 完成后 POST {task_id, status, error_msg}；传空字符串可禁用",
    )

    @model_validator(mode="after")
    def _fill_datasource_name_from_id(self) -> "SemanticImportRequest":
        """datasource_name 未传时用 datasource_id 兜底，保持向后兼容。"""
        if not self.datasource_name:
            self.datasource_name = self.datasource_id
        return self


class SemanticImportResult(BaseModel):
    task_id: str
    status: ImportStatus
    errors: list[ImportErrorItem] = Field(default_factory=list)
    stats: ImportStats = Field(default_factory=ImportStats)
    elapsed_seconds: float = 0.0

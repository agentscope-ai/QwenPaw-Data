"""Pydantic models for the CM API."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------- #
# MCP (REST discovery — GET, not Streamable HTTP)
# ---------------------------------------------------------------------- #


class MCPToolInfo(BaseModel):
    """One MCP tool exposed by the unified CM server."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------- #
# Shared
# ---------------------------------------------------------------------- #

class AmbiguityCandidate(BaseModel):
    entity_type: str
    name: str
    domain: str = ""
    description: str = ""
    match_confidence: float = 0.0
    disambiguation_hint: str = ""


class SimilarExperience(BaseModel):
    question: str
    lesson: str
    similarity: float = 0.0


class TimeHints(BaseModel):
    as_of_date: str = ""
    inferred_year: str = ""
    partition_format: str = ""


class AmbiguousResponse(BaseModel):
    ambiguous: bool = True
    ambiguity_candidates: list[AmbiguityCandidate] = Field(default_factory=list)
    hint: str = ""


class SourceColumn(BaseModel):
    name: str
    dataset: str = ""
    role: str = ""
    granularity_role: str = ""
    topline_value: str = ""


class CommonFilter(BaseModel):
    description: str = ""
    sql_fragment: str = ""


class DrillDimension(BaseModel):
    name: str
    relationship: str = ""


# ---------------------------------------------------------------------- #
# Shared semantic-card vocabulary (capped + relevance-ranked)
# ---------------------------------------------------------------------- #

class MetricFocus(BaseModel):
    """Full metric card with all semantic context, caps enforced by semantic_pack."""
    metric_name: str
    aliases: list[str] = Field(default_factory=list)
    role: str = ""
    unit: str = ""
    description: str = ""
    caliber: str = Field("", description="Formula evidence or definition")
    source_columns: list[SourceColumn] = Field(default_factory=list)
    drill_dimensions: list[DrillDimension] = Field(default_factory=list)
    common_filters: list[CommonFilter] = Field(default_factory=list)
    relevance_score: float = Field(0.0, ge=0.0, le=1.0)


class MetricCandidate(BaseModel):
    """Brief metric reference for alternatives."""
    metric_name: str
    description: str = ""
    relevance_score: float = Field(0.0, ge=0.0, le=1.0)
    disambiguation_hint: str = ""


class KnowledgeNote(BaseModel):
    """Threshold-gated knowledge snippet."""
    label: str
    name: str
    summary: str = ""
    relevance_score: float = Field(0.0, ge=0.0, le=1.0)


class EventCard(BaseModel):
    """Relevance-ranked event with capped description."""
    name: str
    type: str = ""
    scope: str = ""
    date_from: str = ""
    date_to: str = ""
    summary: str = ""
    relevance_score: float = Field(0.0, ge=0.0, le=1.0)
    about_entity_name: str = ""


# ---------------------------------------------------------------------- #
# L1 — Intent Understanding
# ---------------------------------------------------------------------- #

class SearchContextScope(BaseModel):
    domain: Optional[str] = None
    as_of_date: Optional[str] = None


class QueryRelevance(BaseModel):
    """整体相关度评估，用于判断 query 是否与知识库匹配。"""
    status: Literal["relevant", "low_confidence", "no_match"]
    score: float = Field(0.0, ge=0.0, le=1.0)
    detail: str = ""


class SearchContextRequest(BaseModel):
    session_ref: Optional[str] = None
    query: str
    scope: Optional[SearchContextScope] = None
    stream: bool = True
    include_operation: bool = False
    include_debug: bool = False
    datasource_id: Optional[str] = Field(
        None,
        description="数据源标识（如 oltp_primary / warehouse_odps），由调用方传入。"
                    "CM 据此路由 SQL 执行后端并隔离图谱检索范围。"
                    "未传时从 session 继承或按 scope.domain 推断。",
    )
    relevance_threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="相关度门槛。低于此值时 relevance.status 变为 low_confidence 或 no_match。"
                    "不传时使用服务端默认值 (0.40)。",
    )


class SearchContextResponse(BaseModel):
    session_ref: str
    path_hint: str = ""
    schema_prompt: str = ""
    primary_metrics: list[MetricFocus] = Field(default_factory=list)
    alternative_metrics: list[MetricCandidate] = Field(default_factory=list)
    knowledge_notes: list[KnowledgeNote] = Field(default_factory=list)
    similar_experiences: list[SimilarExperience] = Field(default_factory=list)
    ambiguous: bool = False
    ambiguity_candidates: list[AmbiguityCandidate] = Field(default_factory=list)
    time_hints: TimeHints = Field(default_factory=TimeHints)
    relevance: QueryRelevance = Field(default_factory=lambda: QueryRelevance(status="relevant"))
    operation: Optional[dict[str, Any]] = None
    debug: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------- #
# L2 — Context Operations
# ---------------------------------------------------------------------- #

class ExploreEntityRequest(BaseModel):
    session_ref: Optional[str] = None
    entity_name: str
    domain: Optional[str] = Field(
        None,
        description="业务域名称。未传时从 session scope 继承或通过实体名自动推断。",
    )
    datasource_id: Optional[str] = Field(
        None,
        description="数据源标识（如 oltp_primary / warehouse_odps）。未传时从 session 继承或按 domain 推断默认值。",
    )
    relevance_threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="相关度门槛。低于此值时 relevance.status 变为 no_match 并返回 404。"
                    "不传时使用服务端默认值 (0.40)。",
    )


class ExploreEntityHit(BaseModel):
    session_ref: str
    entity_type: str
    name: str
    domain: str = ""
    match_confidence: float = 0.0
    summary: str = ""
    usage_guidance: str = ""
    definition: str = ""
    source_columns: list[SourceColumn] = Field(default_factory=list)
    drill_dimensions: list[DrillDimension] = Field(default_factory=list)
    common_filters: list[CommonFilter] = Field(default_factory=list)
    related_metrics_nl: list[str] = Field(default_factory=list)
    related_events: list[EventCard] = Field(default_factory=list)
    knowledge_notes: list[KnowledgeNote] = Field(default_factory=list)
    experience_hints: list[str] = Field(default_factory=list)
    relevance: QueryRelevance = Field(default_factory=lambda: QueryRelevance(status="relevant"))


class ExploreEntityAmbiguous(BaseModel):
    session_ref: str
    ambiguous: bool = True
    ambiguity_candidates: list[AmbiguityCandidate] = Field(default_factory=list)
    hint: str = ""
    relevance: QueryRelevance = Field(default_factory=lambda: QueryRelevance(status="low_confidence"))


ExploreEntityResponse = Union[ExploreEntityHit, ExploreEntityAmbiguous]


class SearchEventRequest(BaseModel):
    query: str
    limit: int = Field(10, ge=1, le=50, description="返回条数，默认 10")
    relevance_threshold: Optional[float] = Field(
        None, ge=0.0, le=1.0,
        description="相关度门槛。低于此值时 relevance.status 变为 no_match 并清空结果。"
                    "不传时使用服务端默认值 (0.40)。",
    )


class EventSearchHit(BaseModel):
    key: str
    name: str = ""
    type: str = ""
    scope: str = ""
    description: str = ""
    date_from: str = ""
    date_to: str = ""
    score: float = 0.0
    about_entity_key: str = ""
    about_entity_name: str = ""


class SearchEventResponse(BaseModel):
    query: str
    events: list[EventCard] = Field(default_factory=list)
    relevance: QueryRelevance = Field(default_factory=lambda: QueryRelevance(status="relevant"))


class ExecuteSqlRequest(BaseModel):
    session_ref: Optional[str] = None
    sql: str
    datasource_id: Optional[str] = Field(
        None,
        description="数据源标识（如 oltp_primary / warehouse_odps）。"
                    "CM 据此选择 SQL 执行后端；未传时从 session 继承或按 domain/SQL 表名自动判断。",
    )
    max_rows: int = Field(
        2000, ge=1, le=10000,
        description="最多返回的行数上限。查询结果超过此值时 truncated=true，"
                    "超出部分通过 download_url 下载完整 CSV",
    )
    slow_ms_threshold: float = Field(8000.0, ge=500.0, le=600_000.0)


class ExecuteSqlResponse(BaseModel):
    session_ref: str
    exec_status: Literal["success", "error", "empty", "slow"]
    sql: str
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(
        default_factory=list,
        description="仅包含前 20 行预览数据。完整结果请始终通过 download_url 下载 CSV 文件",
    )
    preview_row_count: int = Field(0, description="rows 中实际包含的预览行数（最多 20 行）")
    truncated: bool = Field(
        False,
        description="**重要标识符**：true 表示查询结果超过 max_rows（默认 2000 行），"
                    "数据被截断，即使通过 download_url 下载也无法获取完整结果。"
                    "模型应当：(1) 添加更精确的 WHERE 条件缩小范围；"
                    "(2) 使用 LIMIT/OFFSET 分页查询；"
                    "(3) 使用聚合函数减少返回行数",
    )
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    download_url: Optional[str] = Field(
        None,
        description="完整查询结果的 CSV 下载链接（所有非空结果均会生成此链接）。"
                    "注意：当 truncated=true 时，下载文件也仅包含 max_rows 行",
    )
    total_row_count: Optional[int] = Field(
        None,
        description="查询命中的真实总行数。当 truncated=true 时此值大于 max_rows，"
                    "rows 和 download_url 中仅包含前 max_rows 行",
    )
    expires_in_seconds: Optional[int] = Field(
        None,
        description="download_url 有效剩余秒数；无下载链接时为 null",
    )


class RecallExperienceFocus(BaseModel):
    task_hint: Optional[str] = None
    avoid_only: bool = False


class RecallExperienceRequest(BaseModel):
    session_ref: Optional[str] = None
    focus: Optional[RecallExperienceFocus] = None
    datasource_id: Optional[str] = Field(
        None,
        description="数据源标识（如 oltp_primary / warehouse_odps）。未传时从 session 继承或按 domain 推断默认值。",
    )


class ExperienceCardBrief(BaseModel):
    polarity: str
    lesson: str = ""
    confidence: float = 0.0


class RecallExperienceStats(BaseModel):
    card_count: int = 0
    top_score: float = 0.0


class RecallExperienceResponse(BaseModel):
    session_ref: str
    guidance_summary: str = ""
    do_hints: list[str] = Field(default_factory=list)
    avoid_hints: list[str] = Field(default_factory=list)
    cards: list[ExperienceCardBrief] = Field(default_factory=list)
    stats: RecallExperienceStats = Field(default_factory=RecallExperienceStats)


# ---------------------------------------------------------------------- #
# L3 — Entity Lookup
# ---------------------------------------------------------------------- #

class Domain(BaseModel):
    name: str
    display_name: str = ""
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    datasource_id: str = ""


class MetricSummary(BaseModel):
    metric_name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    role: str = ""


class MetricDimensionBinding(BaseModel):
    dimension_name: str
    is_display_dimension: bool = True
    is_contribution_dimension: bool = True


class MetricFormula(BaseModel):
    dataset: str = ""
    formula: str = ""
    formula_evidence: str = ""
    date_range: str = ""


class RelatedMetric(BaseModel):
    name: str
    description: str = ""


class RelatedKnowledge(BaseModel):
    entity_type: str
    name: str
    summary: str = ""


class MetricDetail(BaseModel):
    metric_name: str
    domain: str
    description: str = ""
    unit: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    role: str = ""
    formula_semantic: str = ""
    formulas: list[MetricFormula] = Field(default_factory=list)
    anomaly_rules: list[dict[str, Any]] = Field(default_factory=list)
    dimensions: list[MetricDimensionBinding] = Field(default_factory=list)
    source_columns: list[SourceColumn] = Field(default_factory=list)
    common_filters: list[CommonFilter] = Field(default_factory=list)
    related_metrics: list[RelatedMetric] = Field(default_factory=list)
    related_knowledge: list[RelatedKnowledge] = Field(default_factory=list)


class DimensionSummary(BaseModel):
    dimension_name: str
    aliases: list[str] = Field(default_factory=list)
    dimension_type: str = ""
    is_display_dimension: bool = True


class DimensionValue(BaseModel):
    value: str
    business_meaning: str = ""
    frequency: float = 0.0
    is_rollup_sentinel: bool = False


class DimensionDetail(BaseModel):
    dimension_name: str
    domain: str
    dataset_name: str = ""
    calculate_expr: str = ""
    dimension_type: str = ""
    data_type: str = "text"
    aliases: list[str] = Field(default_factory=list)
    parent_dimension: str = ""
    hierarchy_level: int = 0
    is_display_dimension: bool = True
    is_contribution_dimension: bool = True
    sample_values: list[DimensionValue] = Field(default_factory=list)
    sample_values_total: Optional[int] = None
    related_knowledge: list[RelatedKnowledge] = Field(default_factory=list)


class DimensionHierarchy(BaseModel):
    dimension_name: str
    parent: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)


class DatasetSummary(BaseModel):
    dataset_name: str
    description: str = ""
    dataset_type: str = "OLAP"


class DatasetListItem(BaseModel):
    dataset_name: str
    domain: str
    description: str = ""
    dataset_type: str = "OLAP"


class ColumnMeta(BaseModel):
    column_name: str
    column_type: str = ""
    data_type: str = ""
    description: str = ""
    granularity_role: str = ""
    topline_value: str = ""
    sample_values: list[str] = Field(default_factory=list)
    sample_values_total: Optional[int] = None
    composite: bool = False
    composite_desc: str = ""


class DatasetSchema(BaseModel):
    dataset_name: str
    domain: str
    description: str = ""
    dataset_type: str = "OLAP"
    columns: list[ColumnMeta] = Field(default_factory=list)


class DomainOverview(BaseModel):
    domain: Domain
    north_star_metrics: list[MetricSummary] = Field(default_factory=list)
    metric_count: int = 0
    dimension_count: int = 0
    dataset_count: int = 0
    top_dimensions: list[str] = Field(default_factory=list)
    datasets: list[DatasetSummary] = Field(default_factory=list)


class MetricDimensionsResponse(BaseModel):
    metric_name: str
    domain: str
    dimensions: list[MetricDimensionBinding] = Field(default_factory=list)


class DimensionMetricsResponse(BaseModel):
    dimension_name: str
    domain: str
    metrics: list[MetricSummary] = Field(default_factory=list)

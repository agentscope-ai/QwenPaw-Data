"""Pydantic models for the Semantic Layer REST API."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


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


class MetricNorthStarSummary(BaseModel):
    """§2.5 — same as MetricSummary but without ``role``."""

    metric_name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class MetricFormula(BaseModel):
    dataset: str = ""
    formula: str = ""
    formula_evidence: str = ""
    date_range: str = ""


class MetricDimensionBinding(BaseModel):
    dimension_name: str
    is_display_dimension: bool = True
    is_contribution_dimension: bool = True


class MetricDetail(MetricSummary):
    domain: str = ""
    description: str = ""
    unit: str = ""
    formulas: list[MetricFormula] = Field(default_factory=list)
    anomaly_rules: list[dict[str, Any]] = Field(default_factory=list)
    dimensions: list[MetricDimensionBinding] = Field(default_factory=list)


class Dimension(BaseModel):
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


class DimensionHierarchy(BaseModel):
    dimension_name: str
    parent: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)


class Dataset(BaseModel):
    dataset_name: str
    domain: str
    description: str = ""
    dataset_type: str = "OLAP"
    sql: str = ""
    parents: str = ""


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
    table_name: str = ""
    columns: list[ColumnMeta] = Field(default_factory=list)


class SearchMetricsMiss(BaseModel):
    items: list[Any] = Field(default_factory=list)
    message: str

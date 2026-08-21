from __future__ import annotations

from pydantic import BaseModel


class MetricCreate(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    metric_name: str | None = None
    description: str | None = None
    unit: str | None = None
    is_polaris: bool | None = False
    show_distribution: bool | None = False
    is_visible: bool | None = True
    synonyms: str | None = None
    tags: str | None = None


class MetricUpdate(BaseModel):
    metric_name: str | None = None
    description: str | None = None
    unit: str | None = None
    is_polaris: bool | None = None
    show_distribution: bool | None = None
    is_visible: bool | None = None
    synonyms: str | None = None
    tags: str | None = None


class MetricResponse(BaseModel):
    id: int
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    metric_name: str | None = None
    description: str | None = None
    unit: str | None = None
    is_polaris: bool | None = None
    show_distribution: bool | None = None
    is_visible: bool | None = None
    synonyms: str | None = None
    tags: str | None = None

from __future__ import annotations

from pydantic import BaseModel


class DatasetDimensionCreate(BaseModel):
    dataset_id: int | None = None
    dimension_id: int | None = None
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    calculate_expr: str | None = None
    dimension_type: str | None = None
    data_type: str | None = None


class DatasetDimensionUpdate(BaseModel):
    calculate_expr: str | None = None
    dimension_type: str | None = None
    data_type: str | None = None


class DatasetDimensionResponse(BaseModel):
    id: int
    dataset_id: int | None = None
    dataset_name: str | None = None
    dimension_id: int | None = None
    dimension_name: str | None = None
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    calculate_expr: str | None = None
    dimension_type: str | None = None
    data_type: str | None = None

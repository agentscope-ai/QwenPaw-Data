from __future__ import annotations

from pydantic import BaseModel


class DatasetDimensionCreate(BaseModel):
    datasource_id: str | None = None
    domain_id: int | None = None
    dataset_id: int | None = None
    dimension_id: int | None = None
    expr: str | None = None
    value_example: str | None = None


class DatasetDimensionUpdate(BaseModel):
    expr: str | None = None
    value_example: str | None = None


class DatasetDimensionResponse(BaseModel):
    id: int
    datasource_id: str | None = None
    domain_id: int | None = None
    dataset_id: int | None = None
    dimension_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    dataset_name: str | None = None
    dimension_name: str | None = None
    expr: str | None = None
    value_example: str | None = None

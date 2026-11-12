from __future__ import annotations

from pydantic import BaseModel


class CaliberCreate(BaseModel):
    datasource_id: str | None = None
    domain_id: int | None = None
    metric_id: int | None = None
    dataset_id: int | None = None
    expr: str | None = None
    time_window: str | None = None
    additivity: str | None = None
    is_default: int | None = None


class CaliberUpdate(BaseModel):
    expr: str | None = None
    time_window: str | None = None
    additivity: str | None = None
    is_default: int | None = None


class CaliberResponse(BaseModel):
    id: int
    datasource_id: str | None = None
    domain_id: int | None = None
    metric_id: int | None = None
    dataset_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    metric_name: str | None = None
    dataset_name: str | None = None
    expr: str | None = None
    time_window: str | None = None
    additivity: str | None = None
    is_default: int | None = None

from __future__ import annotations

from pydantic import BaseModel


class MetricCreate(BaseModel):
    domain_id: int | None = None
    metric_name: str | None = None
    description: str | None = None
    synonyms: str | None = None
    priority: str | None = None  # HIGH / MEDIUM / LOW


class MetricUpdate(BaseModel):
    metric_name: str | None = None
    description: str | None = None
    synonyms: str | None = None
    priority: str | None = None


class MetricResponse(BaseModel):
    metric_id: int
    domain_id: int | None = None
    domain_name: str | None = None
    metric_name: str | None = None
    description: str | None = None
    synonyms: str | None = None
    priority: str | None = None

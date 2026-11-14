from __future__ import annotations

from pydantic import BaseModel


class DimensionCreate(BaseModel):
    domain_id: int | None = None
    dimension_name: str | None = None
    description: str | None = None
    synonyms: str | None = None
    dimension_type: str | None = None
    child_dimensions: str | None = None
    priority: str | None = None  # HIGH / MEDIUM / LOW


class DimensionUpdate(BaseModel):
    dimension_name: str | None = None
    description: str | None = None
    synonyms: str | None = None
    dimension_type: str | None = None
    child_dimensions: str | None = None
    priority: str | None = None


class DimensionResponse(BaseModel):
    dimension_id: int
    domain_id: int | None = None
    domain_name: str | None = None
    dimension_name: str | None = None
    description: str | None = None
    synonyms: str | None = None
    dimension_type: str | None = None
    child_dimensions: str | None = None
    priority: str | None = None

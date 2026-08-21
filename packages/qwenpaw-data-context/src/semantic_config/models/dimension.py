from __future__ import annotations

from pydantic import BaseModel


class DimensionCreate(BaseModel):
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    dimension_name: str | None = None
    description: str | None = None
    parent_name: str | None = None
    depth: int | None = None
    synonyms: str | None = None
    is_visible: bool | None = True
    is_attribution: bool | None = True
    enums: str | None = None


class DimensionUpdate(BaseModel):
    dimension_name: str | None = None
    description: str | None = None
    parent_name: str | None = None
    depth: int | None = None
    synonyms: str | None = None
    is_visible: bool | None = None
    is_attribution: bool | None = None
    enums: str | None = None


class DimensionResponse(BaseModel):
    id: int
    datasource_id: str | None = None  # 数据源对外编码
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    dimension_name: str | None = None
    description: str | None = None
    parent_name: str | None = None
    depth: int | None = None
    synonyms: str | None = None
    is_visible: bool | None = None
    is_attribution: bool | None = None
    enums: str | None = None

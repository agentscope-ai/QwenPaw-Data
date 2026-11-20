from __future__ import annotations

from pydantic import BaseModel


class DatasetFieldCreate(BaseModel):
    datasource_id: str | None = None
    domain_id: int | None = None
    dataset_id: int | None = None
    field_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None


class DatasetFieldUpdate(BaseModel):
    field_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None


class DatasetFieldResponse(BaseModel):
    id: int
    datasource_id: str | None = None
    domain_id: int | None = None
    dataset_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    dataset_name: str | None = None
    field_name: str | None = None
    description: str | None = None
    data_type: str | None = None
    is_primary: str | None = None
    is_nullable: str | None = None

from __future__ import annotations

from pydantic import BaseModel


class DatasetMetaCreate(BaseModel):
    #: Datasource identifier shared with the canonical metadata schema.
    datasource_id: str | None = None
    domain_id: int | None = None
    dataset_name: str | None = None
    description: str | None = None
    source: str | None = None


class DatasetMetaUpdate(BaseModel):
    dataset_name: str | None = None
    description: str | None = None
    source: str | None = None


class DatasetMetaResponse(BaseModel):
    dataset_id: int
    datasource_id: str | None = None
    domain_id: int | None = None
    datasource_name: str | None = None
    domain_name: str | None = None
    dataset_name: str | None = None
    description: str | None = None
    source: str | None = None

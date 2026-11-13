from __future__ import annotations

from pydantic import AliasChoices, BaseModel, Field, computed_field


class DatasetMetaCreate(BaseModel):
    #: Datasource identifier shared with the canonical metadata schema.
    datasource_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("datasource_id", "source_datasource"),
    )
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

    @computed_field(json_schema_extra={"deprecated": True})
    @property
    def source_datasource(self) -> str | None:
        """Mirror the datasource identifier for older dataset editors."""
        return self.datasource_id

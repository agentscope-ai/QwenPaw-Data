"""Define the validated SPEC 01 semantic snapshot input contract."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..utils.validation import require_trimmed_text as validate_trimmed_text


class _ImportModel(BaseModel):
    """Keep bridge values immutable and reject unadapted field drift."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class DatasourcePayload(_ImportModel):
    """Carry datasource identity and display metadata, without connection config."""

    datasource_name: str
    datasource_type: str
    description: str = ""

    @field_validator("datasource_name", "datasource_type")
    @classmethod
    def require_trimmed_text(cls, value: str) -> str:
        """Reject missing or whitespace-normalized datasource metadata."""
        return validate_trimmed_text(value, "datasource metadata")


class TableColumnPayload(_ImportModel):
    """Describe one physical column from the SPEC 01 table inventory."""

    name: str
    comment: str = ""
    data_type: str = ""
    is_primary: bool = False
    is_nullable: bool = True
    sample_values: list[str] = Field(default_factory=list)


class TablePayload(_ImportModel):
    """Describe one physical table by its two-part qualified name."""

    name: str
    comment: str = ""
    partition_keys: list[str] = Field(default_factory=list)
    columns: list[TableColumnPayload] = Field(default_factory=list)


class PhysicalJoinPayload(_ImportModel):
    """Describe one physical join between two tables of the same datasource."""

    left_table: str
    left_columns: list[str] = Field(min_length=1)
    right_table: str
    right_columns: list[str] = Field(min_length=1)
    join_type: str = ""


class DatasetFieldPayload(_ImportModel):
    """Describe one logical dataset field from ``dataset_column``."""

    name: str
    description: str = ""
    data_type: str = ""
    is_primary: bool = False
    is_nullable: bool = True
    sample_values: list[str] = Field(default_factory=list)


class DatasetPayload(_ImportModel):
    """Describe one semantic dataset and its logical fields."""

    name: str
    description: str = ""
    source: str = ""
    dataset_fields: list[DatasetFieldPayload] = Field(default_factory=list)


class DimensionBindingPayload(_ImportModel):
    """Bind one dimension expression to one semantic dataset."""

    dataset: str
    expr: str
    value_example: list[str] = Field(default_factory=list)


class DimensionPayload(_ImportModel):
    """Describe one semantic dimension."""

    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    dimension_type: str = ""
    child_dimensions: list[str] = Field(default_factory=list)
    priority: str = "MEDIUM"
    dimension_bindings: list[DimensionBindingPayload] = Field(default_factory=list)


class MetricCaliberPayload(_ImportModel):
    """Describe one SPEC 01 metric formula bound to a dataset."""

    dataset: str
    expr: str
    time_window: str = ""
    additivity: str = ""
    is_default: bool | None = None


class MetricPayload(_ImportModel):
    """Describe one semantic metric and its calibers."""

    name: str
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    priority: str = "MEDIUM"
    metric_calibers: list[MetricCaliberPayload] = Field(default_factory=list)


class VerifiedQueryPayload(_ImportModel):
    """Describe one domain-anchored verified question and SQL pair."""

    question: str
    verified_sql: str
    source_channel: str = ""


class DomainPayload(_ImportModel):
    """Describe one domain-rooted semantic aggregate.

    ``domain_id`` 是身份（图 key 的 domain 段取值）；缺省回落 ``name``。
    ``name`` 是显示名，可随展示需要变更。
    """

    name: str
    domain_id: str | None = None
    description: str = ""
    datasource_scope: list[str] = Field(default_factory=list)
    datasets: list[DatasetPayload] = Field(default_factory=list)
    dimensions: list[DimensionPayload] = Field(default_factory=list)
    metrics: list[MetricPayload] = Field(default_factory=list)
    verified_queries: list[VerifiedQueryPayload] = Field(default_factory=list)


class SemanticPayload(_ImportModel):
    """Hold the domain aggregates in one committed semantic snapshot."""

    domains: list[DomainPayload] = Field(default_factory=list)


class SemanticImportRequest(_ImportModel):
    """Validate one datasource-scoped semantic snapshot."""

    datasource_id: str
    datasource: DatasourcePayload | None = None
    tables: list[TablePayload] = Field(default_factory=list)
    physical_joins: list[PhysicalJoinPayload] = Field(default_factory=list)
    semantic: SemanticPayload = Field(default_factory=SemanticPayload)
    drop_semantic_first: bool = False
    task_id: str | None = None

    @model_validator(mode="after")
    def validate_datasource_id(self) -> SemanticImportRequest:
        """Reject an absent or whitespace-only datasource route."""
        object.__setattr__(
            self,
            "datasource_id",
            validate_trimmed_text(self.datasource_id, "datasource_id"),
        )
        return self


__all__ = [
    "DatasetPayload",
    "DatasetFieldPayload",
    "DatasourcePayload",
    "DimensionBindingPayload",
    "DimensionPayload",
    "DomainPayload",
    "MetricCaliberPayload",
    "MetricPayload",
    "PhysicalJoinPayload",
    "SemanticImportRequest",
    "SemanticPayload",
    "TableColumnPayload",
    "TablePayload",
    "VerifiedQueryPayload",
]

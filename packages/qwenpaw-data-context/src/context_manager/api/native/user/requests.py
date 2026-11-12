"""Validate the HTTP inputs for user-authored profile and memory edits."""

from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from ....utils.values import normalize_text

MemoryType = Literal["principle", "preference", "anchored_rule", "trigger"]
MemoryStatus = Literal["active", "needs_review", "revoked"]
Description = Annotated[
    str,
    AfterValidator(normalize_text),
    StringConstraints(strip_whitespace=True, min_length=8, max_length=500),
]
Trigger = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)
]


class ProfilePatch(BaseModel):
    """Omitted fields are preserved; explicit null clears a profile field."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    display_name: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=1000)
    locale: str | None = Field(default=None, max_length=32)
    timezone: str | None = Field(default=None, max_length=64)

    @field_validator("*")
    @classmethod
    def normalize_value(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = normalize_text(value)
        if not value:
            raise ValueError("profile fields must not be blank")
        return value

    @model_validator(mode="after")
    def require_change(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("profile patch must contain a change")
        return self


class MemoryReference(BaseModel):
    """Identify an existing MG/KG node by canonical label and Store key."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    graph: Literal["metadata", "knowledge"]
    label: Literal["Metric", "Dimension", "Column", "Entity", "Event"]
    key: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def graph_matches_label(self) -> Self:
        graph = "knowledge" if self.label in {"Entity", "Event"} else "metadata"
        if self.graph != graph:
            raise ValueError("reference graph does not match its label")
        return self


class MemoryCreate(BaseModel):
    """A complete user-authored memory; no extraction or inferred provenance."""

    model_config = ConfigDict(extra="forbid")

    type: MemoryType
    description: Description
    triggers: list[Trigger] = Field(default_factory=list, max_length=10)
    references: list[MemoryReference] = Field(default_factory=list, max_length=20)


class MemoryPatch(BaseModel):
    """Validate supplied fields; the service validates the resulting full memory."""

    model_config = ConfigDict(extra="forbid")

    type: MemoryType | None = None
    description: Description | None = None
    triggers: list[Trigger] | None = Field(default=None, max_length=10)
    references: list[MemoryReference] | None = Field(default=None, max_length=20)

    @model_validator(mode="after")
    def supplied_values_are_not_null(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("memory patch must contain a change")
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError("memory fields cannot be null")
        return self

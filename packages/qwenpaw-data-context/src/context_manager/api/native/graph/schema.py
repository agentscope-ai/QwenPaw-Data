"""Expose the canonical logical GraphSchema as a typed HTTP document."""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict

from ....schema import (
    GRAPH_SCHEMA,
    CrudOperation,
    DisplayPolicy,
    KeyStrategy,
    PropertyOwner,
    ValueKind,
    Zone,
)
from ...dependencies import AuthorizationDependency


class SchemaPropertyResponse(BaseModel):
    """Describe one canonical logical property without a runtime object reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    kind: ValueKind
    editable: bool
    display_policy: DisplayPolicy
    nullable: bool
    owner: PropertyOwner


class SchemaKeyPropertyResponse(SchemaPropertyResponse):
    """Describe the generation rule carried by the specialized key property."""

    strategy: KeyStrategy
    prefix: str
    parts: list[str]


class SchemaNodeResponse(BaseModel):
    """Describe one canonical node declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    zone: Zone
    operations: list[CrudOperation]
    fulltext_fields: list[str]
    embedding_fields: list[str]
    properties: list[SchemaKeyPropertyResponse | SchemaPropertyResponse]


class SchemaEdgeResponse(BaseModel):
    """Describe one endpoint-qualified canonical edge declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    relationship: str
    target: str
    operations: list[CrudOperation]
    properties: list[SchemaPropertyResponse]


class GetSchemaResponse(BaseModel):
    """Return the complete backend-neutral logical schema snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    zones: list[Zone]
    nodes: list[SchemaNodeResponse]
    edges: list[SchemaEdgeResponse]


router = APIRouter()


@router.get("/get_schema", response_model=GetSchemaResponse)
def get_schema(
    _context: AuthorizationDependency,
) -> GetSchemaResponse:
    """Expose business declarations without persistence-only bookkeeping nodes."""
    document = GRAPH_SCHEMA.to_document()
    document["zones"] = [
        zone for zone in document["zones"] if zone != Zone.INTERNAL.value
    ]
    document["nodes"] = [
        {name: value for name, value in node.items() if name != "internal"}
        for node in document["nodes"]
        if not node["internal"]
    ]
    return GetSchemaResponse.model_validate(document)


__all__ = [
    "GetSchemaResponse",
    "SchemaEdgeResponse",
    "SchemaKeyPropertyResponse",
    "SchemaNodeResponse",
    "SchemaPropertyResponse",
    "get_schema",
    "router",
]

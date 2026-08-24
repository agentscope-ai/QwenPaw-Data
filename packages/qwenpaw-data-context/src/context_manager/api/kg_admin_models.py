"""Pydantic request/response models for the KG CRUD API."""
from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


def _parse_string_list(v: Any) -> list[str]:
    """Accept both a real list and common string representations of a list."""
    if isinstance(v, list):
        return v
    if not isinstance(v, str):
        return [str(v)]
    s = v.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [t.strip().strip("'\"") for t in re.split(r"[,，]", s) if t.strip()]


# ─── Entity ─────────────────────────────────────────────────────────── #

class EntityUpsertRequest(BaseModel):
    """Create or update a KG entity node."""
    canonical_name: str = ""
    type: str = ""
    aliases: list[str] = Field(default_factory=list)
    description: str = ""
    lifecycle_state: str = ""

    @field_validator("aliases", mode="before")
    @classmethod
    def coerce_aliases(cls, v: Any) -> list[str]:
        return _parse_string_list(v)


# ─── Event ──────────────────────────────────────────────────────────── #

class EventUpsertRequest(BaseModel):
    """Create or update a KG event node."""
    name: str
    type: str = ""
    description: str = ""
    date_from: str = ""
    date_to: str = ""
    scope: str = "_global"
    zone: str = "knowledge"
    source_id: str = "kg_admin:ui"
    source_trust: float = 0.95
    extractor: str = "manual"


# ─── Edge: RELATED_TO ───────────────────────────────────────────────── #

class RelatedToRequest(BaseModel):
    """Create or update a RELATED_TO edge between two nodes."""
    from_key: str
    to_key: str
    relation_subtype: str = "see_also"
    description: str = ""


class RelatedToDeleteRequest(BaseModel):
    """Remove a RELATED_TO edge between two nodes."""
    from_key: str
    to_key: str


# ─── Edge: ABOUT ────────────────────────────────────────────────────── #

class AboutRequest(BaseModel):
    """Connect or disconnect an event from an entity via ABOUT edge."""
    event_key: str
    entity_key: str
    connect: bool = True


# ─── Edge: Cross-graph ──────────────────────────────────────────────── #

class CrossGraphEdgeRequest(BaseModel):
    """Create an edge across different graph layers."""
    from_key: str
    to_key: str
    rel_type: str
    properties: dict[str, Any] = Field(default_factory=dict)


class CrossGraphEdgeDeleteRequest(BaseModel):
    """Remove a cross-graph edge by endpoints and type."""
    from_key: str
    to_key: str
    rel_type: str


# ─── Edge: generic delete ───────────────────────────────────────────── #

class AdjacentEdgeDeleteRequest(BaseModel):
    """Delete a single edge between two nodes in a given direction."""
    anchor_key: str
    other_key: str
    rel_type: str
    direction: Literal["in", "out"] = "out"


class EdgeDeleteByTypeRequest(BaseModel):
    """Bulk-delete all edges of a given type from a node."""
    anchor_key: str
    rel_type: str
    direction_scope: Literal["both", "out", "in"] = "both"


# ─── Edge: property update ──────────────────────────────────────────── #

class EdgePropertiesUpdateRequest(BaseModel):
    """Update properties on an existing edge."""
    from_key: str
    to_key: str
    rel_type: str
    properties: dict[str, Any]


# ─── Batch ───────────────────────────────────────────────────────────── #

class BatchDeleteRequest(BaseModel):
    """Delete multiple nodes by key in one call."""
    keys: list[str] = Field(default_factory=list)


# ─── Edge: global purge ──────────────────────────────────────────────── #

class GlobalEdgePurgeRequest(BaseModel):
    """Delete all edges of a given type touching any Entity/Event node."""
    rel_type: str

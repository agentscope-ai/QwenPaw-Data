"""Forward legacy KG property updates to canonical graph patch handlers."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
)

from ..dependencies import (
    AuthorizationDependency,
    RuntimeDependency,
)
from ..native.graph.edge import (
    EdgePatch,
    PatchEdgesRequest,
    patch_edges,
)
from ..native.graph.nodes import (
    NodePatch,
    PatchNodesRequest,
    patch_nodes,
)

# TODO(cm-api-compat): remove these write forwarders after the graph frontend
# calls /api/v1/graph/patch_nodes and /api/v1/graph/patch_edges directly.

LegacyGraphKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]


def _parse_string_list(value: Any) -> list[str] | None:
    """Accept the list and comma-delimited alias forms used by the old UI."""
    if value is None:
        return None
    if isinstance(value, list):
        return value
    if not isinstance(value, str):
        return [str(value)]
    normalized = value.strip()
    if normalized.startswith("[") and normalized.endswith("]"):
        normalized = normalized[1:-1]
    return [
        item.strip().strip("'\"")
        for item in re.split(r"[,，]", normalized)
        if item.strip()
    ]


class LegacyEntityPatchRequest(BaseModel):
    """Accept the old Entity update fields used by the graph frontend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    canonical_name: str
    type: str
    aliases: list[str] | None = None
    description: str | None = None
    lifecycle_state: str | None = None

    @field_validator("aliases", mode="before")
    @classmethod
    def coerce_aliases(cls, value: Any) -> list[str] | None:
        """Preserve the legacy aliases coercion at the protocol boundary."""
        return _parse_string_list(value)


class LegacyEventPatchRequest(BaseModel):
    """Accept the old Event update fields used by the graph frontend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    type: str
    description: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    scope: str | None = None
    zone: str | None = None
    source_id: str | None = None
    source_trust: float | None = None
    extractor: str | None = None


class LegacyEdgePropertiesPatchRequest(BaseModel):
    """Accept the old endpoint-key edge property update contract."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_key: LegacyGraphKey
    to_key: LegacyGraphKey
    rel_type: str
    properties: dict[str, JsonValue] = Field(min_length=1)


class LegacyKgSuccessResponse(BaseModel):
    """Preserve the standard legacy KG success envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ok: Literal[True] = True
    data: dict[str, Any]
    error: None = None
    meta: None = None


router = APIRouter(prefix="/api/v1/admin/kg", tags=["legacy-kg"])


def _deprecate(response: Response, successor: str) -> None:
    """Mark a compatibility response and identify its canonical successor."""
    response.headers["Deprecation"] = "true"
    response.headers["Link"] = f'<{successor}>; rel="successor-version"'


def _legacy_endpoint(key: str) -> str:
    """Map established Knowledge key prefixes to canonical endpoint types."""
    prefix = key.partition(":")[0].lower()
    if prefix == "ent":
        return "Entity"
    if prefix in {"ev", "evt"}:
        return "Event"
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail="legacy edge endpoint key must start with ent:, ev:, or evt:",
    )


@router.put("/entities/{key:path}", response_model=LegacyKgSuccessResponse)
async def legacy_patch_entity(
    key: LegacyGraphKey,
    payload: LegacyEntityPatchRequest,
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> LegacyKgSuccessResponse:
    """Translate the old Entity update into one canonical node patch."""
    changes: dict[str, JsonValue] = {
        "name": payload.canonical_name,
        "canonical_name": payload.canonical_name,
        "type": payload.type,
    }
    optional = payload.model_dump(
        include={"aliases", "description", "lifecycle_state"},
        exclude_none=True,
    )
    changes.update(optional)
    await patch_nodes(
        PatchNodesRequest(
            node="Entity",
            patches=[NodePatch(key=key, changes=changes)],
        ),
        runtime,
        context,
    )
    _deprecate(response, "/api/v1/graph/patch_nodes")
    return LegacyKgSuccessResponse(data={"ok": True, "key": key})


@router.put("/events/{key:path}", response_model=LegacyKgSuccessResponse)
async def legacy_patch_event(
    key: LegacyGraphKey,
    payload: LegacyEventPatchRequest,
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> LegacyKgSuccessResponse:
    """Translate the old Event update into one canonical node patch."""
    if payload.zone not in {None, "knowledge"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Event zone must be knowledge",
        )
    changes = payload.model_dump(exclude={"zone"}, exclude_none=True)
    await patch_nodes(
        PatchNodesRequest(
            node="Event",
            patches=[NodePatch(key=key, changes=changes)],
        ),
        runtime,
        context,
    )
    _deprecate(response, "/api/v1/graph/patch_nodes")
    return LegacyKgSuccessResponse(
        data={"ok": True, "key": key, "decision": "accepted"}
    )


@router.patch("/edges/properties", response_model=LegacyKgSuccessResponse)
async def legacy_patch_edge_properties(
    payload: LegacyEdgePropertiesPatchRequest,
    response: Response,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> LegacyKgSuccessResponse:
    """Translate endpoint keys into an exact canonical edge patch."""
    await patch_edges(
        PatchEdgesRequest(
            relationship=payload.rel_type,
            source=_legacy_endpoint(payload.from_key),
            target=_legacy_endpoint(payload.to_key),
            patches=[
                EdgePatch(
                    source_key=payload.from_key,
                    target_key=payload.to_key,
                    changes=payload.properties,
                )
            ],
        ),
        runtime,
        context,
    )
    _deprecate(response, "/api/v1/graph/patch_edges")
    return LegacyKgSuccessResponse(
        data={
            "ok": True,
            "from_key": payload.from_key,
            "to_key": payload.to_key,
            "rel_type": payload.rel_type,
            "updated_properties": dict(payload.properties),
        }
    )


__all__ = [
    "LegacyEdgePropertiesPatchRequest",
    "LegacyEntityPatchRequest",
    "LegacyEventPatchRequest",
    "LegacyKgSuccessResponse",
    "legacy_patch_edge_properties",
    "legacy_patch_entity",
    "legacy_patch_event",
    "router",
]

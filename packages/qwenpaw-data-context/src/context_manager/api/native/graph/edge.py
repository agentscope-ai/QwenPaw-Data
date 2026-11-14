"""Expose canonical edge HTTP models and handlers."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from ....auth import WriteOwner
from ....schema import GRAPH_SCHEMA, Zone
from ....store import StoreError
from ...dependencies import AuthorizationDependency, RuntimeDependency
from .nodes import CanonicalNodeKey, SchemaNodeName

SchemaRelationshipName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    ),
]


class EdgePatch(BaseModel):
    """Identify one endpoint pair and its changes within an edge batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_key: CanonicalNodeKey
    target_key: CanonicalNodeKey
    changes: dict[str, JsonValue] = Field(min_length=1)


class PatchEdgesRequest(BaseModel):
    """Patch endpoint pairs sharing one canonical edge type."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: SchemaNodeName
    relationship: SchemaRelationshipName
    target: SchemaNodeName
    patches: list[EdgePatch]


router = APIRouter()


@router.post("/patch_edges", response_model=bool)
async def patch_edges(
    request: PatchEdgesRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> bool:
    """Patch existing edges and acknowledge the complete batch."""

    def write():
        triple = (
            request.source,
            request.relationship,
            request.target,
        )
        try:
            spec = GRAPH_SCHEMA.edge(triple)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    "unknown edge type: "
                    f"{request.source}-{request.relationship}-{request.target}"
                ),
            ) from exc
        try:
            scope = runtime.issue_graph_write_scope(
                context,
                owner=(
                    WriteOwner.USER
                    if spec.source.zone in {Zone.KNOWLEDGE, Zone.USER}
                    else WriteOwner.WORKSPACE
                ),
                zone=spec.source.zone.value,
            )
            store = runtime.graph_store(scope)

            def apply(tx):
                records = tx.get_edges(
                    spec,
                    tuple(
                        (patch.source_key, patch.target_key)
                        for patch in request.patches
                    ),
                    fields=(),
                    scope=scope,
                )
                if any(record is None for record in records):
                    raise HTTPException(404, "edge not found")
                tx.patch_edges(
                    spec,
                    tuple(
                        (patch.source_key, patch.target_key, patch.changes)
                        for patch in request.patches
                    ),
                    scope=scope,
                )

            store.atomic(apply)
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="graph write is forbidden",
            ) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=str(exc),
            ) from exc
        except StoreError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_503_SERVICE_UNAVAILABLE
                    if exc.retryable
                    else status.HTTP_500_INTERNAL_SERVER_ERROR
                ),
                detail="graph store operation failed",
            ) from exc
        return True

    try:
        return await runtime.run_background(write)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph write execution is busy") from exc


__all__ = [
    "EdgePatch",
    "PatchEdgesRequest",
    "SchemaRelationshipName",
    "patch_edges",
    "router",
]

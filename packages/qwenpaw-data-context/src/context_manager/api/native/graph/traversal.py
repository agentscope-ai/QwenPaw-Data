"""Expose direct-neighbor reads and reusable subgraph response projection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from ....schema import GRAPH_SCHEMA, DisplayPolicy
from ....store import (
    EdgeRecord,
    NeighborDirection,
    StoreError,
    SubgraphRecord,
)
from ...dependencies import AuthorizationDependency, RuntimeDependency
from .nodes import CanonicalNodeKey, NodeResponse, SchemaNodeName

_DETAIL_DISPLAY_POLICIES = frozenset({DisplayPolicy.ALWAYS, DisplayPolicy.CONDITIONAL})


class ExpandNeighborsRequest(BaseModel):
    """Select one exact seed and one finite direct-connection page."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: SchemaNodeName
    key: CanonicalNodeKey
    owner_id: str | None = Field(default=None, min_length=1)
    direction: NeighborDirection = NeighborDirection.BOTH
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class SubgraphNodeReference(BaseModel):
    """Identify one canonical edge endpoint without repeating node properties."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    key: CanonicalNodeKey


class SubgraphEdgeResponse(BaseModel):
    """Expose one canonical relationship and its endpoint references."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relationship: str
    source: SubgraphNodeReference
    target: SubgraphNodeReference
    properties: dict[str, Any]

    @classmethod
    def from_record(cls, record: EdgeRecord) -> SubgraphEdgeResponse:
        """Project one edge using canonical property display policies."""
        visible = {
            prop.name
            for prop in record.spec.properties
            if prop.display_policy in _DETAIL_DISPLAY_POLICIES
        }
        return cls(
            relationship=record.spec.relationship,
            source=SubgraphNodeReference(
                node=record.spec.source.name,
                key=record.source_key,
            ),
            target=SubgraphNodeReference(
                node=record.spec.target.name,
                key=record.target_key,
            ),
            properties={
                name: value
                for name, value in record.properties.items()
                if name in visible
            },
        )


class SubgraphResponse(BaseModel):
    """Expose one endpoint-closed bounded graph projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    nodes: list[NodeResponse]
    edges: list[SubgraphEdgeResponse]
    truncated: bool

    @classmethod
    def from_record(cls, record: SubgraphRecord) -> SubgraphResponse:
        """Project Store records without serializing canonical Python objects."""
        return cls(
            nodes=[
                NodeResponse.from_record(
                    node,
                    display_policies=_DETAIL_DISPLAY_POLICIES,
                )
                for node in record.nodes
            ],
            edges=[SubgraphEdgeResponse.from_record(edge) for edge in record.edges],
            truncated=record.truncated,
        )


router = APIRouter()


@router.post("/expand_neighbors", response_model=list[tuple[NodeResponse, str]])
async def expand_neighbors(
    request: ExpandNeighborsRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> list[tuple[NodeResponse, str]]:
    """Resolve one seed type and invoke the Store one-hop primitive once."""
    try:
        node_spec = GRAPH_SCHEMA.node(request.node)
        if node_spec.internal:
            raise ValueError("internal node")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown node type: {request.node}",
        ) from exc

    def read():
        try:
            scope = runtime.issue_graph_read_scope(context, owner_id=request.owner_id)
            store = runtime.graph_store(scope)
            result = store.expand_neighbors(
                node_spec,
                request.key,
                direction=request.direction,
                limit=request.limit,
                offset=request.offset,
                scope=scope,
            )
        except PermissionError as exc:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="graph read is forbidden",
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
        return [
            (
                NodeResponse.from_record(
                    node,
                    display_policies=_DETAIL_DISPLAY_POLICIES,
                ),
                edge.spec.relationship,
            )
            for node, edge in result
        ]

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph query execution is busy") from exc


__all__ = [
    "ExpandNeighborsRequest",
    "SubgraphEdgeResponse",
    "SubgraphNodeReference",
    "SubgraphResponse",
    "expand_neighbors",
    "router",
]

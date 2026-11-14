"""Expose canonical node HTTP models and handlers."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from ....auth import WriteOwner
from ....schema import GRAPH_SCHEMA, DisplayPolicy, Zone
from ....store import NodeRecord, StoreError
from ...dependencies import (
    AuthorizationDependency,
    RuntimeDependency,
)

_LIST_DISPLAY_POLICIES = frozenset({DisplayPolicy.ALWAYS})
_DETAIL_DISPLAY_POLICIES = frozenset({DisplayPolicy.ALWAYS, DisplayPolicy.CONDITIONAL})

SchemaNodeName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    ),
]

CanonicalNodeKey = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4096),
]


class ListNodesRequest(BaseModel):
    """Select a bounded page for one canonical Schema node name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: SchemaNodeName
    owner_id: str | None = Field(default=None, min_length=1)
    filters: dict[str, JsonValue] | None = None
    limit: int = Field(default=100, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class GetNodesRequest(BaseModel):
    """Read one node type by ordered keys, including repeated or missing keys."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: SchemaNodeName
    owner_id: str | None = Field(default=None, min_length=1)
    keys: list[CanonicalNodeKey]


class NodePatch(BaseModel):
    """Identify one patch within a node batch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: CanonicalNodeKey
    changes: dict[str, JsonValue] = Field(min_length=1)


class PatchNodesRequest(BaseModel):
    """Patch one node type in a single Store operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: SchemaNodeName
    patches: list[NodePatch]


class NodeResponse(BaseModel):
    """Expose a detached node without serializing its Python NodeSpec object."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    key: CanonicalNodeKey
    properties: dict[str, Any]

    @classmethod
    def from_record(
        cls,
        record: NodeRecord,
        *,
        display_policies: frozenset[DisplayPolicy],
    ) -> NodeResponse:
        """Project one Store record using the endpoint's display policy set."""
        visible = {
            prop.name
            for prop in record.spec.properties
            if prop.display_policy in display_policies
        }
        return cls(
            node=record.spec.name,
            key=record.key,
            properties={
                name: value
                for name, value in record.properties.items()
                if name in visible
            },
        )


router = APIRouter()


@router.post("/list_nodes", response_model=list[NodeResponse])
async def list_nodes(
    request: ListNodesRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> list[NodeResponse]:
    """Resolve one node name and invoke the Store list primitive exactly once."""
    try:
        spec = GRAPH_SCHEMA.node(request.node)
        if spec.internal:
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
            records = store.list_nodes(
                spec,
                filters=request.filters,
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
            NodeResponse.from_record(
                record,
                display_policies=_LIST_DISPLAY_POLICIES,
            )
            for record in records
        ]

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph query execution is busy") from exc


@router.post("/get_nodes", response_model=list[NodeResponse | None])
async def get_nodes(
    request: GetNodesRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> list[NodeResponse | None]:
    """Read exact keys once, retaining positions, duplicates and missing results."""
    try:
        spec = GRAPH_SCHEMA.node(request.node)
        if spec.internal:
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
            records = store.get_nodes(spec, tuple(request.keys), scope=scope)
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
            NodeResponse.from_record(record, display_policies=_DETAIL_DISPLAY_POLICIES)
            if record is not None
            else None
            for record in records
        ]

    try:
        return await runtime.run_graph_read(read)
    except BlockingIOError as exc:
        raise HTTPException(503, "Graph query execution is busy") from exc


@router.post("/patch_nodes", response_model=bool)
async def patch_nodes(
    request: PatchNodesRequest,
    runtime: RuntimeDependency,
    context: AuthorizationDependency,
) -> bool:
    """Patch existing nodes and acknowledge the complete batch."""

    def write():
        try:
            spec = GRAPH_SCHEMA.node(request.node)
            if spec.internal:
                raise ValueError("internal node")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"unknown node type: {request.node}",
            ) from exc
        try:
            owner = (
                WriteOwner.USER
                if spec.zone in {Zone.USER, Zone.KNOWLEDGE}
                else WriteOwner.WORKSPACE
            )
            scope = runtime.issue_graph_write_scope(
                context,
                owner=owner,
                zone=spec.zone.value,
            )
            store = runtime.graph_store(scope)

            def apply(tx):
                records = tx.get_nodes(
                    spec,
                    tuple(patch.key for patch in request.patches),
                    fields=(),
                    scope=scope,
                )
                if any(record is None for record in records):
                    raise HTTPException(404, "node not found")
                tx.patch_nodes(
                    spec,
                    tuple((patch.key, patch.changes) for patch in request.patches),
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
    "CanonicalNodeKey",
    "GetNodesRequest",
    "ListNodesRequest",
    "NodePatch",
    "NodeResponse",
    "PatchNodesRequest",
    "SchemaNodeName",
    "get_nodes",
    "list_nodes",
    "patch_nodes",
    "router",
]

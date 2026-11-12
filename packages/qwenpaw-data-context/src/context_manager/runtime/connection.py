"""Describe resolved graph routes without owning backend resources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..store.neo4j.target import GraphTarget
from ..store.runtime import ExecutionTarget

if TYPE_CHECKING:
    from ..store.holoage.target import HoloTarget


@dataclass(frozen=True, slots=True)
class GraphConnectionConfig(ABC):
    """Share Workspace identity across resolved backend connections."""

    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        for field_name in ("tenant_id", "workspace_id"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise RuntimeError(f"{field_name} is required and must be trimmed")

    @property
    @abstractmethod
    def target(self) -> ExecutionTarget:
        """Return the backend route bound to this Workspace."""


@dataclass(frozen=True, slots=True)
class Neo4jConnectionConfig(GraphConnectionConfig):
    """Hold a resolved Neo4j route and credentials."""

    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str = field(repr=False)
    neo4j_database: str | None = None

    def __post_init__(self) -> None:
        """Validate connection fields without exposing credentials."""
        GraphConnectionConfig.__post_init__(self)
        for field_name in ("neo4j_uri", "neo4j_user"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value or value != value.strip():
                raise RuntimeError(f"{field_name} is required and must be trimmed")
        if not isinstance(self.neo4j_password, str) or not self.neo4j_password:
            raise RuntimeError("neo4j_password is required")
        value = self.neo4j_database
        if value is None or value == "":
            object.__setattr__(self, "neo4j_database", None)
        elif not isinstance(value, str) or value != value.strip():
            raise RuntimeError("neo4j_database is required and must be trimmed")

    @property
    def target(self) -> GraphTarget:
        """Return the immutable graph route represented by this connection."""
        return GraphTarget(
            self.tenant_id,
            self.workspace_id,
            database=self.neo4j_database,
        )


@dataclass(frozen=True, slots=True)
class HoloConnectionConfig(GraphConnectionConfig):
    """Hold a registered HoloAge route; deployment Settings supply credentials."""

    graph_name: str

    def __post_init__(self) -> None:
        """Validate route fields without carrying credentials."""
        GraphConnectionConfig.__post_init__(self)
        value = self.graph_name
        if not isinstance(value, str) or not value or value != value.strip():
            raise RuntimeError("graph_name is required and must be trimmed")

    @property
    def target(self) -> HoloTarget:
        """Return the immutable AGE graph route represented by this connection."""
        from ..store.holoage.target import HoloTarget

        return HoloTarget(
            self.tenant_id,
            self.workspace_id,
            self.graph_name,
        )

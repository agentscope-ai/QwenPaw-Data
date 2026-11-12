"""Bind one Neo4j runtime to a concrete tenant and workspace route."""

from __future__ import annotations

from dataclasses import dataclass

from ...utils.validation import require_trimmed_text


@dataclass(frozen=True, slots=True)
class GraphTarget:
    """Identify the tenant/workspace database route owned by one runtime."""

    tenant_id: str
    workspace_id: str
    database: str | None = None

    def __post_init__(self) -> None:
        """Canonicalize the graph route and optional Neo4j database name."""
        object.__setattr__(
            self,
            "tenant_id",
            require_trimmed_text(self.tenant_id, "target tenant_id"),
        )
        object.__setattr__(
            self,
            "workspace_id",
            require_trimmed_text(self.workspace_id, "target workspace_id"),
        )
        if self.database is not None:
            object.__setattr__(
                self,
                "database",
                require_trimmed_text(self.database, "target database"),
            )


__all__ = ["GraphTarget"]

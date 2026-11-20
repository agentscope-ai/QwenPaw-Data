"""Bind one tenant and Workspace to its physical AGE graph."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ...utils.validation import require_trimmed_text


@dataclass(frozen=True, slots=True)
class HoloTarget:
    """Identify the tenant, workspace, and AGE graph owned by one Store."""

    tenant_id: str
    workspace_id: str
    graph_name: str

    def __post_init__(self) -> None:
        """Canonicalize the route and require a concrete graph name."""
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
        object.__setattr__(
            self,
            "graph_name",
            validate_graph_name(self.graph_name),
        )


def validate_graph_name(graph_name: str) -> str:
    """Validate quoted AGE identifiers before Hologres can truncate them."""
    name = require_trimmed_text(graph_name, "graph_name")
    if any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("graph_name must not contain control characters")
    if len(name.encode("utf-8")) > 127:
        raise ValueError("graph_name must not exceed 127 UTF-8 bytes")
    return name


def provisionable_graph_name(
    workspace_id: str,
    *,
    graph_name: str | None = None,
    reserved_graph_names: Sequence[str] = (),
) -> str:
    """Preserve the workspace identity or an explicitly registered graph name."""
    workspace = require_trimmed_text(workspace_id, "workspace_id")
    if workspace == "__offline__":
        raise ValueError("workspace_id must identify a concrete workspace")
    name = validate_graph_name(
        graph_name if graph_name is not None else f"cm_graph_{workspace}"
    )
    if name in {"cm_graph", "cm_graph_offline", *reserved_graph_names}:
        raise ValueError("workspace graph name is reserved")
    return name


__all__ = ["HoloTarget", "provisionable_graph_name"]

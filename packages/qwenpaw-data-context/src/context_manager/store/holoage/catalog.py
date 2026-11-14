"""Declare native AGE labels and searchable columns."""

from __future__ import annotations

from dataclasses import dataclass

from ...schema import GraphSchema


@dataclass(frozen=True, slots=True)
class HoloSchemaCatalog:
    """Describe physical storage derived from one logical graph schema."""

    schema: GraphSchema
    embed_dim: int

    def __post_init__(self) -> None:
        if type(self.schema) is not GraphSchema:
            raise TypeError("catalog schema must be a GraphSchema")
        if (
            isinstance(self.embed_dim, bool)
            or not isinstance(self.embed_dim, int)
            or self.embed_dim < 1
        ):
            raise ValueError("embed_dim must be a positive integer")

    @property
    def vertex_labels(self) -> tuple[str, ...]:
        return tuple(node.name for node in self.schema.nodes)

    @property
    def edge_labels(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(edge.relationship for edge in self.schema.edges))


__all__ = ["HoloSchemaCatalog"]

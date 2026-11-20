"""Define backend-neutral term records and failures."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..utils.validation import require_trimmed_text

_LABEL_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def graph_label(value: str) -> str:
    """Require one safe Neo4j label identifier."""
    label = require_trimmed_text(value, "graph label")
    if _LABEL_PATTERN.fullmatch(label) is None:
        raise ValueError("graph label must be a safe identifier")
    return label


class TermError(RuntimeError):
    """Report a term failure without database details."""


class UnknownTermError(TermError):
    """Report a property value absent from the workspace glossary."""


@dataclass(frozen=True, slots=True)
class Term:
    """Describe one allowed node-property value, its meaning and graph labels."""

    node_name: str
    property_name: str
    value: str
    description: str
    graph_labels: frozenset[str]

    def __post_init__(self) -> None:
        """Validate one detached term."""
        for field_name in (
            "node_name",
            "property_name",
            "value",
            "description",
        ):
            object.__setattr__(
                self,
                field_name,
                require_trimmed_text(getattr(self, field_name), field_name),
            )
        object.__setattr__(
            self,
            "graph_labels",
            frozenset(graph_label(label) for label in self.graph_labels),
        )


__all__ = [
    "Term",
    "TermError",
    "UnknownTermError",
]

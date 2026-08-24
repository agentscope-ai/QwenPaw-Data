"""Context retrieval configuration for anchors, strategy cards, and graph traversal."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RetrievalConfig:
    """Knobs used by context API retrieval paths (formerly part of NL2SQL pipeline config)."""

    semantic_split_retrieval: bool = False
    semantic_split_max_facets: int = 8
    traversal_fallback_max_depth: int = 10
    traversal_fallback_max_nodes: int = 500

"""ConflictAdapter — Protocol that each graph must implement.

Each graph (MG, TG, KG) provides an adapter that bridges the shared
conflict protocol with graph-specific write and scan logic.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional, Protocol, runtime_checkable

from ..model import ConflictDecision, ConflictEvent


@runtime_checkable
class ConflictAdapter(Protocol):
    """Per-graph conflict adapter."""

    def detect(self, node: dict) -> Optional[ConflictEvent]:
        """Detect conflicts for a node being written.

        Called synchronously before the writer commits.
        Returns None if no conflict detected.
        """
        ...

    def scan(
        self, since: Optional[dt.datetime] = None
    ) -> list[ConflictEvent]:
        """Background scan for conflicts among existing nodes.

        Args:
            since: Only scan nodes updated after this time. None = full scan.

        Returns list of detected ConflictEvents.
        """
        ...

    def apply(
        self,
        decision: ConflictDecision,
        event: ConflictEvent,
        winner_id: Optional[str] = None,
    ) -> None:
        """Apply an arbitration result to the graph.

        Writes SUPERSEDED_BY / CONTRADICTS edges, sets valid_to, etc.
        """
        ...

    def confidence(self, node_id: str) -> float:
        """Return the trust/confidence score for a node.

        Per-graph implementation:
        - KG: source_trust
        - TG: success_rate
        - MG: source_weight
        """
        ...

"""Define the shared node-deduplication role for graph applications."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from ..store.models import NodeDraft


@runtime_checkable
class Deduplicator(Protocol):
    """Apply one graph application's complete node-deduplication policy."""

    async def deduplicate(
        self,
        nodes: Sequence[NodeDraft],
    ) -> Sequence[NodeDraft]:
        """Return the application's retained and status-marked node drafts."""
        ...


__all__ = ["Deduplicator"]

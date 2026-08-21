"""Conflict detectors — write-time and background."""
from __future__ import annotations

from typing import Optional

from ..utils import get_logger
from .adapters.base import ConflictAdapter
from .model import ConflictEvent

log = get_logger("conflict.detector")


class FastCheckDetector:
    """Write-time conflict detector — exact key match via adapter."""

    def __init__(self, adapters: dict[str, ConflictAdapter]) -> None:
        self.adapters = adapters

    def check(self, node: dict) -> Optional[ConflictEvent]:
        graph = node.get("graph", "")
        adapter = self.adapters.get(graph)
        if adapter is None:
            return None
        event = adapter.detect(node)
        if event:
            log.info(
                "FastCheck hit: graph=%s, node=%s, type=%s",
                graph,
                event.node_id,
                event.conflict_type,
            )
        return event


class BackgroundScanner:
    """Background conflict scanner — delegates to adapter.scan()."""

    def __init__(self, adapters: dict[str, ConflictAdapter]) -> None:
        self.adapters = adapters

    def run(self, graph: str) -> list[ConflictEvent]:
        adapter = self.adapters.get(graph)
        if adapter is None:
            log.warning("No adapter for graph %s", graph)
            return []
        events = adapter.scan()
        log.info("Background scan %s: found %d conflicts", graph, len(events))
        return events

    def run_all(self) -> dict[str, list[ConflictEvent]]:
        results: dict[str, list[ConflictEvent]] = {}
        for graph in self.adapters:
            results[graph] = self.run(graph)
        total = sum(len(v) for v in results.values())
        log.info(
            "Background scan complete: %d total conflicts across %d graphs",
            total,
            len(results),
        )
        return results

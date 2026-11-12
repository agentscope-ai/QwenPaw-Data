"""Compare a stored heartbeat with a caller-supplied freshness window."""

from __future__ import annotations


def heartbeat_is_fresh(
    heartbeat_at: float | None, *, now: float, fresh_seconds: float
) -> bool:
    """Return whether ``heartbeat_at`` is still inside ``fresh_seconds`` of ``now``."""
    if heartbeat_at is None:
        return False
    return now - float(heartbeat_at) <= fresh_seconds

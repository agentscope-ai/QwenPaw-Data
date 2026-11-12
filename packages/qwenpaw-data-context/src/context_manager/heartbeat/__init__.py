"""Refresh owned rows, serialize claims, and fail abandoned in-flight work."""

from .lock import lock_scope, lock_scope_sync
from .refresh import Heartbeat, is_fresh, stale_after_seconds, stale_cutoff
from .sweep import run_orphan_sweep, sweep_connection

__all__ = [
    "Heartbeat",
    "is_fresh",
    "lock_scope",
    "lock_scope_sync",
    "run_orphan_sweep",
    "stale_after_seconds",
    "stale_cutoff",
    "sweep_connection",
]

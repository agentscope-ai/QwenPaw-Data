"""Expose explicit, in-place migrations for persisted Context Manager data."""

from .stateless import (
    copy_inflight_cm_jobs,
    ensure_deployed_stateless_columns,
    ensure_recovery_run_columns,
)
from .user_graph import UserGraphMigrationError, migrate_user_graph

__all__ = [
    "UserGraphMigrationError",
    "copy_inflight_cm_jobs",
    "ensure_deployed_stateless_columns",
    "ensure_recovery_run_columns",
    "migrate_user_graph",
]

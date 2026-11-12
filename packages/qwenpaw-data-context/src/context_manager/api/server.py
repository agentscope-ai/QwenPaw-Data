"""Serve the Context service at the module path used by App 0.3 launchers."""

from __future__ import annotations

from ..server import app, create_app
from .lifespan import _event_loop_lag_monitor

__all__ = ["_event_loop_lag_monitor", "app", "create_app"]

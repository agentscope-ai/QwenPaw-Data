"""Expose the canonical ASGI entrypoint for the Context Manager process."""

from __future__ import annotations

from functools import wraps
from typing import Any

from fastapi import FastAPI

from .api.app import create_service_app
from .settings import Settings


@wraps(create_service_app)
def create_app(*args: Any, **kwargs: Any) -> FastAPI:
    """Load deployment environment and compose the canonical service app."""
    Settings.load()
    return create_service_app(*args, **kwargs)


app = create_app()

__all__ = ["app", "create_app"]

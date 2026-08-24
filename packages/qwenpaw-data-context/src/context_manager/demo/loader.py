"""Loaders for bundled demo assets.

Assets are packaged inside the wheel so the docker-compose demo works without
a local QwenPaw-Data source checkout.
"""

from __future__ import annotations

import importlib.resources as resources


def semantic_workbook_bytes() -> bytes:
    """Return the raw bytes of the bundled semantic workbook."""
    return (
        resources.files("context_manager.demo.assets")
        .joinpath("demo_semantic_config.xlsx")
        .read_bytes()
    )


def seed_postgres_sql() -> str:
    """Return the bundled PostgreSQL seed script as text."""
    return (
        resources.files("context_manager.demo.assets")
        .joinpath("seed-postgres.sql")
        .read_text(encoding="utf-8")
    )

"""Bundled GAAP demo assets for QwenPaw Data.

This package ships the PostgreSQL seed script and the semantic workbook used
by the docker-compose one-shot demo. Assets are accessed via :mod:`loader`
so they remain available when the package is installed from PyPI.
"""

from context_manager.demo.loader import (
    seed_postgres_sql,
    semantic_workbook_bytes,
)

__all__ = [
    "seed_postgres_sql",
    "semantic_workbook_bytes",
]

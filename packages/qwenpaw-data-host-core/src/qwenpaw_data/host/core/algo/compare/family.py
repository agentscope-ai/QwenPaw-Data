# -*- coding: utf-8 -*-
"""Comparable document families.

Two documents can be diffed only when they map to the same family, so a CSV
never lines up against a SQL script. HTML is normalised to Markdown-shaped text
before diffing, so it belongs to the markdown family alongside plain text.
"""

from __future__ import annotations

from pathlib import Path

_COMPARE_FAMILIES: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "markdown",
    ".htm": "markdown",
    ".txt": "markdown",
    ".csv": "csv",
    ".tsv": "csv",
    ".sql": "sql",
    ".json": "json",
}


def compare_family(path: str) -> str | None:
    """Return the comparable family of a path, or None when not comparable."""
    return _COMPARE_FAMILIES.get(Path(path).suffix.lower())


__all__ = ["compare_family"]

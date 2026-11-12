"""QwenPaw Data CLI command registry."""

from __future__ import annotations

from . import datasource, doctor, semantic

COMMANDS = [datasource, semantic, doctor]

__all__ = ["COMMANDS"]

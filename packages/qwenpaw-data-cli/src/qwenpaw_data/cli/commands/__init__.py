"""QwenPaw Data CLI command registry."""

from __future__ import annotations

from . import chat, datasource, doctor, execute, plan, run, semantic

COMMANDS = [plan, execute, run, chat, datasource, semantic, doctor]

__all__ = ["COMMANDS"]

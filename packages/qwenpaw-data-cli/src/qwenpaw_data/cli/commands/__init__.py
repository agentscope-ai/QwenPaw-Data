"""QwenPaw Data CLI command registry."""

from __future__ import annotations

from . import chat, datasource, doctor, execute, plan, run, semantic

COMMANDS = [datasource, semantic, doctor, run, chat, plan, execute]

__all__ = ["COMMANDS"]

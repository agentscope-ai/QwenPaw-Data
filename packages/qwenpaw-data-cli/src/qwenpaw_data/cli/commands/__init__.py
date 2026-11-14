"""QwenPaw Data CLI command registry."""

from __future__ import annotations

from . import chat, datasource, doctor, plan, run, semantic

COMMANDS = [datasource, semantic, doctor, run, chat, plan]

__all__ = ["COMMANDS"]

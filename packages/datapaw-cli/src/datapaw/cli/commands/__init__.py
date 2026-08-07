"""DataPaw CLI command registry."""

from __future__ import annotations

from . import chat, datasource, doctor, execute, plan, run

COMMANDS = [plan, execute, run, chat, datasource, doctor]

__all__ = ["COMMANDS"]

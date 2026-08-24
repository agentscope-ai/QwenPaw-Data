"""Shared package constants."""

import os

QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED_ENV = "QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED"


def is_spawn_subagent_enabled() -> bool:
    """Return whether QwenPaw Data should expose its spawn_subagent tool."""
    raw = os.environ.get(QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}

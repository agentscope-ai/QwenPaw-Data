"""Shared package constants."""

import os

DATAPAW_SPAWN_SUBAGENT_ENABLED_ENV = "DATAPAW_SPAWN_SUBAGENT_ENABLED"


def is_spawn_subagent_enabled() -> bool:
    """Return whether DataPaw should expose its spawn_subagent tool."""
    raw = os.environ.get(DATAPAW_SPAWN_SUBAGENT_ENABLED_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}

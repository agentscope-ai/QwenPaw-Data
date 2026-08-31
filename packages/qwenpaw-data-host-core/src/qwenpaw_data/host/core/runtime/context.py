# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from qwenpaw_data.host.core.domain.identity import Identity


@dataclass
class RunContext:
    """Ephemeral dependencies and identifiers for one QwenPaw Data run."""

    session_id: str
    chat_id: str
    workspace: Any
    paths: Any
    identity: Identity
    user_runtime_config: Any | None = None
    request_context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.session_id.strip():
            raise ValueError("session_id is required")
        if not self.chat_id.strip():
            raise ValueError("chat_id is required")
        self.request_context = dict(self.request_context)

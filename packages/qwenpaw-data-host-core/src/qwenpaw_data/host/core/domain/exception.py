# -*- coding: utf-8 -*-
from __future__ import annotations


class IterationLimitExceeded(Exception):
    """The reply stopped because the ReAct iteration limit was reached."""

    def __init__(self, agent_name: str) -> None:
        name = agent_name.strip()
        if not name:
            raise ValueError("agent_name is required")
        self.agent_name = name
        super().__init__(
            f"Agent '{name}' has reached the maximum number of iterations."
        )

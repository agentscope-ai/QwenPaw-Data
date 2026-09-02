# -*- coding: utf-8 -*-
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Identity:
    """Business identity stamp for a runtime turn.

    Keeps the same injection position as the enterprise edition's
    tenant-aware Identity so downstream distributions can substitute a
    richer type without changing runtime signatures. Deployment-specific
    attributes go into ``attrs``.
    """

    user_id: str
    attrs: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("user_id is required")

    @classmethod
    def anonymous(cls) -> Identity:
        return cls(user_id="local")

# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass

from qwenpaw_data_auth import Principal


@dataclass(frozen=True)
class Identity:
    """Host business identity stamp: who / which tenant / which workspace."""

    user_id: str
    tenant_id: str
    workspace_id: str

    def __post_init__(self) -> None:
        if not self.user_id.strip():
            raise ValueError("user_id is required")
        if not self.tenant_id.strip():
            raise ValueError("tenant_id is required")
        if not self.workspace_id.strip():
            raise ValueError("workspace_id is required")

    @classmethod
    def from_principal(cls, principal: Principal) -> Identity:
        return cls(
            user_id=principal.user_id.strip(),
            tenant_id=principal.tenant_id.strip(),
            workspace_id=principal.active_workspace_id.strip(),
        )

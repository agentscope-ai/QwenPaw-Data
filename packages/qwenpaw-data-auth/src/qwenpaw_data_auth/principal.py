"""
Principal — the identity subject attached to every request.

A Principal answers: who is making this request, which tenant do they
belong to, which workspaces can they access, and what is their role.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    workspace_ids: list[str] = field(default_factory=list)
    active_workspace_id: str = ""
    role: str = "member"

    @property
    def is_admin(self) -> bool:
        return self.role in ("tenant_admin", "ws_admin")

    @property
    def is_tenant_admin(self) -> bool:
        return self.role == "tenant_admin"


DEFAULT_PRINCIPAL = Principal(
    user_id="__default__",
    tenant_id="default",
    workspace_ids=["default"],
    active_workspace_id="default",
    role="tenant_admin",
)

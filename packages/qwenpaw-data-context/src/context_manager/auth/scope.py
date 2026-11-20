"""Define the graph access result carried between APIs, services and Store."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ..utils.validation import optional_trimmed_text, require_trimmed_text

GLOBAL_OWNER_ID = "__global__"  # Authoritative graph owner within each workspace.


class AccessMode(StrEnum):
    """Finite access modes admitted by the graph persistence boundary."""

    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class WriteOwner(StrEnum):
    """Requested write ownership; GraphScopeAuthority decides whether to allow it."""

    USER = "user"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class ScopeBinding:
    """Carry the internal pass issued for an authorized graph operation.

    The pass identifies the requester separately from the graph's owners and
    limits access by mode, tenant/Workspace, datasource and zone. Construction
    checks data consistency; only GraphScopeAuthority can authorize and issue it.
    """

    tenant_id: str
    workspace_id: str
    user_id: str  # Requester, not necessarily the owner of the graph being read.
    mode: AccessMode  # Authorized read, write or administrative access.
    owner_ids: tuple[
        str, ...
    ] = ()  # Visible graph owners; empty means all in this Workspace.
    write_owner_id: str | None = None  # Exact owner whose graph may be written.
    datasource_id: str | None = None  # None means no additional datasource restriction.
    zone: str | None = None  # None means no additional graph-zone restriction.
    _issuer_id: str = field(default="", repr=False, compare=False)
    _signature: str = field(default="", repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize fields and check consistency, without evaluating permissions."""
        object.__setattr__(
            self,
            "tenant_id",
            require_trimmed_text(self.tenant_id, "scope tenant_id"),
        )
        object.__setattr__(
            self,
            "workspace_id",
            require_trimmed_text(self.workspace_id, "scope workspace_id"),
        )
        object.__setattr__(
            self,
            "user_id",
            require_trimmed_text(self.user_id, "scope user_id"),
        )
        if not isinstance(self.mode, AccessMode):
            object.__setattr__(self, "mode", AccessMode(self.mode))
        owners = tuple(
            dict.fromkeys(
                require_trimmed_text(owner, "owner_id") for owner in self.owner_ids
            )
        )
        object.__setattr__(self, "owner_ids", owners)
        object.__setattr__(
            self,
            "write_owner_id",
            optional_trimmed_text(self.write_owner_id, "write_owner_id"),
        )
        object.__setattr__(
            self,
            "datasource_id",
            optional_trimmed_text(self.datasource_id, "datasource_id"),
        )
        object.__setattr__(
            self,
            "zone",
            optional_trimmed_text(self.zone, "zone"),
        )
        if self.mode is AccessMode.WRITE and self.write_owner_id is None:
            raise ValueError("write scopes require write_owner_id")
        if (
            self.write_owner_id is not None
            and self.owner_ids
            and self.write_owner_id not in self.owner_ids
        ):
            raise ValueError("write_owner_id must be included in owner_ids")


__all__ = ["GLOBAL_OWNER_ID", "AccessMode", "ScopeBinding", "WriteOwner"]

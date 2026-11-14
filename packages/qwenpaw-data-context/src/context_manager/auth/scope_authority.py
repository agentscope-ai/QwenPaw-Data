"""Decide graph permissions and issue ScopeBinding from trusted identity."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from uuid import uuid4

from qwenpaw_data_auth.authorization import AuthorizationContext

from ..utils.validation import require_trimmed_text
from .authorization import active_target, require_target
from .scope import GLOBAL_OWNER_ID, AccessMode, ScopeBinding, WriteOwner

_WORKSPACE_ADMIN_ROLES = frozenset({"tenant_admin", "ws_admin"})


class GraphScopeError(PermissionError):
    """Report a denied or forged graph capability."""


class GraphScopeAuthority:
    """Authorize an operation's graph range and issue its internal pass.

    AuthorizationContext supplies trusted identity and roles; operation inputs
    specify the requested owner, datasource, zone and access mode. This authority
    applies graph policy, issues ScopeBinding, and validates it at Store entry.
    """

    __slots__ = ("_admin_role", "_issuer_id", "_signing_key")

    def __init__(self, *, admin_role: str = "context_admin") -> None:
        """Create one capability authority with an isolated issuer identity."""
        self._admin_role = require_trimmed_text(admin_role, "admin_role")
        self._issuer_id = uuid4().hex
        self._signing_key = secrets.token_bytes(32)

    def issue_read_scope(
        self,
        context: AuthorizationContext,
        *,
        datasource_id: str | None = None,
        zone: str | None = None,
        owner_id: str | None = None,
        review: bool = False,
    ) -> ScopeBinding:
        """Read the caller's graph, authoritative MG, or a review target.

        review=True requires workspace administration even for the caller's own
        graph or an omitted owner; it never grants write access.
        """
        tenant_id, workspace_id = active_target(context)
        require_target(context, tenant_id=tenant_id, workspace_id=workspace_id)
        is_workspace_admin = context.active_workspace_role in _WORKSPACE_ADMIN_ROLES
        if review and not is_workspace_admin:
            raise GraphScopeError("User Graph review requires workspace administration")
        owners = () if is_workspace_admin else (context.user_id,)
        if owner_id is not None:
            owner_id = require_trimmed_text(owner_id, "owner_id")
            if owner_id == GLOBAL_OWNER_ID:
                if zone not in {None, "metadata"}:
                    raise GraphScopeError("workspace reads require the Metadata zone")
                zone = "metadata"  # The global owner exposes only authoritative MG.
            elif owner_id != context.user_id and owners:
                raise GraphScopeError(
                    "reviewing another user requires workspace administration"
                )
            owners = (owner_id,)
        return self._issue(
            context,
            tenant_id,
            workspace_id,
            AccessMode.READ,
            owner_ids=owners,
            datasource_id=datasource_id,
            zone=zone,
        )

    def narrow_read_scope(
        self,
        scope: ScopeBinding,
        *,
        datasource_id: str | None,
        zone: str | None,
    ) -> ScopeBinding:
        """Issue an equal-or-narrower read capability from one valid Scope."""
        self.require(scope, AccessMode.READ)
        if scope.datasource_id is not None and scope.datasource_id != datasource_id:
            raise GraphScopeError("scope cannot widen datasource access")
        if scope.zone is not None and scope.zone != zone:
            raise GraphScopeError("scope cannot widen graph zone access")
        return self._issue_binding(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            mode=AccessMode.READ,
            owner_ids=scope.owner_ids,
            datasource_id=datasource_id,
            zone=zone,
        )

    def metadata_source_scope(self, scope: ScopeBinding) -> ScopeBinding:
        """Authorize reading workspace MG as the source of a member's pull."""
        self.require(scope, AccessMode.READ)
        if scope.zone not in {None, "metadata"}:
            raise GraphScopeError(
                "authoritative Metadata requires a Metadata-compatible scope"
            )
        return self._issue_binding(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            mode=AccessMode.READ,
            owner_ids=(GLOBAL_OWNER_ID,),
            datasource_id=scope.datasource_id,
            zone="metadata",
        )

    def narrow_write_scope(
        self,
        scope: ScopeBinding,
        *,
        datasource_id: str | None,
    ) -> ScopeBinding:
        """Keep one writer and restrict a batch to a single datasource."""
        self.require(scope, AccessMode.WRITE)
        if scope.datasource_id is not None and scope.datasource_id != datasource_id:
            raise GraphScopeError("scope cannot widen datasource access")
        return self._issue_binding(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            mode=AccessMode.WRITE,
            owner_ids=scope.owner_ids,
            write_owner_id=scope.write_owner_id,
            datasource_id=datasource_id,
            zone=scope.zone,
        )

    def issue_write_scope(
        self,
        context: AuthorizationContext,
        *,
        owner: WriteOwner,
        datasource_id: str | None = None,
        zone: str | None = None,
    ) -> ScopeBinding:
        """Issue a write capability using one finite ownership policy."""
        tenant_id, workspace_id = active_target(context)
        require_target(context, tenant_id=tenant_id, workspace_id=workspace_id)
        if type(owner) is not WriteOwner:
            raise TypeError("owner must be a WriteOwner")
        if owner is WriteOwner.USER:
            owner_ids = (context.user_id,)
            write_owner_id = context.user_id
        else:
            if context.active_workspace_role not in _WORKSPACE_ADMIN_ROLES:
                raise GraphScopeError(
                    "workspace writes require workspace administration"
                )
            owner_ids = (GLOBAL_OWNER_ID,)
            write_owner_id = GLOBAL_OWNER_ID
        return self._issue(
            context,
            tenant_id,
            workspace_id,
            AccessMode.WRITE,
            owner_ids=owner_ids,
            write_owner_id=write_owner_id,
            datasource_id=datasource_id,
            zone=zone,
        )

    def issue_admin_scope(
        self,
        context: AuthorizationContext,
        *,
        owner_id: str | None = None,
        datasource_id: str | None = None,
        zone: str | None = None,
    ) -> ScopeBinding:
        """Authorize administration within the trusted actor's active workspace."""
        tenant_id, workspace_id = active_target(context)
        require_target(context, tenant_id=tenant_id, workspace_id=workspace_id)
        if context.active_workspace_role not in (
            _WORKSPACE_ADMIN_ROLES | {self._admin_role}
        ):
            raise GraphScopeError("authorization lacks graph administration role")
        if owner_id is not None:
            owner_id = require_trimmed_text(owner_id, "owner_id")
        return self._issue(
            context,
            tenant_id,
            workspace_id,
            AccessMode.ADMIN,
            owner_ids=(owner_id,) if owner_id is not None else (),
            write_owner_id=owner_id,
            datasource_id=datasource_id,
            zone=zone,
        )

    def internal_read_scope(self, scope: ScopeBinding) -> ScopeBinding:
        """Read personal internal state for the same target as a business scope.

        Internal services use this to consult their metadata; public graph
        endpoints still reject internal Specs. No write capability is granted.
        """
        self.require(scope, AccessMode.READ)
        if scope.owner_ids != (scope.user_id,):
            raise GraphScopeError(
                "internal state reads require the actor's personal scope"
            )
        return self._issue_binding(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            mode=AccessMode.READ,
            owner_ids=scope.owner_ids,
            datasource_id=scope.datasource_id,
            zone="_internal",
        )

    def require(self, scope: ScopeBinding, required: AccessMode | str) -> None:
        """Reject forged, cross-authority, or insufficient capabilities."""
        if type(scope) is not ScopeBinding:
            raise GraphScopeError("scope must be a concrete ScopeBinding")
        if scope._issuer_id != self._issuer_id:
            raise GraphScopeError("scope was not issued by this GraphScopeAuthority")
        if not hmac.compare_digest(scope._signature, self._signature(scope)):
            raise GraphScopeError("scope was forged or modified")
        required_mode = AccessMode(required)
        admitted = {
            AccessMode.READ: frozenset(AccessMode),
            AccessMode.WRITE: frozenset({AccessMode.WRITE, AccessMode.ADMIN}),
            AccessMode.ADMIN: frozenset({AccessMode.ADMIN}),
        }[required_mode]
        if scope.mode not in admitted:
            raise GraphScopeError(
                f"{scope.mode.value} scope cannot satisfy {required_mode.value} access"
            )

    def _issue(
        self,
        context: AuthorizationContext,
        tenant_id: str,
        workspace_id: str,
        mode: AccessMode,
        *,
        owner_ids: tuple[str, ...] = (),
        write_owner_id: str | None = None,
        datasource_id: str | None = None,
        zone: str | None = None,
    ) -> ScopeBinding:
        """Construct one opaque Scope after policy checks."""
        return self._issue_binding(
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            user_id=context.user_id,
            mode=mode,
            owner_ids=owner_ids,
            write_owner_id=write_owner_id,
            datasource_id=datasource_id,
            zone=zone,
        )

    def _issue_binding(self, **values: object) -> ScopeBinding:
        """Construct and sign one normalized internal graph capability."""
        scope = ScopeBinding(**values, _issuer_id=self._issuer_id)
        object.__setattr__(scope, "_signature", self._signature(scope))
        return scope

    def _signature(self, scope: ScopeBinding) -> str:
        """Authenticate every field that can affect graph authorization."""
        payload = json.dumps(
            [
                scope.tenant_id,
                scope.workspace_id,
                scope.user_id,
                scope.mode.value,
                list(scope.owner_ids),
                scope.write_owner_id,
                scope.datasource_id,
                scope.zone,
                scope._issuer_id,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()


__all__ = ["GraphScopeAuthority", "GraphScopeError"]

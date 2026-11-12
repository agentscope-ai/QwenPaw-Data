"""Verify AuthorizationContext-to-Scope capability behavior."""

from __future__ import annotations

import pytest
from context_manager.auth import (
    AccessMode,
    AuthorizationError,
    GraphScopeAuthority,
    GraphScopeError,
    ScopeBinding,
    WriteOwner,
    require_target,
)

from cm.infrastructure.access.support import authorization


def test_authorization_requires_membership_in_the_requested_workspace() -> None:
    with pytest.raises(AuthorizationError, match="active workspace"):
        require_target(
            authorization(),
            tenant_id="tenant-1",
            workspace_id="workspace-2",
        )


def test_scope_authority_rejects_forged_and_cross_authority_scopes() -> None:
    """Only the issuing authority may authenticate a capability."""
    authority = GraphScopeAuthority()
    scope = authority.issue_read_scope(authorization())

    authority.require(scope, AccessMode.READ)
    with pytest.raises(GraphScopeError, match="not issued"):
        GraphScopeAuthority().require(scope, AccessMode.READ)
    forged = ScopeBinding(
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        user_id="user-1",
        mode=AccessMode.READ,
    )
    with pytest.raises(GraphScopeError, match="not issued"):
        authority.require(forged, AccessMode.READ)


def test_read_scope_can_only_be_narrowed_by_its_issuer() -> None:
    """Scope narrowing needs no identity after the base capability is issued."""
    authority = GraphScopeAuthority()
    base = authority.issue_read_scope(
        authorization(),
        datasource_id="datasource-1",
        zone="metadata",
    )

    narrowed = authority.narrow_read_scope(
        base,
        datasource_id="datasource-1",
        zone="metadata",
    )
    authority.require(narrowed, AccessMode.READ)
    assert narrowed.owner_ids == ("user-1",)
    assert narrowed.datasource_id == "datasource-1"
    assert narrowed.zone == "metadata"

    with pytest.raises(GraphScopeError, match="datasource"):
        authority.narrow_read_scope(
            base,
            datasource_id="datasource-2",
            zone="metadata",
        )
    with pytest.raises(GraphScopeError, match="zone"):
        authority.narrow_read_scope(
            base,
            datasource_id="datasource-1",
            zone="knowledge",
        )
    with pytest.raises(GraphScopeError, match="not issued"):
        GraphScopeAuthority().narrow_read_scope(
            base,
            datasource_id="datasource-1",
            zone="metadata",
        )


def test_members_cannot_write_authoritative_metadata_or_review_someone_elses_graph():
    authority = GraphScopeAuthority()
    with pytest.raises(GraphScopeError, match="administration"):
        authority.issue_write_scope(
            authorization(), owner=WriteOwner.WORKSPACE, zone="metadata"
        )
    with pytest.raises(GraphScopeError, match="administration"):
        authority.issue_read_scope(authorization(), owner_id="another-user")


@pytest.mark.parametrize("workspace", ["workspace-1", "workspace-2"])
def test_metadata_source_scope_keeps_workspace_and_datasource_boundaries(workspace):
    authority = GraphScopeAuthority()
    personal = authority.issue_write_scope(
        authorization(workspace_id=workspace),
        owner=WriteOwner.USER,
        datasource_id="warehouse",
        zone="metadata",
    )

    source = authority.metadata_source_scope(personal)

    assert source.owner_ids == ("__global__",)
    assert source.workspace_id == workspace
    assert source.tenant_id == personal.tenant_id
    assert source.user_id == personal.user_id
    assert source.datasource_id == "warehouse"
    assert source.zone == "metadata"
    assert source.mode is AccessMode.READ
    authority.require(source, AccessMode.READ)

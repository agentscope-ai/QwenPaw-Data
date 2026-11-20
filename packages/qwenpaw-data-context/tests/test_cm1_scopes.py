"""Exercise trusted identity and issued graph capabilities without a graph server."""

from dataclasses import replace

import pytest
from qwenpaw_data_auth.authorization import AuthorizationContext

from context_manager.auth import (
    AccessMode,
    AuthorizationError,
    GLOBAL_OWNER_ID,
    GraphScopeAuthority,
    GraphScopeError,
    ScopeBinding,
    WriteOwner,
    active_target,
    bind_authorization,
    get_authorization,
    require_target,
)


def identity(**changes):
    values = dict(
        user_id="alice",
        tenant_id="tenant",
        role="member",
        active_workspace_role="member",
        workspace_ids=frozenset({"workspace"}),
        active_workspace_id="workspace",
    )
    values.update(changes)
    return AuthorizationContext(**values)


def test_authorization_binding_restores_nested_context_after_error():
    outer, inner = identity(), identity(user_id="bob")
    assert get_authorization(required=False) is None
    with pytest.raises(PermissionError):
        get_authorization()
    with bind_authorization(outer):
        with pytest.raises(RuntimeError):
            with bind_authorization(inner):
                assert get_authorization() is inner
                raise RuntimeError("operation failed")
        assert get_authorization() is outer
    assert get_authorization(required=False) is None


@pytest.mark.parametrize("changes", [
    {"tenant_id": None},
    {"active_workspace_id": None},
    {"workspace_ids": frozenset()},
    {"role": "platform_admin"},
    {"active_workspace_role": "platform_admin"},
])
def test_identity_fails_closed(changes):
    with pytest.raises(AuthorizationError):
        require_target(identity(**changes), tenant_id="tenant", workspace_id="workspace")


@pytest.mark.parametrize("tenant,workspace", [
    ("another-tenant", "workspace"), ("tenant", "another-workspace"),
])
def test_membership_cannot_authorize_another_target(tenant, workspace):
    with pytest.raises(AuthorizationError):
        require_target(identity(), tenant_id=tenant, workspace_id=workspace)


def test_identity_rejects_duck_typed_authentication():
    with pytest.raises(TypeError):
        active_target(object())
    with pytest.raises(TypeError):
        with bind_authorization(object()):
            pass


def test_member_read_and_write_owners_are_personal():
    authority = GraphScopeAuthority()
    read = authority.issue_read_scope(identity())
    write = authority.issue_write_scope(identity(), owner=WriteOwner.USER)
    assert read.owner_ids == write.owner_ids == ("alice",)
    assert write.write_owner_id == "alice"
    authority.require(write, AccessMode.READ)
    authority.require(write, AccessMode.WRITE)
    with pytest.raises(GraphScopeError):
        authority.require(read, AccessMode.WRITE)
    with pytest.raises(GraphScopeError):
        authority.require(write, AccessMode.ADMIN)


@pytest.mark.parametrize("kwargs", [
    {"owner_id": "bob"}, {"review": True}, {"owner_id": "alice", "review": True},
])
def test_member_cannot_review_another_or_own_graph(kwargs):
    with pytest.raises(GraphScopeError):
        GraphScopeAuthority().issue_read_scope(identity(), **kwargs)


def test_global_metadata_is_readable_but_not_member_writable():
    authority = GraphScopeAuthority()
    scope = authority.issue_read_scope(identity(), owner_id=GLOBAL_OWNER_ID)
    assert scope.owner_ids == (GLOBAL_OWNER_ID,)
    assert scope.zone == "metadata"
    with pytest.raises(GraphScopeError):
        authority.issue_read_scope(identity(), owner_id=GLOBAL_OWNER_ID, zone="knowledge")
    with pytest.raises(GraphScopeError):
        authority.issue_write_scope(identity(), owner=WriteOwner.WORKSPACE)


@pytest.mark.parametrize("role", ["tenant_admin", "ws_admin"])
def test_workspace_admin_can_review_and_publish(role):
    authority = GraphScopeAuthority()
    admin = identity(active_workspace_role=role)
    review = authority.issue_read_scope(admin, owner_id="bob", review=True)
    write = authority.issue_write_scope(admin, owner=WriteOwner.WORKSPACE)
    assert review.owner_ids == ("bob",)
    assert review.mode is AccessMode.READ
    assert write.write_owner_id == GLOBAL_OWNER_ID
    authority.require(authority.issue_admin_scope(admin), AccessMode.ADMIN)


def test_role_in_other_workspace_does_not_grant_active_admin():
    authority = GraphScopeAuthority()
    context = identity(role="ws_admin", admin_workspace_ids=frozenset({"other"}))
    with pytest.raises(GraphScopeError):
        authority.issue_admin_scope(context)
    with pytest.raises(GraphScopeError):
        authority.issue_write_scope(context, owner=WriteOwner.WORKSPACE)


def test_direct_and_cross_authority_scopes_are_rejected():
    authority = GraphScopeAuthority()
    direct = ScopeBinding("tenant", "workspace", "alice", AccessMode.READ)
    with pytest.raises(GraphScopeError):
        authority.require(direct, AccessMode.READ)
    with pytest.raises(GraphScopeError):
        authority.require(GraphScopeAuthority().issue_read_scope(identity()), AccessMode.READ)
    with pytest.raises(GraphScopeError):
        authority.require(object(), AccessMode.READ)


@pytest.mark.parametrize("changes", [
    {"tenant_id": "other"}, {"workspace_id": "other"}, {"user_id": "bob"},
    {"owner_ids": ()}, {"mode": AccessMode.ADMIN}, {"write_owner_id": "alice"},
    {"datasource_id": None}, {"zone": None},
])
def test_copying_issuer_or_signature_cannot_forge_a_wider_scope(changes):
    authority = GraphScopeAuthority()
    scope = authority.issue_read_scope(identity(), datasource_id="source", zone="metadata")
    with pytest.raises(GraphScopeError, match="forged or modified"):
        authority.require(replace(scope, **changes), AccessMode.READ)
    assert scope._issuer_id not in repr(scope)
    assert scope._signature not in repr(scope)


def test_narrowing_issues_valid_capabilities_without_widening():
    authority = GraphScopeAuthority()
    read = authority.narrow_read_scope(
        authority.issue_read_scope(identity()), datasource_id="source", zone="metadata"
    )
    authority.require(read, AccessMode.READ)
    assert read.owner_ids == ("alice",)
    for datasource, zone in [(None, "metadata"), ("source", None), ("other", "metadata")]:
        with pytest.raises(GraphScopeError):
            authority.narrow_read_scope(read, datasource_id=datasource, zone=zone)
    write = authority.narrow_write_scope(
        authority.issue_write_scope(identity(), owner=WriteOwner.USER), datasource_id="source"
    )
    authority.require(write, AccessMode.WRITE)
    assert write.write_owner_id == "alice"
    with pytest.raises(GraphScopeError):
        authority.narrow_write_scope(write, datasource_id=None)


def test_internal_and_metadata_source_scopes_preserve_target_and_never_grant_write():
    authority = GraphScopeAuthority()
    personal = authority.issue_read_scope(identity(), datasource_id="source")
    internal = authority.internal_read_scope(personal)
    metadata = authority.metadata_source_scope(personal)
    assert internal.owner_ids == ("alice",) and internal.zone == "_internal"
    assert metadata.owner_ids == (GLOBAL_OWNER_ID,) and metadata.zone == "metadata"
    for scope in (internal, metadata):
        assert (scope.tenant_id, scope.workspace_id, scope.datasource_id) == (
            "tenant", "workspace", "source"
        )
        authority.require(scope, AccessMode.READ)
        with pytest.raises(GraphScopeError):
            authority.require(scope, AccessMode.WRITE)
    with pytest.raises(GraphScopeError):
        authority.internal_read_scope(metadata)
    with pytest.raises(GraphScopeError):
        authority.metadata_source_scope(internal)

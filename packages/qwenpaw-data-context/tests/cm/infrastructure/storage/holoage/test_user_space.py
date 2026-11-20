"""Distinguish unavailable personal storage from unauthorized graph access."""

from types import SimpleNamespace

import pytest
from context_manager.auth import GraphScopeAuthority
from context_manager.metadata import MetadataSyncService
from context_manager.schema import DOMAIN, GRAPH_SCHEMA
from context_manager.store import StoreError
from context_manager.store.holoage import HoloGraphStore, HoloTarget

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.schema_support import make_glossary

OWNER = 'cm:user-space:["tenant-1","workspace-1","user-1"]'


@pytest.fixture
def personal_store(database_engine):
    catalog = {"rows": []}

    def read(_statement, _params, *, operation, max_rows=None):
        return catalog["rows"] if operation == "require_user_space" else []

    access = GraphScopeAuthority()
    store = HoloGraphStore(
        SimpleNamespace(
            target=HoloTarget("tenant-1", "workspace-1", "global"), read=read
        ),
        GRAPH_SCHEMA,
        access,
        make_glossary(GRAPH_SCHEMA, engine=database_engine),
        owns_runtime=False,
    )
    scope = access.issue_read_scope(authorization(), owner_id="user-1")
    return (
        store,
        scope,
        catalog,
        MetadataSyncService(store, database_engine=database_engine),
    )


def test_personal_graph_transitions_from_missing_through_initializing_to_ready(
    personal_store,
):
    store, scope, catalog, sync = personal_store
    for rows, state in (
        ([], "uninitialized"),
        ([{"owner": None}], "uninitialized"),
        ([{"owner": "initializing:" + OWNER}], "initializing"),
        ([{"owner": OWNER}], "ready"),
    ):
        catalog["rows"] = rows
        assert sync.status(scope) == {
            "space_status": state,
            "global_digest": "",
            "last_synced_at": None,
            "needs_sync": True,
        }
        if state == "ready":
            assert store.list_nodes(DOMAIN, scope=scope) == ()
        else:
            with pytest.raises(StoreError) as error:
                store.list_nodes(DOMAIN, scope=scope)
            assert error.value.code == f"user_space_{state}"


@pytest.mark.parametrize(
    "marker",
    [
        OWNER.replace("user-1", "user-2"),
        "initializing:" + OWNER.replace("user-1", "user-2"),
    ],
)
def test_other_owner_is_forbidden_not_uninitialized(personal_store, marker):
    store, scope, catalog, sync = personal_store
    catalog["rows"] = [{"owner": marker}]
    with pytest.raises(PermissionError):
        sync.status(scope)
    with pytest.raises(PermissionError):
        store.list_nodes(DOMAIN, scope=scope)

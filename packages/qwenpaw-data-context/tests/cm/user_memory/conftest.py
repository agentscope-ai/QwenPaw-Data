"""Shared authenticated graph for User ingest behavior tests."""

import pytest
from context_manager.auth import WriteOwner

from cm.infrastructure.access.support import authorization
from cm.knowledge.support import MemoryGraphStore


@pytest.fixture
def user_graph():
    store = MemoryGraphStore()
    context = authorization()
    read = store.access.issue_read_scope(context)
    write = store.access.issue_write_scope(context, owner=WriteOwner.USER, zone="user")
    return store, context, read, write

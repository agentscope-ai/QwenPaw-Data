"""Verify migrated memory management and vector rebuilds against local Neo4j."""

import pytest
from context_manager.auth import WriteOwner
from context_manager.embedding import embed_all
from context_manager.schema import ENTITY, HAS_MEMORY, USER, USER_MEMORY
from context_manager.user.crud import (
    MemoryCrudService,
    UserGraphError,
)

from cm.infrastructure.access.support import authorization
from cm.infrastructure.execution.support import executor
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    graph as graph,  # noqa: PLC0414 - intentional test fixture
)
from cm.infrastructure.storage.neo4j.test_batch_integration import (
    local_driver as local_driver,  # noqa: PLC0414 - intentional test fixture
)
from cm.user_memory.support import FakeLlmCaller


@pytest.mark.asyncio
async def test_memory_management_replaces_references_and_soft_deletes_without_cross_user_access(
    graph,  # noqa: F811
):
    store, _, (knowledge_scope, _) = graph
    context = authorization(user_id="alice", workspace_id=knowledge_scope.workspace_id)
    read_scope = store.access.issue_read_scope(context)
    write_scope = store.access.issue_write_scope(
        context, owner=WriteOwner.USER, zone="user"
    )
    first, second = store.get_nodes(
        ENTITY,
        store.create_nodes(
            ENTITY,
            (
                {"identity_hash": "sales", "type": "product", "name": "销售报表"},
                {"identity_hash": "refunds", "type": "product", "name": "退款报表"},
            ),
            scope=knowledge_scope,
        ),
        scope=knowledge_scope,
    )
    store.create_nodes(
        USER, ({"user_id": "alice", "status": "active"},), scope=write_scope
    )
    service = MemoryCrudService(store)
    assert (
        service.patch_profile({"display_name": "小林"}, write_scope)["profile"][
            "display_name"
        ]
        == "小林"
    )
    profile = service.patch_profile({"locale": "zh-CN"}, write_scope)
    assert profile["profile"] == {
        "display_name": "小林",
        "locale": "zh-CN",
        "description": None,
        "timezone": None,
    }

    request = {
        "type": "anchored_rule",
        "description": "分析销售报表时，请先排除退款订单。",
    }
    created = service.create_memory(
        request,
        read_scope,
        write_scope,
        references=[("Entity", first.key)],
    )
    merged = service.create_memory(
        request,
        read_scope,
        write_scope,
        references=[("Entity", first.key)],
    )
    assert created["action"] == "created" and merged["action"] == "merged"
    assert created["id"] == merged["id"]
    assert len(merged["source_refs"]) == 2
    assert len(store.list_edges(HAS_MEMORY, scope=read_scope)) == 1
    (manual,) = store.list_nodes(USER_MEMORY, scope=read_scope)
    assert not manual.properties.get("embedding")
    assert not manual.properties.get("embedding_hash")
    embedder = FakeLlmCaller(embeddings=((1.0, 0.0),))
    assert await embed_all(store, write_scope, embedder, executor=executor) == 1
    (original,) = store.get_nodes(USER_MEMORY, (manual.key,), scope=read_scope)

    changed = service.patch_memory(
        created["id"],
        {
            "description": "分析退款报表时，请按退款完成日期汇总。",
        },
        read_scope,
        write_scope,
        references=[("Entity", second.key)],
    )
    (updated,) = store.get_nodes(USER_MEMORY, (original.key,), scope=read_scope)
    assert updated.key == original.key
    # Manual edits preserve vectors until a later ingest or explicit rebuild.
    assert updated.properties["embedding"] == original.properties["embedding"]
    assert updated.properties["embedding_hash"] == original.properties["embedding_hash"]
    assert len(embedder.embed_calls) == 1
    assert [reference["key"] for reference in changed["references"]] == [second.key]
    reference_spec = store.schema.edge(("UserMemory", "REFERS_TO", "Entity"))
    old_ref, new_ref = store.get_edges(
        reference_spec,
        ((original.key, first.key), (original.key, second.key)),
        scope=read_scope,
    )
    assert old_ref is None and new_ref is not None

    bob_scope = store.access.issue_read_scope(
        authorization(user_id="bob", workspace_id=knowledge_scope.workspace_id)
    )
    with pytest.raises(UserGraphError) as denied:
        service.get_memory("alice", created["id"], bob_scope)
    assert denied.value.code == "not_found"
    assert store.get_nodes(USER_MEMORY, (original.key,), scope=bob_scope) == (None,)

    revoked = service.revoke_memory(created["id"], read_scope, write_scope)
    assert revoked["status"] == "revoked"
    assert [reference["key"] for reference in revoked["references"]] == [second.key]
    (persisted,) = store.get_nodes(USER_MEMORY, (original.key,), scope=read_scope)
    assert persisted.properties["status"] == "revoked"
    assert len(store.list_edges(HAS_MEMORY, scope=read_scope)) == 1

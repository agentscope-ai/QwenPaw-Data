"""Verify bounded document parsing and atomic Knowledge graph import."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest
from context_manager.auth import ScopeBinding, WriteOwner
from context_manager.knowledge.algo.deduplicator import identity_hash
from context_manager.process import (
    build_embedding_text,
)
from context_manager.schema import (
    DEFAULT_GLOSSARY_FILE,
    ENTITY,
    EVENT,
    GRAPH_SCHEMA,
    PARTICIPATION,
    Glossary,
)

from cm.infrastructure.access.support import authorization
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.document_support import (
    DocumentResources,
    document_importer,
    make_document_resources,
)
from cm.knowledge.import_support import FakeKnowledgeCaller, _docx, _response
from cm.knowledge.support import MemoryGraphStore


@pytest.fixture
def document_resources(tmp_path: Path) -> DocumentResources:
    return make_document_resources(
        tmp_path,
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        user_id="user-1",
    )


def _knowledge_scope(
    store: MemoryGraphStore,
    *,
    user_id: str = "user-1",
    workspace_id: str = "workspace-1",
) -> ScopeBinding:
    """Issue one user-owned Knowledge write capability."""
    return store.access.issue_write_scope(
        authorization(user_id=user_id, workspace_id=workspace_id),
        owner=WriteOwner.USER,
        zone="knowledge",
    )


def _seed_existing_entity(
    store: MemoryGraphStore,
    scope: ScopeBinding,
) -> str:
    """Create one previously embedded Entity and return its Store key."""
    identity = {
        "name": "AcmeChat",
        "canonical_name": "AcmeChat",
        "type": "product",
        "aliases": ("Acme",),
        "lifecycle_state": "active",
    }
    embedding_text = build_embedding_text(ENTITY, identity)
    record = store.get_nodes(
        ENTITY,
        store.create_nodes(
            ENTITY,
            (
                {
                    **identity,
                    "identity_hash": identity_hash(ENTITY, identity),
                    "source_ids": ("document:existing",),
                    "embedding": (1.0, 0.0, 0.0),
                    "embedding_hash": sha256(
                        embedding_text.encode("utf-8")
                    ).hexdigest(),
                },
            ),
            scope=scope,
        ),
        scope=scope,
    )[0]
    return record.key


def _isolated_glossary(tmp_path: Path) -> Glossary:
    """Copy the canonical terms so mutation remains test-local."""
    path = tmp_path / "terms.json"
    path.write_bytes(DEFAULT_GLOSSARY_FILE.read_bytes())
    return make_glossary(GRAPH_SCHEMA, path)


@pytest.mark.asyncio
async def test_document_import_and_reimport_preserve_identity_and_reviewed_relationship(
    document_resources,
):
    store = MemoryGraphStore()
    response = _response()
    response["events"][0]["properties"]["date_from"] = "2026-08-01"
    caller = FakeKnowledgeCaller(response)
    service = document_importer(document_resources, store=store, caller=caller)
    scope = _knowledge_scope(store)
    upload = _docx("AcmeChat released a new feature.")
    result = await service.import_document(upload, authorization(), scope)
    assert result.status == "success"
    entity = store.list_nodes(ENTITY, scope=scope)[0]
    event = store.list_nodes(EVENT, scope=scope)[0]
    assert event.properties["date_from"].isoformat() == "2026-08-01"
    edge = store.list_edges(PARTICIPATION, scope=scope)[0]
    assert entity.properties["name"] == "AcmeChat"
    assert entity.properties["source_ids"] == (result.source_id,)
    assert {edge.source_key, edge.target_key} == {entity.key, event.key}
    assert edge.properties["description"] == "AcmeChat is the subject of the update"
    store.patch_edges(
        PARTICIPATION,
        (
            (
                edge.source_key,
                edge.target_key,
                {"description": None, "role": "reviewed subject"},
            ),
        ),
        scope=scope,
    )
    await service.reingest_document(result.source_id, authorization(), scope)
    assert [node.key for node in store.list_nodes(ENTITY, scope=scope)] == [entity.key]
    assert [node.key for node in store.list_nodes(EVENT, scope=scope)] == [event.key]
    persisted = store.list_edges(PARTICIPATION, scope=scope)
    assert len(persisted) == 1
    assert persisted[0].properties["role"] == "reviewed subject"
    assert persisted[0].properties["description"] == edge.properties["description"]


@pytest.mark.asyncio
async def test_existing_facts_are_merged_before_persistent_embedding(
    document_resources,
) -> None:
    """Prior aliases survive while new aliases and description refresh the vector."""
    store = MemoryGraphStore()
    scope = _knowledge_scope(store)
    entity_key = _seed_existing_entity(store, scope)
    caller = FakeKnowledgeCaller(_response(), resolution_decision="match")
    service = document_importer(document_resources, store=store, caller=caller)

    plan = await service.prepare_plan(
        _docx("AcmeChat, also known as Example Assistant, is an AI product."),
        authorization(scope.user_id, scope.tenant_id, scope.workspace_id),
        scope,
    )
    await service.execute_plan(plan, scope)

    persisted = store.get_nodes(ENTITY, (entity_key,), scope=scope)[0]
    assert persisted is not None
    assert persisted.properties["aliases"] == ("Acme", "示例助手")
    assert persisted.properties["description"] == "An AI product"
    assert persisted.properties["source_ids"] == ("document:existing", plan.source_id)
    assert (
        persisted.properties["embedding_hash"]
        == sha256(
            build_embedding_text(ENTITY, persisted.properties).encode("utf-8")
        ).hexdigest()
    )


@pytest.mark.asyncio
async def test_distinct_entity_type_is_added_as_non_core_term(
    tmp_path: Path,
    document_resources,
) -> None:
    """A missing reusable category becomes a described non-Core value."""
    store = MemoryGraphStore()
    glossary = _isolated_glossary(tmp_path)
    caller = FakeKnowledgeCaller(
        _response(
            entity_type="regulation",
            type_description="A binding rule issued by an authority.",
        )
    )
    service = document_importer(
        document_resources, store=store, caller=caller, glossary=glossary
    )

    result = await service.import_document(
        _docx("A new data regulation was issued."),
        authorization(),
        _knowledge_scope(store),
    )

    assert result.status == "success"
    term = glossary.resolve_term(ENTITY, "type", "regulation")
    assert term.description == "A binding rule issued by an authority."
    assert term.graph_labels == frozenset()
    entity = next(values for (spec, _), values in store.nodes.items() if spec is ENTITY)
    assert entity["type"] == "regulation"
    assert "type_description" not in entity


@pytest.mark.asyncio
async def test_ambiguous_candidates_are_skipped_without_leaking_names(
    document_resources,
) -> None:
    """Resolver embeddings are cached and public warnings omit document facts."""
    store = MemoryGraphStore()
    scope = _knowledge_scope(store)
    for name in ("Alpha", "Beta"):
        values = {
            "name": name,
            "canonical_name": name,
            "type": "product",
        }
        store.create_nodes(
            ENTITY,
            (
                {
                    **values,
                    "identity_hash": identity_hash(ENTITY, values),
                },
            ),
            scope=scope,
        )[0]
    response = _response()
    response["entities"] = [
        {
            **response["entities"][0],
            "ref": ref,
            "properties": {
                **response["entities"][0]["properties"],
                "name": name,
                "canonical_name": name,
            },
        }
        for ref, name in (("one", "Candidate One"), ("two", "Candidate Two"))
    ]
    response["events"] = []
    response["relationships"] = []
    caller = FakeKnowledgeCaller(response, resolution_decision="ambiguous")
    service = document_importer(document_resources, store=store, caller=caller)

    result = await service.import_document(
        _docx("Sensitive candidate names."),
        authorization(scope.user_id, scope.tenant_id, scope.workspace_id),
        scope,
    )

    assert result.status == "degraded"
    assert result.warnings
    assert "Candidate" not in result.model_dump_json()
    assert len(store.nodes) == 2


@pytest.mark.asyncio
async def test_semantic_batch_deduplication_merges_llm_confirmed_aliases(
    document_resources,
) -> None:
    """Different extracted names merge only after a finite LLM identity decision."""
    response = _response()
    response["entities"] = [
        response["entities"][0],
        {
            **response["entities"][0],
            "ref": "entity-alias",
            "properties": {
                **response["entities"][0]["properties"],
                "name": "示例助手聊天产品",
                "canonical_name": "示例助手聊天产品",
                "aliases": ["AcmeChat"],
            },
        },
    ]
    response["events"] = []
    response["relationships"] = []
    store = MemoryGraphStore()
    caller = FakeKnowledgeCaller(response, batch_decision="match")
    service = document_importer(document_resources, store=store, caller=caller)

    result = await service.import_document(
        _docx("AcmeChat is also known as the Example Assistant chat product."),
        authorization(),
        _knowledge_scope(store),
    )

    assert result.status == "success"
    assert len(store.nodes) == 1

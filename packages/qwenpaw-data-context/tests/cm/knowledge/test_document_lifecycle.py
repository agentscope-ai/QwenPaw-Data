"""Verify original-file and status persistence through the KG business entrypoint."""

from __future__ import annotations

import asyncio
from io import BytesIO

import pytest
from context_manager.auth import WriteOwner
from context_manager.knowledge import (
    KnowledgeImportService,
    UploadedDocument,
)
from context_manager.knowledge.document import DocumentStatus
from context_manager.knowledge.repository import DocumentRepository
from context_manager.model.usage import (
    UsageContext,
    bind_usage_context,
    current_usage_context,
)
from context_manager.schema import COMPOSITION, ENTITY, GRAPH_SCHEMA, HIERARCHY
from docx import Document

from cm.infrastructure.access.support import authorization
from cm.infrastructure.models.support import StaticModelService
from cm.infrastructure.storage.schema_support import make_glossary
from cm.knowledge.document_support import (
    download_document,
    make_document_resources,
)
from cm.knowledge.import_support import FakeKnowledgeCaller
from cm.knowledge.support import MemoryGraphStore


@pytest.fixture
def setup(tmp_path):
    graph = MemoryGraphStore()
    scope = graph.access.issue_write_scope(
        authorization(),
        owner=WriteOwner.USER,
        zone="knowledge",
    )
    documents = make_document_resources(
        tmp_path,
        tenant_id="tenant-1",
        workspace_id="workspace-1",
        user_id="user-1",
    )
    caller = FakeKnowledgeCaller(
        {
            "entities": [
                {
                    "ref": "entity-1",
                    "confidence": 0.95,
                    "properties": {
                        "name": "星舟数据助手",
                        "canonical_name": "星舟数据助手",
                        "type": "product",
                        "aliases": [],
                        "description": "帮助分析销售数据",
                        "lifecycle_state": "active",
                    },
                }
            ],
            "events": [],
            "relationships": [],
        }
    )
    models = StaticModelService(caller)
    importer = KnowledgeImportService(
        graph,
        make_glossary(GRAPH_SCHEMA),
        lambda user_id: models,
        **documents.import_arguments,
    )
    source = Document()
    source.add_paragraph("星舟数据助手能帮助我们分析销售数据。")
    buffer = BytesIO()
    source.save(buffer)
    upload = UploadedDocument("产品介绍.docx", None, buffer.getvalue())
    return graph, scope, documents, caller, importer, upload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relationship",
    [None, "COMPOSITION", "HIERARCHY"],
    ids=["omitted-edge", "same-edge", "different-edge-type"],
)
async def test_reingest_preserves_prior_edges_and_merges_by_full_edge_identity(
    setup, monkeypatch, relationship
):
    """Re-extraction merges facts; omission or another edge type does not retract them."""
    graph, scope, documents, caller, importer, _ = setup
    upload = UploadedDocument(
        "产品关系.txt",
        "text/plain",
        "星舟科技的产品包括星舟数据助手，它能帮助我们分析销售数据。".encode(),
    )
    caller.response["entities"].append(
        {
            "ref": "company-1",
            "confidence": 0.95,
            "properties": {
                "name": "星舟科技",
                "canonical_name": "星舟科技",
                "type": "company",
            },
        }
    )
    edge = {
        "source_ref": "company-1",
        "relationship": "COMPOSITION",
        "target_ref": "entity-1",
        "confidence": 0.95,
        "properties": {"description": "产品包括星舟数据助手"},
    }
    caller.response["relationships"] = [edge]
    first = await importer.import_document(upload, authorization(), scope)
    original = documents.repository.get(first.source_id)
    before_nodes = tuple(graph.list_nodes(ENTITY, scope=scope))
    (before_edge,) = graph.list_edges(COMPOSITION, scope=scope)

    caller.response["relationships"] = (
        []
        if relationship is None
        else [
            {
                **edge,
                "relationship": relationship,
                "properties": {"description": "第二轮抽取的关系描述"},
            }
        ]
    )
    repeated = await importer.reingest_document(first.source_id, authorization(), scope)

    assert repeated.status == "success" and repeated.source_id == first.source_id
    assert tuple(graph.list_nodes(ENTITY, scope=scope)) == before_nodes
    assert tuple(graph.list_edges(COMPOSITION, scope=scope)) == (before_edge,)
    assert len(documents.repository.list()) == 1
    current = documents.repository.get(first.source_id)
    assert current.metadata.status is DocumentStatus.READY
    assert current.metadata.created_at == original.metadata.created_at
    assert download_document(documents, first.source_id)[1] == upload.content
    if relationship == "HIERARCHY":
        (added,) = graph.list_edges(HIERARCHY, scope=scope)
        assert (added.source_key, added.target_key) == (
            before_edge.source_key,
            before_edge.target_key,
        )
        assert added.properties["description"] == "第二轮抽取的关系描述"
        assert len(graph.edges) == 2
    else:
        assert not graph.list_edges(HIERARCHY, scope=scope)
        assert len(graph.edges) == 1


@pytest.mark.asyncio
async def test_terminal_status_write_failure_does_not_claim_graph_rollback(
    setup, monkeypatch
):
    graph, scope, documents, _, importer, upload = setup
    set_status = DocumentRepository.set_status

    def fail_ready(self, document_id, status, **kwargs):
        if status is DocumentStatus.READY:
            raise OSError("disk unavailable after graph commit")
        return set_status(self, document_id, status, **kwargs)

    monkeypatch.setattr(DocumentRepository, "set_status", fail_ready)
    with pytest.raises(OSError, match="after graph commit"):
        await importer.import_document(upload, authorization(), scope)
    assert len(graph.nodes) == 1
    (document,) = documents.repository.list()
    assert document.metadata.status is DocumentStatus.FAILED
    assert "interrupted" in document.metadata.ingest_error
    assert download_document(documents, document.document_id)[1] == upload.content


@pytest.mark.asyncio
async def test_lost_execution_stops_extraction_and_original_can_be_retried(
    setup, monkeypatch
):
    from context_manager.knowledge.repository import DocumentLeaseLost
    from context_manager.settings import get_settings

    graph, scope, documents, _, importer, upload = setup
    monkeypatch.setattr(get_settings().heartbeat, "interval_seconds", 0.02)
    prepare = importer.prepare_plan
    cancelled = asyncio.Event()

    async def blocked(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with monkeypatch.context() as patch:
        patch.setattr(importer, "prepare_plan", blocked)
        patch.setattr(DocumentRepository, "renew", lambda *_: False)
        with pytest.raises(DocumentLeaseLost):
            await asyncio.wait_for(
                importer.import_document(upload, authorization(), scope), 5
            )
    assert cancelled.is_set()
    (failed,) = documents.repository.list()
    assert failed.metadata.status is DocumentStatus.FAILED
    assert not graph.nodes
    assert documents.files.read(upload.filename) == upload.content
    monkeypatch.setattr(importer, "prepare_plan", prepare)
    await importer.reingest_document(failed.document_id, authorization(), scope)
    assert (
        documents.repository.get(failed.document_id).metadata.status
        is DocumentStatus.READY
    )
    await importer.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reingest", [False, True])
async def test_document_usage_keeps_document_id_after_request_ends(
    setup, monkeypatch, reingest
):
    _, scope, documents, caller, importer, upload = setup
    if reingest:
        original = await importer.import_document(upload, authorization(), scope)
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []
    complete, embed = caller.complete, caller.embed

    async def observed_complete(*args, **kwargs):
        entered.set()
        await release.wait()
        seen.append(("llm", current_usage_context()))
        return await complete(*args, **kwargs)

    async def observed_embed(*args, **kwargs):
        seen.append(("embedding", current_usage_context()))
        return await embed(*args, **kwargs)

    monkeypatch.setattr(caller, "complete", observed_complete)
    monkeypatch.setattr(caller, "embed", observed_embed)
    parent = UsageContext(
        session_id="upload-session", chat_id="upload-chat", probe=True
    )
    before = current_usage_context()
    try:
        with bind_usage_context(parent):
            if reingest:
                accepted = await importer.start_reingest(
                    original.source_id, authorization(), scope
                )
            else:
                accepted = await importer.start_import(upload, authorization(), scope)
            assert current_usage_context() == parent
        await asyncio.wait_for(entered.wait(), 5)
    finally:
        release.set()
        await importer.aclose()
    assert current_usage_context() == before
    assert {kind for kind, _ in seen} == {"llm", "embedding"}
    assert all(
        usage
        == UsageContext(
            source="知识抽取", source_step=f"knowledge_extract#{accepted.document_id}"
        )
        for _, usage in seen
    )
    assert (
        documents.repository.get(accepted.document_id).metadata.status
        is DocumentStatus.READY
    )

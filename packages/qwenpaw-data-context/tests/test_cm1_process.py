"""Exercise the real shared processing contracts without provider I/O."""

import asyncio
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from context_manager.model import ChatMessage
from context_manager.process import (
    BatchExtractor,
    CandidateSearch,
    Chunker,
    ChunkExtractor,
    ChunkLimitExceeded,
    Deduplicator,
    EmbeddingEnricher,
    ResolutionKind,
    Resolver,
    ScoredCandidate,
    SemanticDecision,
    SemanticDecisionKind,
    SemanticJudge,
    TextChunk,
    build_embedding_text,
    complete_json,
)
from context_manager.schema import ENTITY, GRAPH_SCHEMA, USER, USER_MEMORY
from context_manager.store import NodeDraft, NodeRecord


def draft(ref="subject", **properties):
    return NodeDraft.build(
        GRAPH_SCHEMA, ENTITY, ref,
        {"name": "Qwen", "type": "product", "identity_hash": ref, **properties},
        transient={"source_ids": ["document-1"]},
    )


def embedder(vectors=((1.0, 0.0),)):
    return SimpleNamespace(embed=AsyncMock(return_value=vectors), aclose=AsyncMock())


def test_chunker_preserves_source_offsets_paragraphs_overlap_and_budget():
    text = "  " + "a" * 140 + "\n\n" + "b" * 160 + "\n" + "c" * 200 + "  "
    chunker = Chunker(max_chars=200, overlap_chars=20)
    chunks = chunker.chunk(text)
    assert chunks == chunker.chunk(text)
    assert chunks[0].text == "a" * 140
    assert all(chunk.text == text[chunk.start:chunk.end] for chunk in chunks)
    assert [chunk.index for chunk in chunks] == list(range(len(chunks)))
    assert chunks[1].start < chunks[0].end
    assert all(len(chunk.text) <= 200 for chunk in chunks)
    with pytest.raises(ChunkLimitExceeded):
        Chunker(max_chars=200, overlap_chars=0, max_chunks=1).chunk("x" * 201)
    with pytest.raises(ValueError):
        chunker.chunk(" \n\t")
    with pytest.raises(TypeError):
        Chunker(max_chars=True)
    with pytest.raises(ValueError):
        Chunker(overlap_chars=4000)
    with pytest.raises(ValueError):
        TextChunk(-1, "text", 0, 4)


async def test_batch_extraction_bounds_concurrency_and_retains_input_order():
    entered, release = asyncio.Event(), asyncio.Event()
    active = peak = 0
    order = []

    class Strategy:
        async def extract(self, chunk):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
                order.append(chunk.index)
                return chunk.index
            finally:
                active -= 1

    strategy = Strategy()
    assert isinstance(strategy, ChunkExtractor)
    chunks = tuple(TextChunk(i, str(i), i, i + 1) for i in range(5))
    task = asyncio.create_task(BatchExtractor(strategy, max_concurrency=2).extract(chunks))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert peak == 2 and not task.done()
    finally:
        release.set()
    assert await task == (0, 1, 2, 3, 4)
    assert sorted(order) == list(range(5)) and active == 0 and peak == 2


@pytest.mark.parametrize("cancel", [False, True])
async def test_batch_failure_or_cancellation_drains_every_child(cancel):
    entered, fail, finished = asyncio.Event(), asyncio.Event(), []

    class Strategy:
        async def extract(self, chunk):
            try:
                if chunk.index == 0:
                    await fail.wait()
                    raise ValueError("extraction failed")
                entered.set()
                await asyncio.Event().wait()
            finally:
                finished.append(chunk.index)

    chunks = (TextChunk(0, "a", 0, 1), TextChunk(1, "b", 1, 2))
    task = asyncio.create_task(BatchExtractor(Strategy(), max_concurrency=2).extract(chunks))
    await asyncio.wait_for(entered.wait(), 2)
    if cancel:
        task.cancel()
    else:
        fail.set()
    with pytest.raises(asyncio.CancelledError if cancel else ValueError):
        await task
    assert sorted(finished) == [0, 1]


async def test_complete_json_retries_only_parsing_and_schema_failures():
    caller = SimpleNamespace(complete=AsyncMock(side_effect=["not JSON", '{"count":"bad"}', '{"count":2}']))
    messages = (ChatMessage("system", "Extract counts."), ChatMessage("user", "Two."))
    schema = {"type": "object", "required": ["count"], "properties": {"count": {"type": "integer"}}}
    result = await complete_json(caller, messages, schema=schema, attempts=3, max_tokens=100, timeout_seconds=5)
    assert result == {"count": 2} and caller.complete.await_count == 3
    assert messages[0].content == "Extract counts."
    for call in caller.complete.await_args_list:
        assert call.kwargs == {"temperature": 0, "max_tokens": 100, "json_mode": True, "timeout_seconds": 5}
        assert call.args[0][0].content.startswith("Extract counts.\n\nThe response must match")
    caller.complete = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    with pytest.raises(RuntimeError):
        await complete_json(caller, messages, attempts=3)
    assert caller.complete.await_count == 1
    caller.complete = AsyncMock(return_value="[]")
    with pytest.raises(ValueError, match="after 2 attempts"):
        await complete_json(caller, messages, attempts=2)
    with pytest.raises(ValueError, match="positive"):
        await complete_json(caller, messages, attempts=0)
    caller.complete = AsyncMock(return_value='```json\n{"ok":true}\n```')
    assert await complete_json(caller, messages[1:], schema={"type": "object"}) == {"ok": True}
    assert caller.complete.await_args.args[0][0].role == "system"
    caller.complete = AsyncMock(side_effect=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await complete_json(caller, messages, attempts=2)
    assert caller.complete.await_count == 1


@pytest.mark.parametrize("kind", list(SemanticDecisionKind))
async def test_resolver_ranks_limits_and_validates_semantic_identity(kind):
    records = tuple(NodeRecord(ENTITY, {"key": key}) for key in ("low", "high", "mid"))
    candidates = tuple(ScoredCandidate(record.key, record, score) for record, score in zip(records, (0.1, 0.9, 0.8)))
    search = SimpleNamespace(search=AsyncMock(return_value=candidates))
    selected = ("high",) if kind is SemanticDecisionKind.MATCH else ()
    judge = SimpleNamespace(judge=AsyncMock(return_value=SemanticDecision(kind, selected, 0.95)))
    client = embedder()
    resolver = Resolver(embedder=client, build_semantic_text=lambda node: node.properties["name"],
                        candidate_search=search, identity_judge=judge, embedding_dimensions=2)
    subject = draft()
    result = await resolver.resolve(subject, search_limit=3, llm_candidate_limit=2)
    assert isinstance(search, CandidateSearch) and isinstance(judge, SemanticJudge)
    assert result.kind.value == kind.value and result.subject_embedding == (1.0, 0.0)
    assert result.confidence == 0.95
    assert [item.candidate_id for item in judge.judge.await_args.args[1]] == ["high", "mid"]
    client.embed.assert_awaited_once_with(("Qwen",), dimensions=2)
    search.search.assert_awaited_once_with(subject, (1.0, 0.0), limit=3)
    if kind is SemanticDecisionKind.MATCH:
        assert result.match is records[1]
    if kind is SemanticDecisionKind.AMBIGUOUS:
        assert result.candidates == (records[1], records[2])


async def test_resolver_rejects_duplicate_unknown_and_invalid_vectors_without_writes():
    record = NodeRecord(ENTITY, {"key": "candidate"})
    candidate = ScoredCandidate("candidate", record, 0.8)
    search = SimpleNamespace(search=AsyncMock(return_value=()))
    judge = SimpleNamespace(judge=AsyncMock(return_value=SemanticDecision("match", ("unknown",))))
    client = embedder()
    resolver = Resolver(embedder=client, build_semantic_text=lambda _: "text", candidate_search=search, identity_judge=judge)
    assert (await resolver.resolve(draft())).kind is ResolutionKind.NEW
    judge.judge.assert_not_awaited()
    search.search.return_value = (candidate, candidate)
    with pytest.raises(ValueError, match="duplicate"):
        await resolver.resolve(draft())
    search.search.return_value = (candidate,)
    with pytest.raises(ValueError, match="unknown"):
        await resolver.resolve(draft())
    for vectors in ((), ((float("nan"), 1.0),), ((True, 1.0),)):
        client.embed.return_value = vectors
        with pytest.raises((TypeError, ValueError)):
            await resolver.resolve(draft())
    with pytest.raises(ValueError):
        ScoredCandidate("candidate", record, float("inf"))
    with pytest.raises(ValueError):
        SemanticDecision("match", ())
    with pytest.raises(ValueError):
        SemanticDecision("ambiguous", ("candidate", "candidate"))
    with pytest.raises(ValueError):
        await resolver.resolve(draft(), search_limit=1, llm_candidate_limit=2)


async def test_embedding_enrichment_is_immutable_ordered_and_preserves_attribution():
    first = draft(name=" Qwen ", aliases=[" alias ", " "])
    empty = NodeDraft.build(GRAPH_SCHEMA, USER_MEMORY, "empty", {"id": "empty", "description": " "})
    plain = NodeDraft.build(GRAPH_SCHEMA, USER, "user", {"user_id": "alice"})
    client = embedder()
    nodes = (first, empty, plain)
    enriched = await EmbeddingEnricher(client).enrich(nodes)
    text = '{"name":"Qwen","aliases":["alias"],"type":"product"}'
    assert build_embedding_text(ENTITY, first.properties) == text
    client.embed.assert_awaited_once_with((text,))
    assert enriched[0].properties["embedding"] == (1.0, 0.0)
    assert enriched[0].properties["embedding_hash"] == sha256(text.encode()).hexdigest()
    assert enriched[0].transient["source_ids"] == ("document-1",)
    assert "embedding" not in first.properties and first.properties["name"] == " Qwen "
    assert enriched[1].properties["embedding"] is None and enriched[1].properties["embedding_hash"] is None
    assert enriched[2] is plain
    prepared, pending = EmbeddingEnricher.prepare(nodes)
    for vectors in ((), ((float("nan"), 0.0),), ((1.0, 0.0), (0.0, 1.0))):
        with pytest.raises((ValueError, TypeError)):
            EmbeddingEnricher.apply(prepared, pending, vectors)
    with pytest.raises(TypeError):
        build_embedding_text(ENTITY, {"aliases": "not a list"})
    no_model = embedder()
    assert await EmbeddingEnricher(no_model).enrich((plain,)) == (plain,)
    no_model.embed.assert_not_awaited()
    policy = SimpleNamespace(deduplicate=AsyncMock(return_value=(first,)))
    assert isinstance(policy, Deduplicator)
    assert await policy.deduplicate(nodes) == (first,)

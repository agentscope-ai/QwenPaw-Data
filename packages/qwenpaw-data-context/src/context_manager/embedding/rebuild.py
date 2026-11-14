"""Prepare authorized graph embeddings and replace them in bounded transactions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from ..auth import ScopeBinding
from ..executor import BoundedExecutor
from ..model import TextEmbedder
from ..process import EmbeddingEnricher
from ..schema import NodeSpec
from ..settings import get_settings
from ..store import GraphStore, NodeDraft, NodeRecord
from ..transaction import StoreOperation, Transaction, TransactionPlan
from ..transaction.recovery import RecoveryStackSession
from ..utils.batches import grouped

ProgressCallback = Callable[[dict[str, object]], Awaitable[None]]


def _inputs(node: NodeRecord) -> dict[str, object]:
    return {name: node.properties.get(name) for name in node.spec.embedding_fields}


def _prepare_embeddings(enricher, spec, nodes):
    drafts = tuple(
        NodeDraft(spec, f"{spec.name}:{node.key}", _inputs(node)) for node in nodes
    )
    return enricher.prepare(drafts)


def _prepare_updates(enricher, nodes, prepared, pending, vectors, dimensions):
    enriched_nodes = enricher.apply(prepared, pending, vectors)
    if vectors:
        batch_dimensions = len(vectors[0])
        if dimensions is not None and batch_dimensions != dimensions:
            raise ValueError("embeddings have inconsistent dimensions across labels")
        dimensions = batch_dimensions
    updates = []
    for node, enriched in zip(nodes, enriched_nodes, strict=True):
        updates.append(
            (
                node,
                {
                    "embedding": enriched.properties["embedding"],
                    "embedding_hash": enriched.properties["embedding_hash"],
                },
            )
        )
    return updates, dimensions


def _scan(
    store: GraphStore, scope: ScopeBinding, spec: NodeSpec, page_size: int, offset: int
):
    """Read only the next transaction's nodes, with stable key ordering."""
    return tuple(
        store.list_nodes(
            spec,
            filters={"owner_id": scope.write_owner_id} if scope.write_owner_id else {},
            limit=page_size,
            offset=offset,
            fields=("owner_id", *spec.embedding_fields),
            scope=scope,
        )
    )


def _plan_updates(scope, updates, write_scope_for_owner):
    operations, scopes = [], []
    for (spec, owner), entries in grouped(
        updates, key=lambda item: (item[0].spec, item[0].properties["owner_id"])
    ):
        operations.append(
            StoreOperation(
                "patch_nodes",
                {
                    "spec": spec,
                    "patches": tuple((node.key, changes) for node, changes in entries),
                },
            )
        )
        scopes.append(write_scope_for_owner(owner) if write_scope_for_owner else scope)
    return TransactionPlan(tuple(operations)), tuple(scopes)


async def embed_all(
    store: GraphStore,
    scope: ScopeBinding,
    embedder: TextEmbedder,
    *,
    executor: BoundedExecutor,
    page_size: int = 500,
    on_progress: ProgressCallback | None = None,
    write_scope_for_owner: Callable[[str], ScopeBinding] | None = None,
    recovery_session: RecoveryStackSession | None = None,
) -> int:
    """Re-embed authorized text fields across graph zones.

    Read, prepare and commit one bounded page before reading the next.
    Every invocation recomputes vectors, including matching text hashes.
    Workspace rebuilds supply an exact-owner write scope factory.
    """
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError("page_size must be an integer from 1 to 1000")
    enricher = EmbeddingEnricher(embedder)
    specs = tuple(
        spec
        for spec in store.schema.nodes
        if spec.embedding_fields
        and (scope.zone is None or spec.zone.value == scope.zone)
    )

    async def progress(phase: str, label: str = "", done: int = 0) -> None:
        if on_progress is not None:
            await on_progress(
                {
                    "phase": phase,
                    "current_label": label,
                    "labels_done": done,
                    "labels_total": len(specs),
                }
            )

    execute = (
        recovery_session.execute
        if recovery_session is not None
        else Transaction(store).execute
    )
    dimensions: int | None = None
    updated = 0
    # Freeze the deployment budget for this run; page_size can only narrow it.
    limit = min(page_size, get_settings().graph.write_batch_max_rows)
    for index, spec in enumerate(specs):
        offset = 0

        async def rebuild_page():
            nonlocal dimensions
            await progress("scanning", spec.name, index)
            nodes = await executor.run_sync(_scan, store, scope, spec, limit, offset)
            if not nodes:
                return 0, 0
            await progress("embedding", spec.name, index)
            prepared, pending = await executor.run_sync(
                _prepare_embeddings, enricher, spec, nodes
            )
            vectors = (
                await embedder.embed(tuple(text for _, text in pending))
                if pending
                else ()
            )
            batch, dimensions = await executor.run_sync(
                _prepare_updates,
                enricher,
                nodes,
                prepared,
                pending,
                vectors,
                dimensions,
            )
            await progress("committing", spec.name, index)
            plan, scopes = await executor.run_sync(
                _plan_updates, scope, batch, write_scope_for_owner
            )
            result = await executor.run_sync(execute, plan, scopes)
            return len(nodes), result.stats["nodes"]["updates_submitted"]

        while True:
            size, count = await executor.run(rebuild_page)
            updated += count
            offset += size
            if size < limit:
                break
    await progress("committing", done=len(specs))
    return updated


__all__ = ["embed_all"]

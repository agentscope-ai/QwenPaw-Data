"""Retract a document's Knowledge sources before removing its original."""

from collections.abc import Awaitable, Callable, Iterator
import asyncio

from ..schema import ENTITY, EVENT, EdgeSpec, NodeSpec
from ..auth import AccessMode, ScopeBinding
from ..store import (
    EdgeRecord,
    GraphStore,
    NodeRecord,
)
from ..transaction import (
    StoreOperation,
    TransactionPlan,
    execute_transaction_plan,
)
from .document import DocumentStatus
from .execution import DocumentExecution
from .files import DocumentFileStore
from .repository import DocumentLeaseLost, DocumentRepository


class KnowledgeDeleteService:
    """Remove unclaimed KG nodes and incident edges through Store primitives."""

    def __init__(
        self,
        store: GraphStore,
        *,
        documents: DocumentRepository,
        files: DocumentFileStore,
        run_sync: Callable[..., Awaitable],
    ) -> None:
        self._store = store
        self._run_sync = run_sync
        self._documents = documents
        self._files = files

    async def delete_document(self, document_id: str, scope: ScopeBinding) -> None:
        """Commit graph retraction, then remove the original and status record."""
        self._store.access.require(scope, AccessMode.WRITE)
        if (
            scope.write_owner_id != scope.user_id
            or scope.zone is not None
            or scope.datasource_id is not None
            or (scope.tenant_id, scope.workspace_id, scope.user_id)
            != self._documents.target
        ):
            raise PermissionError(
                "knowledge deletion requires the document owner's cross-zone scope"
            )
        execution = DocumentExecution(self._documents, document_id)
        await execution.claim()
        async with execution:
            document = await execution.call(
                self._documents.set_status,
                document_id,
                DocumentStatus.DELETING,
                token=execution.token,
            )
            try:
                plan = await self._run_sync(self._prepare_plan, document_id, scope)
                await execution.call(
                    self._documents.check_execution, document_id, execution.token
                )
                await self._run_sync(execute_transaction_plan, self._store, plan, scope)
                # TODO: Fence graph/file effects and reconcile them with catalog updates.
                await execution.call(
                    self._documents.check_execution, document_id, execution.token
                )
                await self._run_sync(self._files.delete, document.metadata.filename)
                await execution.call(
                    self._documents.delete,
                    document_id,
                    token=execution.token,
                    status=document.metadata.status.value,
                )
            except (Exception, asyncio.CancelledError):
                try:
                    await execution.call(
                        self._documents.set_status,
                        document_id,
                        DocumentStatus.DELETING,
                        error="document deletion failed; retry deletion",
                        token=execution.token,
                    )
                except DocumentLeaseLost:
                    pass  # A replacement execution owns the catalog now.
                raise

    def _prepare_plan(self, document_id: str, scope: ScopeBinding) -> TransactionPlan:
        """Keep shared nodes and explicitly remove all edges of unclaimed nodes."""
        operations = []
        doomed: dict[NodeSpec, set[str]] = {}
        for spec in (ENTITY, EVENT):
            patches = []
            keys = set()
            for node in self._list(spec, scope):
                sources = node.properties.get("source_ids") or ()
                if document_id not in sources:
                    continue
                remaining = tuple(source for source in sources if source != document_id)
                if remaining:
                    patches.append((node.key, {"source_ids": remaining}))
                else:
                    keys.add(node.key)
            if patches:
                operations.append(
                    StoreOperation(
                        "patch_nodes",
                        {
                            "spec": spec,
                            "patches": tuple(patches),
                        },
                    )
                )
            if keys:
                doomed[spec] = keys
        for spec in self._store.schema.edges:
            if spec.source not in doomed and spec.target not in doomed:
                continue
            endpoints = tuple(
                (edge.source_key, edge.target_key)
                for edge in self._list(spec, scope)
                if edge.source_key in doomed.get(spec.source, ())
                or edge.target_key in doomed.get(spec.target, ())
            )
            if endpoints:
                operations.append(
                    StoreOperation(
                        "delete_edges",
                        {
                            "spec": spec,
                            "endpoints": endpoints,
                        },
                    )
                )
        for spec, keys in doomed.items():
            operations.append(
                StoreOperation(
                    "delete_nodes",
                    {
                        "spec": spec,
                        "keys": tuple(sorted(keys)),
                    },
                )
            )
        return TransactionPlan(tuple(operations))

    def _list(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
    ) -> Iterator[NodeRecord | EdgeRecord]:
        """Read every scoped page before graph writes change pagination offsets."""
        read = (
            self._store.list_nodes
            if isinstance(spec, NodeSpec)
            else self._store.list_edges
        )
        offset = 0
        while page := read(spec, limit=100, offset=offset, scope=scope):
            yield from page
            offset += len(page)


__all__ = ["KnowledgeDeleteService"]

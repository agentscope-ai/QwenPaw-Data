"""Import user documents through file persistence and atomic Knowledge graph plans."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from ..utils.batches import grouped
from pathlib import Path
from typing import cast

from qwenpaw_data_auth.authorization import AuthorizationContext
from sqlalchemy.engine import Engine

from ..executor import BoundedExecutor
from ..model.usage import bind_usage_context
from ..model import LlmError, ModelService, TextEmbedder
from ..process import (
    BatchExtractor,
    Chunker,
    ChunkLimitExceeded,
    Deduplicator,
    EmbeddingEnricher,
    ResolutionKind,
    Resolver,
    TextChunk,
    build_embedding_text,
)
from ..schema import (
    ENTITY,
    EVENT,
    GRAPH_SCHEMA,
    EdgeSpec,
    NodeSpec,
    PropertyOwner,
    Glossary,
    Zone,
)
from ..auth import AccessMode, ScopeBinding, require_target
from ..settings import get_settings
from ..store import (
    EdgeDraft,
    GraphStore,
    NodeDraft,
    NodeRecord,
    StoreError,
)
from ..transaction import (
    StoreOperation,
    TransactionPlan,
    execute_transaction_plan,
)
from ..utils.validation import require_integer, require_trimmed_text
from ..utils.values import is_empty_value, text_members
from .document import (
    Document,
    DocumentContentMismatchError,
    DocumentMetadata,
    DocumentStatus,
)
from .execution import DocumentExecution
from .files import DocumentFileStore
from .repository import DocumentLeaseLost, DocumentRepository
from .deletion import KnowledgeDeleteService
from .algo.contract import (
    KnowledgeImportError,
    KnowledgeImportErrorCode,
    KnowledgeImportResult,
    KnowledgeNodeSeed,
    UploadedDocument,
)
from .algo.deduplicator import (
    KnowledgeDeduplicator,
    deduplication_status,
    identity_hash,
    judge_document,
)
from .algo.document_reader import DocumentLimits, read_document, validate_upload
from .algo.extraction import KnowledgeChunkExtractor
from .algo.judge import KnowledgeSemanticJudge
from .algo.search import KnowledgeCandidateSearch, node_record_document


@dataclass(frozen=True, slots=True)
class KnowledgeImportLimits:
    """Bound synchronous extraction, resolution, and one write transaction."""

    document: DocumentLimits
    max_concurrency: int
    chunk_chars: int = 4000
    chunk_overlap: int = 400
    max_chunks: int = 64
    max_nodes: int = 200
    max_edges: int = 400
    candidate_limit: int = 500
    embedding_dimensions: int | None = None

    def __post_init__(self) -> None:
        """Reject limits that could bypass process and Store bounds."""
        if type(self.document) is not DocumentLimits:
            raise TypeError("document must be DocumentLimits")
        for name, lower, upper in (
            ("chunk_chars", 200, 20_000),
            ("chunk_overlap", 0, self.chunk_chars - 1),
            ("max_chunks", 1, 256),
            ("max_concurrency", 1, 32),
            ("max_nodes", 1, 1000),
            ("max_edges", 0, 2000),
            ("candidate_limit", 1, 999),
        ):
            value = require_integer(
                getattr(self, name),
                name,
                minimum=lower,
                maximum=upper,
            )
            object.__setattr__(self, name, value)
        if self.chunk_overlap > self.chunk_chars // 2:
            raise ValueError("chunk_overlap cannot exceed half of chunk_chars")
        if self.embedding_dimensions is not None and self.embedding_dimensions <= 0:
            raise ValueError("embedding_dimensions must be positive")


@dataclass(frozen=True, slots=True)
class KnowledgeImportPlan:
    """Hold scope-bound facts and Store operations prepared by the Service."""

    source_id: str
    tenant_id: str
    workspace_id: str
    user_id: str = field(kw_only=True)  # Owner used during resolution and execution.
    nodes: tuple[NodeDraft, ...]
    edges: tuple[EdgeDraft, ...]
    warnings: tuple[str, ...] = ()
    transaction: TransactionPlan = field(
        kw_only=True
    )  # Prepared Store invocations in execution order.

    def __post_init__(self) -> None:
        """Freeze prepared data and validate the closed draft graph."""
        for name in ("source_id", "tenant_id", "workspace_id", "user_id"):
            object.__setattr__(
                self, name, require_trimmed_text(getattr(self, name), name)
            )
        nodes, edges, warnings = (
            tuple(self.nodes),
            tuple(self.edges),
            tuple(self.warnings),
        )
        if any(type(node) is not NodeDraft for node in nodes):
            raise TypeError("plan nodes must contain NodeDraft values")
        if any(type(edge) is not EdgeDraft for edge in edges):
            raise TypeError("plan edges must contain EdgeDraft values")
        nodes_by_ref: dict[str, NodeDraft] = {}
        identities: set[tuple[NodeSpec, str]] = set()
        for node in nodes:
            if node.spec not in {ENTITY, EVENT}:
                raise TypeError("plan nodes must contain only Entity or Event drafts")
            values = node.properties
            try:
                require_trimmed_text(values.get("identity_hash"), "node identity_hash")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "plan nodes must contain a non-empty identity_hash"
                ) from exc
            if node.ref in nodes_by_ref:
                raise ValueError("plan nodes must have unique refs")
            identity = (node.spec, values["identity_hash"])
            if identity in identities:
                raise ValueError("plan nodes must have unique business identities")
            identities.add(identity)
            nodes_by_ref[node.ref] = node
        edge_keys: set[tuple[EdgeSpec, str, str]] = set()
        for edge in edges:
            source = nodes_by_ref.get(edge.source_ref)
            target = nodes_by_ref.get(edge.target_ref)
            if source is None or target is None:
                raise ValueError("plan edge endpoint ref must resolve to a plan node")
            if (
                source.spec is not edge.spec.source
                or target.spec is not edge.spec.target
            ):
                raise ValueError("plan edge endpoint specs do not match its EdgeSpec")
            edge_key = (edge.spec, edge.source_ref, edge.target_ref)
            if edge_key in edge_keys:
                raise ValueError("plan edges cannot contain duplicates")
            edge_keys.add(edge_key)
        if any(not isinstance(warning, str) or not warning for warning in warnings):
            raise ValueError("plan warnings must contain non-empty text")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "warnings", warnings)
        if type(self.transaction) is not TransactionPlan:
            raise TypeError("plan transaction must be a TransactionPlan")


class KnowledgeImportService:
    """Share Workspace import tasks while acquiring user dependencies per operation."""

    @property
    def document_limits(self) -> DocumentLimits:
        """Expose the same document budget used by import admission and parsing."""
        return self._limits.document

    def __init__(
        self,
        store: GraphStore,
        glossary: Glossary,
        models: Callable[[str], ModelService],
        *,
        executor: BoundedExecutor,
        database_engine: Engine,
        home: Path,
        tenant_id: str,
        workspace_id: str,
        limits: KnowledgeImportLimits | None = None,
    ) -> None:
        """Bind canonical infrastructure and bounded import policy."""
        if not isinstance(store, GraphStore):
            raise TypeError("store must implement GraphStore")
        if type(glossary) is not Glossary or glossary.schema is not store.schema:
            raise ValueError("glossary must govern the Store schema")
        if limits is None:
            settings = get_settings().knowledge
            limits = KnowledgeImportLimits(
                document=DocumentLimits(
                    max_bytes=settings.max_upload_bytes,
                    max_pages=settings.max_document_pages,
                    max_chars=settings.max_document_chars,
                ),
                max_concurrency=settings.max_concurrency,
            )
        resolved_limits = limits
        if type(resolved_limits) is not KnowledgeImportLimits:
            raise TypeError("limits must be KnowledgeImportLimits")
        self._store = store
        self._executor = executor
        self._glossary = glossary
        self._models = models
        self._engine = database_engine
        self._home = home
        self._tenant_id = tenant_id
        self._workspace_id = workspace_id
        self._limits = resolved_limits
        self._imports: set[asyncio.Task[KnowledgeImportResult]] = set()
        self._closed = False
        self._chunker = Chunker(
            max_chars=resolved_limits.chunk_chars,
            overlap_chars=resolved_limits.chunk_overlap,
            max_chunks=resolved_limits.max_chunks,
        )

    def document_repository(self, context: AuthorizationContext) -> DocumentRepository:
        """Create catalog access for this Workspace's authenticated user."""
        require_target(
            context, tenant_id=self._tenant_id, workspace_id=self._workspace_id
        )
        return DocumentRepository(
            self._engine,
            tenant_id=self._tenant_id,
            workspace_id=self._workspace_id,
            user_id=context.user_id,
        )

    def document_files(self, context: AuthorizationContext) -> DocumentFileStore:
        """Create original-file access for the authenticated user."""
        require_target(
            context, tenant_id=self._tenant_id, workspace_id=self._workspace_id
        )
        return DocumentFileStore(
            self._home,
            tenant_id=self._tenant_id,
            workspace_id=self._workspace_id,
            user_id=context.user_id,
        )

    def deleter(self, context: AuthorizationContext) -> KnowledgeDeleteService:
        """Compose one deletion using the importer's document coordination."""
        return KnowledgeDeleteService(
            self._store,
            documents=self.document_repository(context),
            files=self.document_files(context),
            run_sync=self._executor.run,
        )

    async def import_document(
        self,
        upload: UploadedDocument,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> KnowledgeImportResult:
        """Save the original, import KG facts, and persist the terminal document state."""
        _, task = await self._start_import(upload, context, scope)
        return await task

    async def start_import(
        self,
        upload: UploadedDocument,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> Document:
        """Persist acceptance before returning; retain the background task until shutdown."""
        document, _ = await self._start_import(upload, context, scope)
        return document

    async def _start_import(
        self,
        upload: UploadedDocument,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> tuple[Document, asyncio.Task[KnowledgeImportResult]]:
        self._require_document_scope(context, scope)
        validate_upload(upload, limits=self._limits.document)
        await self._executor.run(self._require_models, context)
        now = datetime.now(UTC)
        document = await self._executor.run(
            Document.from_content,
            upload.content,
            DocumentMetadata(
                filename=upload.filename,
                content_type=upload.content_type,
                byte_size=len(upload.content),
                status=DocumentStatus.PROCESSING,
                created_at=now,
                updated_at=now,
            ),
        )
        return await self._schedule_import(upload, document, context, scope)

    async def _schedule_import(
        self,
        upload: UploadedDocument | None,
        document: Document,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> tuple[Document, asyncio.Task[KnowledgeImportResult]]:
        """Retain an accepted import until completion and report background errors."""
        accepted = asyncio.get_running_loop().create_future()
        self._require_open()
        task = asyncio.create_task(
            self._import_document(upload, document, context, scope, accepted)
        )
        self._imports.add(task)

        def finished(completed: asyncio.Task[KnowledgeImportResult]) -> None:
            self._imports.discard(completed)
            if not completed.cancelled():
                error = completed.exception()
                if (
                    error is not None
                    and not accepted.cancelled()
                    and accepted.exception() is None
                ):
                    logging.getLogger(__name__).error(
                        "background document import failed",
                        exc_info=error,
                    )

        task.add_done_callback(finished)
        return await accepted, task

    async def start_reingest(
        self,
        document_id: str,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> Document:
        """Start a fresh import from the current user's retained original."""
        document, _ = await self._start_reingest(document_id, context, scope)
        return document

    async def _start_reingest(
        self,
        document_id: str,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> tuple[Document, asyncio.Task[KnowledgeImportResult]]:
        self._require_document_scope(context, scope)
        await self._executor.run(self._require_models, context)
        document = await self._executor.run(
            self.document_repository(context).get, document_id
        )
        return await self._schedule_import(None, document, context, scope)

    def _load_reingest(
        self,
        document_id: str,
        documents: DocumentRepository,
        files: DocumentFileStore,
    ) -> tuple[Document, UploadedDocument]:
        """Validate the retained original after acquiring its execution claim."""
        document = documents.get(document_id)
        if document.metadata.status is DocumentStatus.DELETING:
            raise FileExistsError("doc_delete_in_progress")
        content = files.read(document.metadata.filename)
        if hashlib.sha256(content).hexdigest() != document.content_sha256:
            raise DocumentContentMismatchError(
                "document content does not match its identity"
            )
        upload = UploadedDocument(
            document.metadata.filename, document.metadata.content_type, content
        )
        validate_upload(upload, limits=self._limits.document)
        return document, upload

    async def aclose(self) -> None:
        """Stop admission and finish accepted imports before Store shutdown."""
        self._closed = True
        tasks = tuple(self._imports)
        await asyncio.gather(*tasks, return_exceptions=True)

    def _require_open(self) -> None:
        if self._closed:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.UNAVAILABLE, "knowledge import is closed"
            )

    def _require_models(self, context: AuthorizationContext) -> ModelService:
        models = self._models(context.user_id)
        if not models.ready:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.UNAVAILABLE, "knowledge import is unavailable"
            )
        return models

    def _persist_document(
        self,
        document: Document,
        content: bytes,
        documents: DocumentRepository,
        files: DocumentFileStore,
        *,
        token: str,
    ) -> Document:
        """Roll back the catalog and this upload's original if either write fails."""
        created = False
        try:
            with documents.transaction() as connection:
                documents.create(connection, document, token=token)
                files.write(document.metadata.filename, content)
                created = True
        except BaseException:
            if created:
                files.delete(document.metadata.filename)
            raise
        return document

    async def _import_document(
        self,
        upload: UploadedDocument | None,
        document: Document,
        context: AuthorizationContext,
        scope: ScopeBinding,
        accepted: asyncio.Future,
    ) -> KnowledgeImportResult:
        with bind_usage_context(
            source="知识抽取",
            source_step=f"knowledge_extract#{document.document_id}",
            session_id="",
            chat_id="",
            probe=False,
        ):
            try:
                documents, files = (
                    self.document_repository(context),
                    self.document_files(context),
                )
                execution = DocumentExecution(documents, document.document_id)
                await execution.claim()
                async with execution:
                    if upload is None:
                        document, upload = await self._executor.run(
                            self._load_reingest, document.document_id, documents, files
                        )
                        document = await execution.call(
                            documents.set_status,
                            document.document_id,
                            DocumentStatus.PROCESSING,
                            token=execution.token,
                        )
                    else:
                        document = await self._executor.run(
                            self._persist_document,
                            document,
                            upload.content,
                            documents,
                            files,
                            token=execution.token,
                        )
                    if not accepted.done():
                        accepted.set_result(document)
                    try:

                        async def work():
                            plan = await self.prepare_plan(
                                upload, context, scope, source_id=document.document_id
                            )
                            await execution.call(
                                documents.check_execution,
                                document.document_id,
                                execution.token,
                            )
                            write = asyncio.create_task(self.execute_plan(plan, scope))
                            try:
                                result = await asyncio.shield(write)
                            except asyncio.CancelledError:
                                await write
                                raise
                            return result

                        result = await self._executor.run(work)
                    except (Exception, asyncio.CancelledError) as exc:
                        if isinstance(exc, KnowledgeImportError):
                            error = str(exc)
                        elif isinstance(exc, asyncio.CancelledError):
                            error = "document import was cancelled"
                        else:
                            error = "document import failed"
                        try:
                            await execution.call(
                                documents.set_status,
                                document.document_id,
                                DocumentStatus.FAILED,
                                error=error,
                                token=execution.token,
                            )
                        except DocumentLeaseLost:
                            pass  # A replacement execution owns the catalog now.
                        raise
                    # TODO: Coordinate graph commit and catalog confirmation across stores.
                    await execution.call(
                        documents.set_status,
                        document.document_id,
                        DocumentStatus.DEGRADED
                        if result.status == "degraded"
                        else DocumentStatus.READY,
                        token=execution.token,
                    )
                    return result
            except BaseException as exc:
                if not accepted.done():
                    accepted.set_exception(exc)
                raise

    async def reingest_document(
        self,
        document_id: str,
        context: AuthorizationContext,
        scope: ScopeBinding,
    ) -> KnowledgeImportResult:
        """Load the retained original and run a new import for the same user."""
        _, task = await self._start_reingest(document_id, context, scope)
        return await task

    def _read_chunks(self, upload: UploadedDocument):
        """Parse and split the document as one synchronous preparation step."""
        parsed = read_document(upload, limits=self._limits.document)
        try:
            chunks = self._chunker.chunk(parsed.text)
        except ChunkLimitExceeded as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE,
                "document produces too many processing chunks",
            ) from exc
        return parsed, chunks

    async def prepare_plan(
        self,
        upload: UploadedDocument,
        context: AuthorizationContext,
        scope: ScopeBinding,
        *,
        source_id: str | None = None,
    ) -> KnowledgeImportPlan:
        """Validate scope, snapshot clients, and prepare input and algorithms."""
        if type(upload) is not UploadedDocument:
            raise TypeError("upload must be an UploadedDocument")
        self._require_document_scope(context, scope)
        models = await self._executor.run_sync(self._require_models, context)
        async with models.open() as (llm, embedder):
            batch_extractor = BatchExtractor(
                KnowledgeChunkExtractor(
                    llm, self._glossary, run_sync=self._executor.run_sync
                ),
                max_concurrency=self._limits.max_concurrency,
            )
            deduplicator: Deduplicator = KnowledgeDeduplicator(
                llm,
                embedder,
                embedding_dimensions=self._limits.embedding_dimensions,
                run_sync=self._executor.run_sync,
            )
            resolution_judge = KnowledgeSemanticJudge[NodeRecord](
                llm,
                mode="graph_resolution",
                subject_document=judge_document,
                candidate_document=node_record_document,
            )
            parsed, chunks = await self._executor.run_sync(self._read_chunks, upload)
            resolver = Resolver(
                embedder=embedder,
                build_semantic_text=lambda node: build_embedding_text(
                    node.spec, node.properties
                ),
                candidate_search=KnowledgeCandidateSearch(
                    self._store,
                    scope,
                    embedder,
                    run_sync=self._executor.run_sync,
                    candidate_limit=self._limits.candidate_limit,
                    dimensions=self._limits.embedding_dimensions,
                ),
                identity_judge=resolution_judge,
                embedding_dimensions=self._limits.embedding_dimensions,
            )
            try:
                return await self._build_plan(
                    # Reingest keeps the persisted document's ID, including older originals.
                    source_id if source_id is not None else parsed.source_id,
                    chunks,
                    batch_extractor,
                    deduplicator,
                    resolver,
                    embedder,
                    scope=scope,
                )
            except LlmError as exc:
                raise KnowledgeImportError(
                    KnowledgeImportErrorCode.EXTRACTION_FAILED,
                    "knowledge embedding request failed",
                ) from exc
            except StoreError as exc:
                raise KnowledgeImportError(
                    KnowledgeImportErrorCode.WRITE_FAILED,
                    "knowledge graph candidate read failed",
                ) from exc
            except (TypeError, ValueError) as exc:
                raise KnowledgeImportError(
                    KnowledgeImportErrorCode.EXTRACTION_INVALID,
                    "knowledge semantic processing returned an invalid decision",
                ) from exc

    async def execute_plan(
        self,
        plan: KnowledgeImportPlan,
        scope: ScopeBinding,
    ) -> KnowledgeImportResult:
        """Execute one prepared plan through transaction-bound Store primitives."""
        if type(plan) is not KnowledgeImportPlan:
            raise TypeError("plan must be a KnowledgeImportPlan")
        self._require_write_scope(scope)
        if (scope.tenant_id, scope.workspace_id, scope.user_id) != (
            plan.tenant_id,
            plan.workspace_id,
            plan.user_id,
        ):
            raise PermissionError("knowledge import plan belongs to another scope")
        try:
            result = await self._executor.run_sync(
                execute_transaction_plan,
                self._store,
                plan.transaction,
                scope,
            )
        except Exception as exc:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.WRITE_FAILED,
                "knowledge graph write failed",
            ) from exc
        return KnowledgeImportResult(
            source_id=plan.source_id,
            status="degraded" if plan.warnings else "success",
            seed_nodes=[
                KnowledgeNodeSeed(
                    node=node.spec.name,
                    identity={
                        "key": node.transient.get("existing_key")
                        or result.node_keys[node.ref],
                    },
                )
                for node in plan.nodes[:20]
            ],
            warnings=list(plan.warnings),
        )

    async def _build_plan(
        self,
        source_id: str,
        chunks: Sequence[TextChunk],
        extractor: BatchExtractor[tuple[NodeDraft | EdgeDraft, ...]],
        deduplicator: Deduplicator,
        resolver: Resolver,
        embedder: TextEmbedder,
        *,
        scope: ScopeBinding,
    ) -> KnowledgeImportPlan:
        """Extract, deduplicate, resolve, merge, enrich, and compile final facts."""
        extractions = await extractor.extract(chunks)
        drafts, ambiguous_drafts, relationships = await self._drafts(
            extractions,
            deduplicator,
        )
        warnings: list[str] = []
        resolved_by_origin: dict[str, NodeDraft | None] = {}
        resolved_nodes: list[NodeDraft] = []
        positions: dict[tuple[NodeSpec, str], int] = {}
        for draft in ambiguous_drafts:
            warnings.append(f"ambiguous {draft.spec.name} candidate was skipped")
            for origin in cast(tuple[str, ...], draft.transient["origin_refs"]):
                resolved_by_origin[origin] = None
        for draft in drafts:
            resolution = await resolver.resolve(
                draft,
            )
            if resolution.kind is ResolutionKind.AMBIGUOUS:
                warnings.append(f"ambiguous {draft.spec.name} candidate was skipped")
                resolved = None
            else:
                existing = resolution.match
                if existing is None:
                    # Exact replay identity is independent of semantic recall.
                    matches = tuple(
                        await self._executor.run_sync(
                            self._store.list_nodes,
                            draft.spec,
                            filters={
                                "identity_hash": identity_hash(
                                    draft.spec, draft.properties
                                )
                            },
                            limit=2,
                            scope=scope,
                        )
                    )
                    if len(matches) > 1:
                        raise ValueError(
                            "knowledge identity resolves to multiple nodes"
                        )
                    existing = matches[0] if matches else None
                resolved = self._finalize_node(
                    draft,
                    existing,
                    source_id=source_id,
                )
                key = (resolved.spec, resolved.properties["identity_hash"])
                position = positions.get(key)
                if position is None:
                    positions[key] = len(resolved_nodes)
                    resolved_nodes.append(resolved)
                else:
                    canonical = resolved_nodes[position]
                    resolved = replace(
                        canonical,
                        properties={
                            **canonical.properties,
                            **self._merge_node_properties(resolved, canonical),
                        },
                    )
                    resolved_nodes[position] = resolved
            for origin in cast(tuple[str, ...], draft.transient["origin_refs"]):
                resolved_by_origin[origin] = resolved
        prepared, pending = await self._executor.run_sync(
            EmbeddingEnricher.prepare, resolved_nodes
        )
        vectors = (
            await embedder.embed(tuple(text for _, text in pending)) if pending else ()
        )
        return await self._executor.run_sync(
            self._compile_plan,
            source_id,
            prepared,
            pending,
            vectors,
            relationships,
            resolved_by_origin,
            warnings,
            scope=scope,
        )

    def _compile_plan(
        self,
        source_id: str,
        prepared: tuple[NodeDraft, ...],
        pending,
        vectors,
        relationships: tuple[EdgeDraft, ...],
        resolved_by_origin: Mapping[str, NodeDraft | None],
        warnings: list[str],
        *,
        scope: ScopeBinding,
    ) -> KnowledgeImportPlan:
        """Apply model results and compile the graph projection in one blocking phase."""
        nodes = EmbeddingEnricher.apply(prepared, pending, vectors)
        resolved_edges = self._remap_and_deduplicate_edges(
            relationships,
            resolved_by_origin,
            warnings=warnings,
        )
        edges = self._prepare_edges(resolved_edges, nodes, scope)
        operations: list[StoreOperation] = []
        known_keys = {
            node.ref: node.transient["existing_key"]
            for node in nodes
            if node.transient.get("existing_key") is not None
        }
        for (spec, existing), drafts in grouped(
            nodes,
            key=lambda node: (
                node.spec,
                node.transient.get("existing_key") is not None,
            ),
        ):
            if not existing:
                operations.append(StoreOperation.project_nodes(tuple(drafts)))
                continue
            patches = []
            for node in drafts:
                changes = self._property_changes(
                    spec, node.transient["existing_properties"], node.output()
                )
                if changes:
                    patches.append((known_keys[node.ref], changes))
            if patches:
                operations.append(
                    StoreOperation(
                        "patch_nodes", {"spec": spec, "patches": tuple(patches)}
                    )
                )
        for (spec, create), drafts in grouped(
            edges,
            key=lambda edge: (
                edge.spec,
                edge.transient.get("existing_properties") is None,
            ),
        ):
            parameter = "edges" if create else "patches"
            rows, refs = [], {}
            for edge in drafts:
                properties = (
                    edge.output()
                    if create
                    else self._property_changes(
                        spec,
                        edge.transient["existing_properties"],
                        edge.output(),
                    )
                )
                if not create and not properties:
                    continue
                for endpoint, ref in enumerate((edge.source_ref, edge.target_ref)):
                    if ref not in known_keys:
                        refs[(parameter, len(rows), endpoint)] = ref
                rows.append(
                    (
                        known_keys.get(edge.source_ref),
                        known_keys.get(edge.target_ref),
                        properties,
                    )
                )
            if rows:
                parameters = {"spec": spec, parameter: tuple(rows)}
                if create:
                    parameters["fields"] = ()
                operations.append(
                    StoreOperation(
                        "create_edges" if create else "patch_edges",
                        parameters,
                        node_key_refs=refs,
                    )
                )
        return KnowledgeImportPlan(
            source_id,
            scope.tenant_id,
            scope.workspace_id,
            nodes,
            edges,
            tuple(warnings),
            user_id=scope.user_id,
            transaction=TransactionPlan(tuple(operations)),
        )

    @staticmethod
    def _property_changes(
        spec: NodeSpec | EdgeSpec,
        existing: Mapping[str, object],
        desired: Mapping[str, object],
    ) -> dict[str, object]:
        """Diff final values; merge policy has already run before Plan construction."""
        return {
            prop.name: desired[prop.name]
            for prop in spec.properties
            if prop.owner is PropertyOwner.CALLER
            and prop.editable
            and prop.name in desired
            and existing.get(prop.name) != desired[prop.name]
        }

    def _prepare_edges(
        self,
        edges: tuple[EdgeDraft, ...],
        nodes: tuple[NodeDraft, ...],
        scope: ScopeBinding,
    ) -> tuple[EdgeDraft, ...]:
        """Read existing edges and finalize their fill-only business properties."""
        nodes_by_ref = {node.ref: node for node in nodes}
        prepared_edges: list[EdgeDraft] = []
        for edge in edges:
            source_key = nodes_by_ref[edge.source_ref].transient.get("existing_key")
            target_key = nodes_by_ref[edge.target_ref].transient.get("existing_key")
            existing_edge = None
            if isinstance(source_key, str) and isinstance(target_key, str):
                existing_edge = self._store.get_edges(
                    edge.spec,
                    ((source_key, target_key),),
                    scope=scope,
                )[0]
            properties = edge.properties
            if existing_edge is not None:
                existing_properties = {
                    prop.name: existing_edge.properties[prop.name]
                    for prop in edge.spec.properties
                    if prop.owner is PropertyOwner.CALLER
                    and prop.name in existing_edge.properties
                }
                properties = self._merge_edge_properties(
                    existing_properties, properties
                )
            prepared_edges.append(
                replace(
                    edge,
                    properties=properties,
                    transient={
                        **edge.transient,
                        "existing_properties": (
                            None if existing_edge is None else existing_edge.properties
                        ),
                    },
                )
            )
        return tuple(prepared_edges)

    def _require_document_scope(
        self, context: AuthorizationContext, scope: ScopeBinding
    ) -> None:
        """Check file ownership before reading or persisting an import's source."""
        self._require_write_scope(scope)
        require_target(
            context, tenant_id=self._tenant_id, workspace_id=self._workspace_id
        )
        if (scope.tenant_id, scope.workspace_id, scope.user_id) != (
            self._tenant_id,
            self._workspace_id,
            context.user_id,
        ):
            raise PermissionError(
                "document store and import scope belong to different users"
            )

    def _require_write_scope(self, scope: ScopeBinding) -> None:
        """Require the current user's private Knowledge write capability."""
        if type(scope) is not ScopeBinding:
            raise TypeError("scope must be a concrete ScopeBinding")
        self._store.access.require(scope, AccessMode.WRITE)
        if (
            scope.zone != Zone.KNOWLEDGE.value
            or scope.datasource_id is not None
            or scope.write_owner_id != scope.user_id
        ):
            raise PermissionError("knowledge import scope does not match the request")

    async def _drafts(
        self,
        extractions: Sequence[Sequence[NodeDraft | EdgeDraft]],
        deduplicator: Deduplicator,
    ) -> tuple[
        tuple[NodeDraft, ...],
        tuple[NodeDraft, ...],
        tuple[EdgeDraft, ...],
    ]:
        """Flatten chunk Drafts and run same-document node deduplication."""
        raw_nodes: list[NodeDraft] = []
        raw_edges: list[EdgeDraft] = []
        for extraction in extractions:
            for draft in extraction:
                if type(draft) is NodeDraft:
                    raw_nodes.append(draft)
                elif type(draft) is EdgeDraft:
                    raw_edges.append(draft)
                else:
                    raise TypeError("Knowledge extraction must return Draft values")
        if len(raw_nodes) > self._limits.max_nodes:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE,
                "extracted knowledge exceeds the node limit",
            )
        if len(raw_edges) > self._limits.max_edges:
            raise KnowledgeImportError(
                KnowledgeImportErrorCode.DOCUMENT_TOO_LARGE,
                "extracted knowledge exceeds the edge limit",
            )
        deduplicated = tuple(await deduplicator.deduplicate(raw_nodes))
        retained: list[NodeDraft] = []
        ambiguous: list[NodeDraft] = []
        for draft in deduplicated:
            status = deduplication_status(draft)
            if status == "retained":
                retained.append(draft)
            else:
                ambiguous.append(draft)
        return tuple(retained), tuple(ambiguous), tuple(raw_edges)

    def _finalize_node(
        self,
        draft: NodeDraft,
        existing: NodeRecord | None,
        *,
        source_id: str,
    ) -> NodeDraft:
        """Merge final persisted facts before the batch embedding pass."""
        if existing is None:
            resolved_identity_hash = identity_hash(draft.spec, draft.properties)
            transient = dict(draft.transient)
        else:
            resolved_identity_hash = require_trimmed_text(
                existing.properties.get("identity_hash"),
                "existing node identity_hash",
            )
            transient = {
                **draft.transient,
                "existing_key": existing.key,
                "existing_properties": existing.properties,
            }
        properties = self._merge_node_properties(draft, existing)
        existing_source_ids = (
            ()
            if existing is None
            else text_members(existing.properties.get("source_ids"))
        )
        properties.update(
            {
                "identity_hash": resolved_identity_hash,
                "source_ids": tuple(dict.fromkeys((*existing_source_ids, source_id))),
            }
        )
        return NodeDraft.build(
            GRAPH_SCHEMA,
            draft.spec,
            draft.ref,
            {name: value for name, value in properties.items() if value is not None},
            transient=transient,
        )

    def _remap_and_deduplicate_edges(
        self,
        relationships: Sequence[EdgeDraft],
        resolved_by_origin: Mapping[str, NodeDraft | None],
        *,
        warnings: list[str],
    ) -> tuple[EdgeDraft, ...]:
        """Remap resolved endpoints and merge duplicate edges by spec and refs."""
        positions: dict[tuple[object, ...], int] = {}
        result: list[EdgeDraft] = []
        for draft in relationships:
            source = resolved_by_origin.get(draft.source_ref)
            target = resolved_by_origin.get(draft.target_ref)
            if source is None or target is None:
                warnings.append("relationship with an ambiguous endpoint was skipped")
                continue
            value = replace(
                draft,
                source_ref=source.ref,
                target_ref=target.ref,
            )
            key = (value.spec, value.source_ref, value.target_ref)
            index = positions.get(key)
            if index is None:
                positions[key] = len(result)
                result.append(value)
            else:
                left = result[index]
                result[index] = replace(
                    left,
                    properties=self._merge_edge_properties(
                        left.properties, value.properties
                    ),
                )
        return tuple(result)

    @staticmethod
    def _merge_node_properties(
        draft: NodeDraft,
        existing: NodeRecord | NodeDraft | None,
    ) -> dict[str, object]:
        """Merge caller-owned business facts without identity, provenance, or vectors."""
        mergeable = {
            property_spec.name
            for property_spec in draft.spec.properties
            if property_spec.owner is PropertyOwner.CALLER
            and property_spec.name
            not in {"identity_hash", "source_ids", "embedding", "embedding_hash"}
        }
        properties = (
            {}
            if existing is None
            else {
                name: value
                for name, value in existing.properties.items()
                if name in mergeable
            }
        )
        if "aliases" in mergeable:
            aliases = tuple(
                dict.fromkeys(
                    (
                        *text_members(properties.get("aliases")),
                        *text_members(draft.properties.get("aliases")),
                    )
                )
            )
            if aliases:
                properties["aliases"] = aliases
        for name, value in draft.properties.items():
            if name not in mergeable or name == "aliases":
                continue
            if name not in properties or is_empty_value(properties[name]):
                properties[name] = value
        return properties

    @staticmethod
    def _merge_edge_properties(
        existing: Mapping[str, object],
        incoming: Mapping[str, object],
    ) -> dict[str, object]:
        """Keep prior relationship facts and fill only missing values."""
        properties = dict(existing)
        for name, value in incoming.items():
            if is_empty_value(properties.get(name)):
                properties[name] = value
        return properties


__all__ = [
    "KnowledgeImportLimits",
    "KnowledgeImportPlan",
    "KnowledgeImportService",
]

"""Extract one user's messages and atomically persist the resulting User Graph."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from types import MappingProxyType
from typing import cast

from ..auth import AccessMode, ScopeBinding
from ..executor import BoundedExecutor
from ..model import ModelService, TextEmbedder
from ..process import BatchExtractor, EmbeddingEnricher, TextChunk, build_embedding_text
from ..schema import (
    COLUMN,
    DIMENSION,
    ENTITY,
    EVENT,
    HAS_MEMORY,
    METRIC,
    USER,
    USER_MEMORY,
    USER_MEMORY_REFERS_TO_COLUMN,
    USER_MEMORY_REFERS_TO_DIMENSION,
    USER_MEMORY_REFERS_TO_ENTITY,
    USER_MEMORY_REFERS_TO_EVENT,
    USER_MEMORY_REFERS_TO_METRIC,
    EdgeSpec,
    NodeSpec,
    PropertyOwner,
    Zone,
)
from ..settings import get_settings
from ..store import (
    EdgeDraft,
    EdgeRecord,
    GraphStore,
    NeighborDirection,
    NodeDraft,
    NodeRecord,
)
from ..transaction import (
    StoreOperation,
    TransactionPlan,
)
from ..utils.batches import grouped
from ..utils.immutable import freeze_mapping
from ..utils.parsing import parse_json_object
from ..utils.validation import require_trimmed_text
from ..utils.values import normalize_text
from .algo.deduplicator import UserMemoryDeduplicator, user_memory_id
from .algo.extraction import (
    UserMemoryChunkExtractor,
    extract_project_preferences,
    parse_message_document,
)
from .algo.reconciliation import (
    UserMemoryReconciler,
    evidence_time,
    merge_profile,
    stale_memory,
)
from .algo.resolver import UserMemoryResolver

_PROFILE_FIELDS = frozenset({"display_name", "description", "locale", "timezone"})
_REFERENCE_EDGES: Mapping[NodeSpec, EdgeSpec] = {
    METRIC: USER_MEMORY_REFERS_TO_METRIC,
    DIMENSION: USER_MEMORY_REFERS_TO_DIMENSION,
    COLUMN: USER_MEMORY_REFERS_TO_COLUMN,
    ENTITY: USER_MEMORY_REFERS_TO_ENTITY,
    EVENT: USER_MEMORY_REFERS_TO_EVENT,
}
_REFERENCE_EDGE_SPECS = frozenset(_REFERENCE_EDGES.values())
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MemoryIngestPlan:
    """Hold algorithm results for reconciliation with current facts at write time."""

    tenant_id: str
    workspace_id: str
    user_id: str
    occurred_at: datetime
    profile_delta: Mapping[str, object]
    profile_source_refs: tuple[str, ...]
    memories: tuple[NodeDraft, ...]  # Incoming evidence and reconciliation decisions.
    reference_keys: Mapping[str, str]  # External ref -> persisted target key.
    edges: tuple[EdgeDraft, ...]  # Intended connections using request-local refs.
    profile_evidence_at: str = field(
        default="", kw_only=True
    )  # Authoritative clock for this profile delta.
    evidence_times: Mapping[str, str] = field(
        default_factory=dict, kw_only=True
    )  # Source clocks for graph rows predating evidence_at.

    def __post_init__(self) -> None:
        """Freeze collections and reject incomplete or cross-user operations."""
        for name in ("tenant_id", "workspace_id", "user_id"):
            object.__setattr__(
                self,
                name,
                require_trimmed_text(getattr(self, name), name),
            )
        object.__setattr__(self, "occurred_at", _utc_timestamp(self.occurred_at))
        delta = freeze_mapping(
            self.profile_delta,
            field_name="profile_delta",
            key_field_name="profile field",
        )
        if set(delta) - _PROFILE_FIELDS:
            raise ValueError("profile_delta contains unsupported fields")
        if any(not isinstance(value, str) or not value for value in delta.values()):
            raise ValueError("profile_delta values must contain non-empty text")
        source_refs = tuple(
            require_trimmed_text(value, "profile source_ref")
            for value in self.profile_source_refs
        )
        if len(source_refs) != len(set(source_refs)):
            raise ValueError("profile_source_refs cannot contain duplicates")
        memories = tuple(self.memories)
        edges = tuple(self.edges)
        if any(type(memory) is not NodeDraft for memory in memories):
            raise TypeError("plan memories must contain NodeDraft values")
        if any(memory.spec is not USER_MEMORY for memory in memories):
            raise TypeError("plan memories must contain only UserMemory drafts")
        if any(type(edge) is not EdgeDraft for edge in edges):
            raise TypeError("plan edges must contain EdgeDraft values")
        reference_keys = MappingProxyType(
            {
                require_trimmed_text(ref, "reference ref"): require_trimmed_text(
                    key, "reference Store key"
                )
                for ref, key in self.reference_keys.items()
            }
        )
        memory_refs = [memory.ref for memory in memories]
        memory_ids = [memory.properties["id"] for memory in memories]
        if len(memory_refs) != len(set(memory_refs)):
            raise ValueError("plan memories must have unique refs")
        if len(memory_ids) != len(set(memory_ids)):
            raise ValueError("plan memories must have unique ids")
        user_ref = f"user:{self.user_id}"
        if set(reference_keys).intersection((user_ref, *memory_refs)):
            raise ValueError("existing reference refs cannot shadow plan node refs")
        edge_keys: list[tuple[object, ...]] = []
        has_memory_refs: list[str] = []
        used_reference_refs: set[str] = set()
        for edge in edges:
            if edge.spec is HAS_MEMORY:
                if edge.source_ref != user_ref:
                    raise ValueError("HAS must start at the plan user")
                has_memory_refs.append(edge.target_ref)
            elif edge.spec in _REFERENCE_EDGE_SPECS:
                if edge.source_ref not in memory_refs:
                    raise ValueError("REFERS_TO must start at a plan memory")
                if edge.target_ref not in reference_keys:
                    raise ValueError("REFERS_TO target must have a resolved Store key")
                used_reference_refs.add(edge.target_ref)
            else:
                raise TypeError("plan contains an unsupported User Graph edge")
            edge_keys.append((edge.spec, edge.source_ref, edge.target_ref))
        if sorted(has_memory_refs) != sorted(memory_refs):
            raise ValueError("plan requires exactly one HAS per memory")
        if used_reference_refs != set(reference_keys):
            raise ValueError("reference_keys must match REFERS_TO targets")
        if len(edge_keys) != len(set(edge_keys)):
            raise ValueError("plan edges cannot contain duplicates")
        object.__setattr__(
            self, "evidence_times", MappingProxyType(dict(self.evidence_times))
        )
        object.__setattr__(
            self, "profile_evidence_at", evidence_time(self.profile_evidence_at)
        )
        object.__setattr__(self, "profile_delta", delta)
        object.__setattr__(self, "profile_source_refs", source_refs)
        object.__setattr__(self, "memories", memories)
        object.__setattr__(self, "reference_keys", reference_keys)
        object.__setattr__(self, "edges", edges)


class MemoryIngestService:
    """Prepare one User Graph plan and execute it through one transaction."""

    def __init__(
        self,
        store: GraphStore,
        models: ModelService,
        *,
        executor: BoundedExecutor,
    ) -> None:
        """Bind only Workspace-stable Store and model infrastructure."""
        if not isinstance(store, GraphStore):
            raise TypeError("store must implement GraphStore")
        store.schema.require_node(USER)
        store.schema.require_node(USER_MEMORY)
        self._store = store
        self._models = models
        self._executor = executor

    async def ingest(
        self,
        messages: Sequence[Mapping[str, str]],
        read_scope: ScopeBinding,
        write_scope: ScopeBinding,
        *,
        occurred_at: datetime | None = None,
        target_message_id: str | None = None,
        evidence_times: Mapping[str, str] | None = None,
        tool_observations: Sequence[Mapping[str, object]] = (),
    ) -> None:
        """Prepare a scope-bound plan, then execute one finite transaction."""

        async def work():
            async with self._prepare_plan(
                messages,
                read_scope,
                occurred_at=occurred_at,
                target_message_id=target_message_id,
                evidence_times=evidence_times,
                tool_observations=tool_observations,
            ) as (plan, embedder):
                await self.execute_plan(plan, write_scope, embedder=embedder)

        return await self._executor.run(work)

    async def prepare_plan(
        self,
        messages: Sequence[Mapping[str, str]],
        scope: ScopeBinding,
        *,
        occurred_at: datetime | None = None,
        target_message_id: str | None = None,
        evidence_times: Mapping[str, str] | None = None,
        tool_observations: Sequence[Mapping[str, object]] = (),
    ) -> MemoryIngestPlan:
        """Return a data-only plan after closing preparation clients."""
        async with self._prepare_plan(
            messages,
            scope,
            occurred_at=occurred_at,
            target_message_id=target_message_id,
            evidence_times=evidence_times,
            tool_observations=tool_observations,
        ) as (plan, _):
            return plan

    @asynccontextmanager
    async def _prepare_plan(
        self,
        messages: Sequence[Mapping[str, str]],
        scope: ScopeBinding,
        *,
        occurred_at: datetime | None,
        target_message_id: str | None,
        evidence_times: Mapping[str, str] | None,
        tool_observations: Sequence[Mapping[str, object]],
    ) -> AsyncIterator[tuple[MemoryIngestPlan, TextEmbedder | None]]:
        """Validate evidence, then acquire clients for model-backed preparation."""
        self._require_read_scope(scope)
        document, chunks = await self._executor.run_sync(
            self._prepare_messages,
            messages,
            target_message_id,
        )
        if not document["messages"]:
            yield (
                MemoryIngestPlan(
                    tenant_id=scope.tenant_id,
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    occurred_at=_utc_timestamp(occurred_at),
                    profile_delta={},
                    profile_source_refs=(),
                    memories=(),
                    reference_keys={},
                    edges=(),
                    evidence_times=evidence_times or {},
                ),
                None,
            )
            return
        async with self._models.open() as (llm, embedder):
            matches = tuple(
                await self._executor.run_sync(
                    self._store.list_nodes,
                    USER,
                    filters={"user_id": scope.user_id, "owner_id": scope.user_id},
                    limit=2,
                    scope=scope,
                )
            )
            if len(matches) > 1:
                raise RuntimeError("User business lookup returned multiple nodes")
            existing_user = matches[0] if matches else None
            existing_profile: Mapping[str, object] = {}
            if (
                existing_user is not None
                and existing_user.properties.get("status") == "active"
            ):
                raw_profile = existing_user.properties.get("profile")
                if raw_profile is not None and raw_profile != "":
                    existing_profile = parse_json_object(
                        raw_profile, "existing User profile"
                    )
            extractor = BatchExtractor(
                UserMemoryChunkExtractor(
                    llm,
                    user_id=scope.user_id,
                    existing_profile=existing_profile,
                    schema=self._store.schema,
                ),
                max_concurrency=get_settings().user.max_concurrency,
            )
            plan = await self._build_plan(
                chunks,
                extractor,
                existing_user,
                UserMemoryDeduplicator(llm),
                UserMemoryReconciler(llm, embedder, run_sync=self._executor.run_sync),
                UserMemoryResolver(
                    self._store, scope, llm, embedder, run_sync=self._executor.run_sync
                ),
                scope=scope,
                timestamp=_utc_timestamp(occurred_at),
                evidence_times=evidence_times or {},
                project_preferences=await self._executor.run_sync(
                    extract_project_preferences,
                    tool_observations,
                    workspace_id=scope.workspace_id,
                    user_id=scope.user_id,
                    schema=self._store.schema,
                ),
            )
            yield plan, embedder

    @staticmethod
    def _prepare_messages(messages, target_message_id):
        """Normalize evidence and construct the complete extraction input."""
        document = parse_message_document(
            {
                "messages": list(messages),
                **(
                    {"target_message_ids": [target_message_id]}
                    if target_message_id is not None
                    else {}
                ),
            }
        )
        chunks = _message_chunks(
            cast(Sequence[Mapping[str, str]], document["messages"]),
            target_message_id=target_message_id,
        )
        return document, chunks

    async def execute_plan(
        self,
        plan: MemoryIngestPlan,
        scope: ScopeBinding,
        *,
        embedder: TextEmbedder | None = None,
    ) -> None:
        """Execute one prepared plan through transaction-bound Store calls."""
        if type(plan) is not MemoryIngestPlan:
            raise TypeError("plan must be a MemoryIngestPlan")
        self._require_write_scope(scope)
        if (
            plan.tenant_id != scope.tenant_id
            or plan.workspace_id != scope.workspace_id
            or plan.user_id != scope.user_id
        ):
            raise PermissionError("memory ingest plan belongs to another scope")

        def execute(transaction_store: GraphStore) -> None:
            writer = MemoryIngestService(
                transaction_store,
                self._models,
                executor=self._executor,
            )
            transaction = writer._materialize_plan(plan, scope)
            writer._write_plan(transaction, scope)

        await self._executor.run_sync(self._store.atomic, execute)
        if plan.memories:
            try:
                async with AsyncExitStack() as stack:
                    if embedder is None:
                        embedder = await stack.enter_async_context(
                            self._models.embedder()
                        )
                    await self._embed_memories(scope, embedder)
            except Exception:
                _LOGGER.warning("User Graph post-write embedding failed", exc_info=True)

    async def _embed_memories(
        self,
        scope: ScopeBinding,
        embedder: TextEmbedder,
    ) -> None:
        """Refresh missing or stale vectors across this user's non-revoked memories."""
        prepared, pending = await self._executor.run_sync(self._embedding_drafts, scope)
        if not prepared:
            return
        vectors = (
            await embedder.embed(tuple(text for _, text in pending)) if pending else ()
        )
        await self._executor.run_sync(
            self._write_embeddings, scope, prepared, pending, vectors
        )

    def _embedding_drafts(self, scope: ScopeBinding):
        """Read and prepare stale-vector nodes without occupying the event loop."""
        drafts = []
        offset = 0
        page_size = 500
        while True:
            nodes = self._store.list_nodes(
                USER_MEMORY,
                filters={"owner_id": scope.user_id},
                limit=page_size,
                offset=offset,
                scope=scope,
            )
            for node in nodes:
                if node.properties.get("status") == "revoked":
                    continue
                text = build_embedding_text(USER_MEMORY, node.properties)
                if (
                    node.properties.get("embedding")
                    and node.properties.get("embedding_hash")
                    == sha256(text.encode("utf-8")).hexdigest()
                ):
                    continue
                drafts.append(
                    NodeDraft(
                        USER_MEMORY,
                        node.key,
                        {
                            name: node.properties.get(name)
                            for name in USER_MEMORY.embedding_fields
                        },
                    )
                )
            offset += len(nodes)
            if len(nodes) < page_size:
                break
        return EmbeddingEnricher.prepare(tuple(drafts))

    def _write_embeddings(self, scope, prepared, pending, vectors):
        """Validate vectors and write the complete prepared projection."""
        enriched = EmbeddingEnricher.apply(prepared, pending, vectors)

        def write(tx):
            existing = tx.get_nodes(
                USER_MEMORY,
                tuple(draft.ref for draft in enriched),
                fields=(),
                scope=scope,
            )
            if any(node is None for node in existing):
                raise RuntimeError("UserMemory embedding target no longer exists")
            tx.patch_nodes(
                USER_MEMORY,
                tuple(
                    (
                        draft.ref,
                        {
                            "embedding": draft.properties["embedding"],
                            "embedding_hash": draft.properties["embedding_hash"],
                        },
                    )
                    for draft in enriched
                ),
                scope=scope,
            )

        self._store.atomic(write)

    def _write_plan(self, transaction: TransactionPlan, scope: ScopeBinding) -> None:
        """Apply graph writes through the transaction-bound Store."""
        node_keys = {}
        for operation in transaction.operations:
            parameters = dict(operation.parameters)
            if operation.name == "create_nodes":
                keys = self._store.create_nodes(**parameters, scope=scope)
                node_keys.update(
                    (ref, key)
                    for ref, key in zip(operation.node_refs, keys, strict=True)
                )
            elif operation.name == "create_edges":
                edges = [list(edge) for edge in parameters["edges"]]
                for (_, index, endpoint), ref in operation.node_key_refs.items():
                    edges[index][endpoint] = node_keys[ref]
                self._store.create_edges(
                    parameters["spec"],
                    tuple(tuple(edge) for edge in edges),
                    fields=(),
                    scope=scope,
                )
            elif operation.name in {"patch_nodes", "delete_edges"}:
                existing = (
                    self._store.get_nodes(
                        parameters["spec"],
                        tuple(key for key, _ in parameters["patches"]),
                        fields=(),
                        scope=scope,
                    )
                    if operation.name == "patch_nodes"
                    else self._store.get_edges(
                        parameters["spec"],
                        parameters["endpoints"],
                        fields=(),
                        scope=scope,
                    )
                )
                if any(record is None for record in existing):
                    raise RuntimeError(
                        "User Graph changed before plan execution; retry source"
                    )
                getattr(self._store, operation.name)(**parameters, scope=scope)
            else:
                raise ValueError(f"unsupported UG write operation: {operation.name}")

    async def _build_plan(
        self,
        chunks: Sequence[TextChunk],
        extractor: BatchExtractor[tuple[NodeDraft, ...]],
        existing_user: NodeRecord | None,
        deduplicator: UserMemoryDeduplicator,
        reconciler: UserMemoryReconciler,
        resolver: UserMemoryResolver,
        *,
        scope: ScopeBinding,
        timestamp: datetime,
        evidence_times: Mapping[str, str],
        project_preferences: tuple[NodeDraft, ...] = (),
    ) -> MemoryIngestPlan:
        """Prepare memory decisions, profile deltas and resolved reference edges."""
        extractions = await extractor.extract(chunks)
        profiles, memories = await self._drafts(extractions, deduplicator)
        memories = tuple(
            NodeDraft(
                USER_MEMORY,
                node.ref,
                {
                    **node.properties,
                    "id": _memory_id(scope.workspace_id, scope.user_id, node),
                },
                transient=node.transient,
            )
            for node in memories
        )
        memories += project_preferences
        existing_memories = (
            await self._executor.run_sync(
                self._owned_memories,
                existing_user,
                scope,
                evidence_times,
            )
            if memories
            else ()
        )
        memories = await reconciler.reconcile(
            memories,
            tuple(
                node
                for node in existing_memories
                if node.properties.get("status") in {"active", "needs_review"}
            ),
        )
        retained = {node.properties["id"] for node in memories}
        if any(
            item["id"] in retained
            for node in memories
            for item in node.transient.get("superseded_memories", ())
        ):
            raise ValueError("UG batch both retains and supersedes a memory")
        profile_delta: dict[str, object] = {}
        profile_source_refs: list[str] = []
        profile_stamp = ""
        for draft in profiles:
            if draft.properties["user_id"] != scope.user_id:
                raise TypeError("cannot merge profiles for different users")
            profile_delta.update(
                cast(Mapping[str, object], draft.transient["profile_delta"])
            )
            profile_source_refs.extend(
                cast(Sequence[str], draft.transient["source_refs"])
            )
            profile_stamp = max(
                profile_stamp, str(draft.transient.get("evidence_at", ""))
            )
        references = await resolver.resolve_batch(memories)
        reference_keys = {}
        edges = []
        for memory, targets in zip(memories, references, strict=True):
            edges.append(EdgeDraft(HAS_MEMORY, f"user:{scope.user_id}", memory.ref, {}))
            for target in targets:
                target_ref = f"existing:{target.spec.name}:{target.key}"
                reference_keys[target_ref] = target.key
                edges.append(
                    EdgeDraft(_REFERENCE_EDGES[target.spec], memory.ref, target_ref, {})
                )
        return MemoryIngestPlan(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            occurred_at=timestamp,
            memories=memories,
            reference_keys=reference_keys,
            edges=tuple(edges),
            profile_delta=profile_delta,
            profile_source_refs=tuple(dict.fromkeys(profile_source_refs)),
            profile_evidence_at=profile_stamp,
            evidence_times=evidence_times,
        )

    def _lookup(
        self, spec: NodeSpec, field: str, value: str, scope: ScopeBinding
    ) -> NodeRecord | None:
        matches = self._store.list_nodes(
            spec,
            filters={field: value, "owner_id": scope.user_id},
            limit=2,
            scope=scope,
        )
        if len(matches) > 1:
            raise RuntimeError(f"{spec.name} business lookup returned multiple nodes")
        return matches[0] if matches else None

    def _materialize_plan(
        self,
        plan: MemoryIngestPlan,
        scope: ScopeBinding,
    ) -> TransactionPlan:
        """Apply prepared meanings to current graph facts without calling models."""
        timestamp = plan.occurred_at
        evidence_times = plan.evidence_times
        existing_user = self._lookup(USER, "user_id", scope.user_id, scope)
        existing_profile = (
            parse_json_object(
                existing_user.properties.get("profile") or "{}", "existing User profile"
            )
            if existing_user is not None
            else {}
        )
        profile_json = merge_profile(
            existing_profile,
            plan.profile_delta,
            stamp=plan.profile_evidence_at,
        )
        if existing_user is None:
            user_properties = {
                "user_id": scope.user_id,
                "profile": profile_json,
                "status": "active",
                "created_at": timestamp,
                "updated_at": timestamp,
            }
        else:
            user_properties = {
                prop.name: existing_user.properties[prop.name]
                for prop in USER.properties
                if prop.owner is PropertyOwner.CALLER
                and prop.name in existing_user.properties
            }
            user_properties["status"] = (
                existing_user.properties.get("status") or "active"
            )
            if profile_json != existing_user.properties.get("profile"):
                user_properties.update(
                    profile=profile_json,
                    updated_at=timestamp,
                )
        user = NodeDraft.build(
            self._store.schema,
            USER,
            f"user:{scope.user_id}",
            user_properties,
        )
        nodes: list[tuple[NodeDraft, NodeRecord | None]] = [(user, existing_user)]
        additional_writes: list[StoreOperation] = []
        replaced_keys: set[str] = set()
        for draft in plan.memories:
            memory_id = str(draft.properties["id"])
            current = self._lookup(USER_MEMORY, "id", memory_id, scope)
            if current is not None:
                current = _with_evidence_time(current, evidence_times)
            incoming = {
                **draft.output(),
                "id": memory_id,
                "created_at": timestamp,
                "updated_at": timestamp,
            }
            action = draft.transient.get("action", "merge")
            if current is not None and stale_memory(
                {**incoming, "action": action}, current.properties
            ):
                continue
            obsolete_nodes = []
            for retired in draft.transient.get("superseded_memories", ()):
                obsolete = self._lookup(USER_MEMORY, "id", retired["id"], scope)
                if obsolete is None:
                    raise RuntimeError(
                        "Superseded UserMemory scope changed; retry source"
                    )
                obsolete = _with_evidence_time(obsolete, evidence_times)
                obsolete_nodes.append((retired, obsolete))
            if any(
                stale_memory({**incoming, "action": "replace"}, obsolete.properties)
                for _, obsolete in obsolete_nodes
            ):
                continue
            for retired, obsolete in obsolete_nodes:
                if normalize_text(obsolete.properties["description"]) != normalize_text(
                    retired["expected_description"]
                ):
                    raise RuntimeError("Superseded UserMemory changed; retry source")
            if current is not None:
                expected = draft.transient.get("expected_description")
                if expected is not None and normalize_text(
                    current.properties.get("description")
                ) != normalize_text(expected):
                    raise RuntimeError(
                        "UserMemory changed after reconciliation; retry source"
                    )
                if action == "create" and normalize_text(
                    current.properties.get("description")
                ) != normalize_text(incoming["description"]):
                    raise RuntimeError(
                        "UserMemory identity already has another meaning; retry source"
                    )
            if current is None:
                properties = incoming
            else:
                properties = {
                    prop.name: current.properties[prop.name]
                    for prop in USER_MEMORY.properties
                    if prop.owner is PropertyOwner.CALLER
                    and prop.name in current.properties
                }
                properties.update(
                    self._merge_memory_properties(
                        current, incoming, timestamp, action=str(action)
                    )
                )
                if action == "replace":
                    replaced_keys.add(current.key)
                    additional_writes.extend(self._clear_references(current, scope))
            for _, obsolete in obsolete_nodes:
                additional_writes.append(
                    StoreOperation(
                        "patch_nodes",
                        {
                            "spec": USER_MEMORY,
                            "patches": (
                                (
                                    obsolete.key,
                                    {
                                        "status": "revoked",
                                        "source_ref": incoming["source_ref"],
                                        "source_refs": tuple(
                                            dict.fromkeys(
                                                (
                                                    *obsolete.properties.get(
                                                        "source_refs", ()
                                                    ),
                                                    incoming["source_ref"],
                                                )
                                            )
                                        ),
                                        "evidence_at": incoming["evidence_at"],
                                        "source_type": incoming["source_type"],
                                        "updated_at": timestamp,
                                    },
                                ),
                            ),
                        },
                    )
                )
            memory = NodeDraft.build(
                self._store.schema,
                USER_MEMORY,
                draft.ref,
                properties,
            )
            nodes.append((memory, current))
        retained_refs = {node.ref for node, _ in nodes[1:]}
        edges = tuple(
            edge
            for edge in plan.edges
            if (edge.target_ref if edge.spec is HAS_MEMORY else edge.source_ref)
            in retained_refs
        )
        known_keys = {
            **plan.reference_keys,
            **{node.ref: current.key for node, current in nodes if current is not None},
        }
        prepared_edges: list[tuple[EdgeDraft, EdgeRecord | None]] = []
        for edge in edges:
            current_edge = None
            if (
                edge.source_ref in known_keys
                and edge.target_ref in known_keys
                and known_keys[edge.source_ref] not in replaced_keys
            ):
                current_edge = (
                    self._store.get_edges(
                        edge.spec,
                        ((known_keys[edge.source_ref], known_keys[edge.target_ref]),),
                        scope=scope,
                    )
                )[0]
            prepared_edges.append((edge, current_edge))
        return self._compile_plan(
            tuple(nodes),
            tuple(prepared_edges),
            plan.reference_keys,
            additional_writes=additional_writes,
        )

    def _owned_memories(
        self,
        user: NodeRecord | None,
        scope: ScopeBinding,
        evidence_times: Mapping[str, str],
    ) -> tuple[NodeRecord, ...]:
        """Read owned memories ordered by update time and business ID."""
        if user is None:
            return ()
        memories = []
        offset = 0
        while True:
            page = self._store.expand_neighbors(
                USER,
                user.key,
                direction=NeighborDirection.OUTGOING,
                limit=100,
                offset=offset,
                node_fields=(
                    "id",
                    "owner_id",
                    "type",
                    "description",
                    "source_ref",
                    "evidence_at",
                    "triggers",
                    "status",
                    "updated_at",
                ),
                edge_fields=(),
                scope=scope,
            )
            memories.extend(
                _with_evidence_time(node, evidence_times)
                for node, edge in page
                if edge.spec.relationship == "HAS"
                and node.spec is USER_MEMORY
                and node.properties.get("owner_id") == scope.user_id
            )
            offset += len(page)
            if len(page) < 100:
                break
        memories.sort(key=lambda node: str(node.properties["id"]))
        memories.sort(
            key=lambda node: str(node.properties.get("updated_at") or ""), reverse=True
        )
        return tuple(memories)

    def _clear_references(
        self, memory: NodeRecord, scope: ScopeBinding
    ) -> list[StoreOperation]:
        """Prepare deletion of the old meaning's outgoing references."""
        operations = []
        for spec in _REFERENCE_EDGES.values():
            offset = 0
            while True:
                page = self._store.list_edges(
                    spec,
                    filters={"owner_id": scope.user_id},
                    limit=100,
                    offset=offset,
                    scope=scope,
                )
                operations.extend(
                    StoreOperation(
                        "delete_edges",
                        {
                            "spec": spec,
                            "endpoints": ((memory.key, edge.target_key),),
                        },
                    )
                    for edge in page
                    if edge.source_key == memory.key
                )
                offset += len(page)
                if len(page) < 100:
                    break
        return operations

    def _compile_plan(
        self,
        nodes: tuple[tuple[NodeDraft, NodeRecord | None], ...],
        edges: tuple[tuple[EdgeDraft, EdgeRecord | None], ...],
        reference_keys: Mapping[str, str],
        *,
        additional_writes: Sequence[StoreOperation] = (),
    ) -> TransactionPlan:
        """Compile final facts into ordered writes, using known keys directly."""
        operations: list[StoreOperation] = []
        known_keys = {
            **reference_keys,
            **{node.ref: current.key for node, current in nodes if current is not None},
        }
        for (spec, create), entries in grouped(
            nodes,
            key=lambda item: (
                item[0].spec,
                item[1] is None,
            ),
        ):
            batch = tuple(entries)
            if create:
                operations.append(
                    StoreOperation.project_nodes(tuple(node for node, _ in batch))
                )
                continue
            patches = []
            for node, current in batch:
                changes = {
                    name: value
                    for name, value in node.output().items()
                    if spec.property(name).editable
                    and current.properties.get(name) != value
                }
                if changes:
                    patches.append((current.key, changes))
            if patches:
                operations.append(
                    StoreOperation(
                        "patch_nodes",
                        {"spec": spec, "patches": tuple(patches)},
                    )
                )
        operations.extend(additional_writes)
        for (spec, create), entries in grouped(
            edges,
            key=lambda item: (
                item[0].spec,
                item[1] is None,
            ),
        ):
            batch = tuple(entries)
            if not create:
                continue
            operations.append(
                StoreOperation(
                    "create_edges",
                    {
                        "fields": (),
                        "spec": spec,
                        "edges": tuple(
                            (
                                known_keys.get(edge.source_ref),
                                known_keys.get(edge.target_ref),
                                edge.output(),
                            )
                            for edge, _ in batch
                        ),
                    },
                    node_key_refs={
                        ("edges", index, endpoint): ref
                        for index, (edge, _) in enumerate(batch)
                        for endpoint, ref in enumerate(
                            (edge.source_ref, edge.target_ref)
                        )
                        if ref not in known_keys
                    },
                )
            )
        return TransactionPlan(tuple(operations))

    @staticmethod
    async def _drafts(
        extractions: Sequence[Sequence[NodeDraft]],
        deduplicator: UserMemoryDeduplicator,
    ) -> tuple[tuple[NodeDraft, ...], tuple[NodeDraft, ...]]:
        """Flatten extracted Drafts and deduplicate only the UserMemory nodes."""
        drafts = tuple(draft for batch in extractions for draft in batch)
        profiles = tuple(draft for draft in drafts if draft.spec is USER)
        memories = tuple(
            await deduplicator.deduplicate(
                tuple(draft for draft in drafts if draft.spec is USER_MEMORY)
            )
        )
        return profiles, memories

    @staticmethod
    def _merge_memory_properties(
        current: NodeRecord,
        incoming: Mapping[str, object],
        timestamp: datetime,
        *,
        action: str = "merge",
    ) -> Mapping[str, object]:
        """Preserve meaning on merge and replace conflicting behavior explicitly."""
        source_refs = tuple(
            dict.fromkeys(
                ref
                for ref in (
                    *cast(
                        Sequence[str],
                        current.properties.get("source_refs")
                        or (current.properties.get("source_ref"),),
                    ),
                    *cast(Sequence[str], incoming.get("source_refs", ())),
                )
                if ref
            )
        )
        if action == "replace":
            return {
                **{
                    name: incoming[name]
                    for name in ("type", "description", "source_type", "status")
                },
                **{
                    name: incoming[name]
                    for name in ("source_ref", "evidence_at")
                    if name in incoming
                },
                "source_refs": source_refs,
                "triggers": tuple(incoming.get("triggers", ())),
                "updated_at": timestamp,
            }
        changes = {
            "source_type": (
                "user_explicit"
                if incoming.get("source_type") == "user_explicit"
                else current.properties.get("source_type")
            ),
            "source_refs": source_refs,
            "triggers": tuple(
                dict.fromkeys(
                    (
                        *current.properties.get("triggers", ()),
                        *incoming.get("triggers", ()),
                    )
                )
            ),
            "status": (
                "active"
                if incoming.get("status") == "active"
                else current.properties.get("status")
            ),
        }
        if any(
            current.properties.get(name, ()) != value for name, value in changes.items()
        ) or (
            evidence_time(incoming.get("evidence_at"))
            > evidence_time(current.properties.get("evidence_at"))
        ):
            changes.update(
                {
                    name: incoming[name]
                    for name in ("source_ref", "evidence_at")
                    if name in incoming
                }
            )
            changes["updated_at"] = timestamp
            return changes
        return {}

    def _require_read_scope(self, scope: ScopeBinding) -> None:
        """Require one cross-zone read capability for the plan target."""
        if type(scope) is not ScopeBinding:
            raise TypeError("scope must be a concrete ScopeBinding")
        self._store.access.require(scope, AccessMode.READ)
        if (
            scope.mode is not AccessMode.READ
            or scope.zone is not None
            or scope.datasource_id is not None
        ):
            raise PermissionError("memory ingest read scope does not match the request")

    def _require_write_scope(self, scope: ScopeBinding) -> None:
        """Require one User-owned write capability for plan execution."""
        if type(scope) is not ScopeBinding:
            raise TypeError("scope must be a concrete ScopeBinding")
        self._store.access.require(scope, AccessMode.WRITE)
        if (
            scope.zone != Zone.USER.value
            or scope.datasource_id is not None
            or scope.write_owner_id != scope.user_id
        ):
            raise PermissionError(
                "memory ingest write scope does not match the request"
            )


def _message_chunks(
    messages: Sequence[Mapping[str, str]],
    *,
    target_message_id: str | None = None,
) -> tuple[TextChunk, ...]:
    """Serialize validated evidence into one complete extraction request."""
    document = {"messages": [dict(value) for value in messages]}
    if target_message_id is not None:
        document["target_message_ids"] = [target_message_id]
    text = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return (TextChunk(0, text, 0, len(text)),)


def _with_evidence_time(
    node: NodeRecord, evidence_times: Mapping[str, str]
) -> NodeRecord:
    """Recover clocks for older graph rows from authoritative source references."""
    if node.properties.get("evidence_at"):
        return node
    stamp = evidence_times.get(str(node.properties.get("source_ref", "")), "")
    return (
        NodeRecord(node.spec, {**node.properties, "evidence_at": stamp})
        if stamp
        else node
    )


def _memory_id(workspace_id: str, user_id: str, draft: NodeDraft) -> str:
    """Derive replay-safe identity from the durable UserMemory meaning."""
    return user_memory_id(
        workspace_id, user_id, draft.properties["type"], draft.properties["description"]
    )


def _utc_timestamp(value: datetime | None) -> datetime:
    """Normalize one optional occurrence timestamp for graph persistence."""
    timestamp = value or datetime.now(UTC)
    if not isinstance(timestamp, datetime):
        raise TypeError("occurred_at must be a datetime or None")
    if timestamp.tzinfo is None:
        raise ValueError("occurred_at must be timezone-aware")
    return timestamp.astimezone(UTC)


__all__ = ["MemoryIngestPlan", "MemoryIngestService"]

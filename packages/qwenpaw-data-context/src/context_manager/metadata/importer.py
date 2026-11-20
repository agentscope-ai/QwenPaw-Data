"""Project validated SPEC 01 snapshots through canonical Store primitives."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from typing import cast

from ..executor import BoundedExecutor
from ..model import ModelService
from ..process import EmbeddingEnricher
from ..schema import (
    ANCHORED_TO,
    AVAILABLE_IN,
    CALIBER_ON,
    COLUMN,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION,
    DIMENSION_BINDING,
    DOMAIN,
    HAS_CALIBER,
    HAS_DATASET,
    HAS_DIMENSION,
    HAS_METRIC,
    HAS_TABLE,
    JOINS,
    MAPS_TO_FIELD,
    METRIC,
    METRIC_CALIBER,
    TABLE,
    TABLE_HAS_COLUMN_COLUMN,
    VERIFIED_QUERY,
    EdgeSpec,
    KeySpec,
    NodeSpec,
    PropertyOwner,
    Zone,
)
from ..auth import GLOBAL_OWNER_ID, ScopeBinding
from ..store import (
    EdgeDraft,
    EdgeRecord,
    GraphStore,
    NodeDraft,
    NodeRecord,
    StoreError,
)
from ..transaction import (
    StoreOperation,
    TransactionPlan,
    TransactionResult,
)
from ..transaction.recovery import RecoveryService, RecoveryStackSession
from ..utils.validation import require_trimmed_text
from ..utils.batches import grouped
from .contract import (
    DatasetPayload,
    DatasourcePayload,
    DimensionBindingPayload,
    DimensionPayload,
    DomainPayload,
    MetricPayload,
    PhysicalJoinPayload,
    SemanticImportRequest,
    TablePayload,
    VerifiedQueryPayload,
)
from .weaving.vocabulary import (
    additivity,
    binding_properties,
    dimension_column,
    dimension_role,
    priority,
)
from .writer import MetadataWriter

_SOURCE = "semantic_config"
# FULL rebuilds this semantic projection, preserving DataSource, Table and Column.
_FULL_REBUILD_NODES = (
    VERIFIED_QUERY,
    METRIC_CALIBER,
    DATASET_FIELD,
    METRIC,
    DIMENSION,
    DATASET,
    DOMAIN,
)
_FULL_REBUILD_EDGES = (
    ANCHORED_TO,
    AVAILABLE_IN,
    CALIBER_ON,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    DIMENSION_BINDING,
    MAPS_TO_FIELD,
    HAS_DATASET,
    HAS_DIMENSION,
    HAS_CALIBER,
    HAS_METRIC,
    JOINS,  # FULL also replaces declared joins while preserving physical tables.
)


@dataclass(frozen=True, slots=True)
class MetadataImportPlan:
    """Hold scoped snapshot facts and the ordered Store operations to commit them."""

    tenant_id: str
    workspace_id: str
    owner_id: str
    datasource_id: str
    nodes: tuple[NodeDraft, ...]
    edges: tuple[EdgeDraft, ...]
    warnings: tuple[str, ...]
    transaction: TransactionPlan

    def __post_init__(self) -> None:
        """Freeze facts and validate unique identities and resolved, typed endpoints."""
        for name in ("tenant_id", "workspace_id", "owner_id", "datasource_id"):
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
        if any(node.spec.zone is not Zone.METADATA for node in nodes):
            raise ValueError("plan nodes must belong to Metadata Graph")
        if len({node.ref for node in nodes}) != len(nodes):
            raise ValueError("plan nodes must have unique refs")
        if type(self.transaction) is not TransactionPlan:
            raise TypeError("plan transaction must be a TransactionPlan")
        node_specs = {node.ref: node.spec for node in nodes}
        edge_keys: set[tuple[EdgeSpec, str, str]] = set()
        for edge in edges:
            if any(
                spec.zone is not Zone.METADATA
                for spec in (edge.spec.source, edge.spec.target)
            ):
                raise ValueError("plan edges must belong to Metadata Graph")
            # External endpoints are resolved during preparation; check local endpoint types here.
            for ref, spec in (
                (edge.source_ref, edge.spec.source),
                (edge.target_ref, edge.spec.target),
            ):
                if ref in node_specs and node_specs[ref] is not spec:
                    raise ValueError(
                        "plan edge endpoints must resolve to matching node specs"
                    )
            identity = (edge.spec, edge.source_ref, edge.target_ref)
            if identity in edge_keys:
                raise ValueError("plan edges cannot contain duplicates")
            edge_keys.add(identity)
        if any(not isinstance(warning, str) or not warning for warning in warnings):
            raise ValueError("plan warnings must contain non-empty text")
        object.__setattr__(self, "nodes", nodes)
        object.__setattr__(self, "edges", edges)
        object.__setattr__(self, "warnings", warnings)


class MetadataImportService:
    """Prepare and publish a datasource snapshot through persistent recovery.

    Projection and standalone execute_plan do not require RecoveryService.
    Runtime supplies it after startup for whole-snapshot imports.
    """

    def __init__(
        self,
        store: GraphStore,
        models: ModelService,
        *,
        executor: BoundedExecutor,
        recovery: RecoveryService | None = None,
    ) -> None:
        self._store = store
        self._models = models
        self._executor = executor
        self._recovery = recovery
        self._writer = MetadataWriter(store, executor=executor)

    async def import_snapshot(
        self,
        request: SemanticImportRequest,
        scope: ScopeBinding,
        *,
        before_commit=None,
    ) -> TransactionResult:
        """Recover all committed batches if preparation or publication fails."""
        if self._recovery is None:
            raise RuntimeError("snapshot import requires a RecoveryService")
        async with self._recovery.run(scope, task_type="import") as transaction:
            return await self._writer.consume(
                self._prepare_batches(request, scope),
                partial(self.execute_plan, transaction=transaction),
                scope,
                before_commit=before_commit,
            )

    async def execute_plan(
        self,
        plan: MetadataImportPlan,
        scope: ScopeBinding,
        *,
        transaction: RecoveryStackSession | None = None,
    ) -> TransactionResult:
        """Execute one plan, optionally as a batch of the caller's recovery session."""
        if (
            scope.write_owner_id != GLOBAL_OWNER_ID
            or scope.zone != Zone.METADATA.value
            or (
                scope.tenant_id,
                scope.workspace_id,
                scope.write_owner_id,
                scope.datasource_id,
            )
            != (plan.tenant_id, plan.workspace_id, plan.owner_id, plan.datasource_id)
        ):
            raise PermissionError("metadata plan scope does not match the request")
        return await self._writer.execute(
            plan.transaction,
            scope,
            execute=transaction.execute if transaction is not None else None,
        )

    def project_snapshot(
        self,
        request: SemanticImportRequest,
        scope: ScopeBinding,
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Lazily map snapshot fields without selecting or executing mutations."""
        datasource_key = self._node_key(DATA_SOURCE, {}, scope)
        datasource = self._store.get_nodes(DATA_SOURCE, (datasource_key,), scope=scope)[
            0
        ]
        if request.datasource is not None:
            yield self._datasource(request.datasource, scope)
        elif any(domain.datasets for domain in request.semantic.domains):
            # Develop creates a datasource anchor for Dataset.AVAILABLE_IN.
            yield NodeDraft.build(
                self._store.schema,
                DATA_SOURCE,
                datasource_key,
                {
                    "datasource_name": scope.datasource_id,
                    "datasource_type": "",
                },
                transient={"reference_only": True},
            )
        visible = (
            request.datasource is not None
            or datasource is not None
            or any(domain.datasets for domain in request.semantic.domains)
        )
        for table in request.tables:
            yield from self._table(table, scope, datasource_visible=visible)
        yield from self._physical_joins(request.physical_joins, scope)
        for domain in request.semantic.domains:
            yield from self._domain(domain, scope)

    def _require_import_scope(
        self, request: SemanticImportRequest, scope: ScopeBinding
    ) -> None:
        if (
            scope.datasource_id != request.datasource_id
            or scope.zone != Zone.METADATA.value
            or scope.write_owner_id != GLOBAL_OWNER_ID
        ):
            raise PermissionError(
                "metadata import requires the authoritative workspace scope matching the request"
            )

    async def _prepare_batches(
        self, request: SemanticImportRequest | dict, scope: ScopeBinding
    ):
        """Yield one batch's preparation while keeping its model client alive."""
        request = await self._executor.run(
            SemanticImportRequest.model_validate, request
        )
        self._require_import_scope(request, scope)
        records_to_delete = {}
        existing_nodes: dict[tuple[NodeSpec, str], NodeRecord | None] = {}
        existing_edges: dict[tuple[EdgeSpec, str, str], EdgeRecord | None] = {}
        new_nodes: dict[tuple[NodeSpec, str], NodeDraft] = {}
        new_edges: dict[tuple[EdgeSpec, str, str], EdgeDraft] = {}
        warnings: list[str] = []

        def prepare():
            if request.drop_semantic_first:
                records_to_delete.update(
                    (spec, self._read_rebuild_records(spec, scope))
                    for spec in (*_FULL_REBUILD_EDGES, *_FULL_REBUILD_NODES)
                )
            timestamp = datetime.now(timezone.utc).isoformat()

            def existing_node(spec: NodeSpec, key: str) -> NodeRecord | None:
                identity = (spec, key)
                if identity not in existing_nodes:
                    existing_nodes[identity] = self._store.get_nodes(
                        spec, (key,), scope=scope
                    )[0]
                return existing_nodes[identity]

            datasource_key = self._node_key(DATA_SOURCE, {}, scope)
            datasource = existing_node(DATA_SOURCE, datasource_key)
            datasource_visible = (
                request.datasource is not None
                or datasource is not None
                or any(domain.datasets for domain in request.semantic.domains)
            )
            if not datasource_visible and (
                request.tables or request.physical_joins or request.semantic.domains
            ):
                warnings.append(
                    "DataSource is absent from Metadata Graph; DataSource-to-Table HAS relationships were skipped"
                )
            drafts = self.project_snapshot(request, scope)

            for draft in drafts:
                if isinstance(draft, NodeDraft):
                    identity = (draft.spec, draft.ref)
                    current = existing_node(*identity)
                    if draft.transient.get("reference_only") and (
                        identity in new_nodes
                        or (current is not None and draft.spec not in records_to_delete)
                    ):
                        continue
                    previous = new_nodes.get(identity)
                    inherited = (
                        {
                            prop.name: current.properties[prop.name]
                            for prop in draft.spec.properties
                            if prop.owner is PropertyOwner.CALLER
                            and prop.name not in {"embedding", "embedding_hash"}
                            and prop.name in current.properties
                        }
                        if current is not None and draft.spec not in records_to_delete
                        else {}
                    )
                    new_nodes[identity] = replace(
                        draft,
                        properties={
                            **inherited,
                            **(previous.properties if previous is not None else {}),
                            **draft.properties,
                            "updated_at": timestamp,
                        },
                        transient={},
                    )
                else:
                    identity = (draft.spec, draft.source_ref, draft.target_ref)
                    previous = new_edges.get(identity)
                    new_edges[identity] = replace(
                        draft,
                        properties={
                            **(previous.properties if previous is not None else {}),
                            **draft.properties,
                        },
                    )

            for identity, edge in new_edges.items():
                for spec, key in (
                    (edge.spec.source, edge.source_ref),
                    (edge.spec.target, edge.target_ref),
                ):
                    current = existing_node(spec, key)
                    if (spec, key) not in new_nodes and (
                        current is None or spec in records_to_delete
                    ):
                        raise StoreError(
                            "metadata edge endpoint is missing from the prepared graph",
                            operation="metadata_import",
                            code="metadata_endpoint_missing",
                        )
                existing_edges[identity] = (
                    self._store.get_edges(
                        identity[0],
                        ((identity[1], identity[2]),),
                        scope=scope,
                    )
                )[0]

        async with self._models.embedder() as embedder:
            await self._executor.run(prepare)
            deletions = (
                record for records in records_to_delete.values() for record in records
            )
            batches = self._writer.batches(
                new_nodes.values(), new_edges.values(), deletions
            )
            for nodes, edges, deleted in batches:

                async def prepare_batch():
                    prepared, pending = await self._executor.run_sync(
                        EmbeddingEnricher.prepare, nodes
                    )
                    vectors = (
                        await embedder.embed(tuple(text for _, text in pending))
                        if pending
                        else ()
                    )

                    def compile_plan():
                        enriched = EmbeddingEnricher.apply(prepared, pending, vectors)
                        return self._compile_plan(
                            enriched,
                            edges,
                            scope,
                            warnings=tuple(warnings),
                            existing_nodes=existing_nodes,
                            existing_edges=existing_edges,
                            records_to_delete={
                                spec: tuple(rows)
                                for spec, rows in grouped(
                                    deleted, key=lambda row: row.spec
                                )
                            },
                            rebuilt_specs=records_to_delete,
                        )

                    return await self._executor.run_sync(compile_plan)

                yield prepare_batch

    def _compile_plan(
        self,
        new_nodes: tuple[NodeDraft, ...],
        new_edges: tuple[EdgeDraft, ...],
        scope: ScopeBinding,
        *,
        warnings: tuple[str, ...],
        existing_nodes: Mapping[tuple[NodeSpec, str], NodeRecord | None],
        existing_edges: Mapping[tuple[EdgeSpec, str, str], EdgeRecord | None],
        records_to_delete: Mapping[
            NodeSpec | EdgeSpec, Sequence[NodeRecord | EdgeRecord]
        ],
        rebuilt_specs: Mapping,
    ) -> MetadataImportPlan:
        """Compile final facts and FULL targets into ordered deletes, creates and patches."""
        operations: list[StoreOperation] = []
        # FULL removes exact managed edges before nodes; physical nodes and foreign scopes remain.
        for spec, records in records_to_delete.items():
            if not records:
                continue
            if isinstance(spec, NodeSpec):
                operations.append(
                    StoreOperation(
                        "delete_nodes",
                        {"spec": spec, "keys": tuple(record.key for record in records)},
                    )
                )
            else:
                operations.append(
                    StoreOperation(
                        "delete_edges",
                        {
                            "spec": spec,
                            "endpoints": tuple(
                                (record.source_key, record.target_key)
                                for record in records
                            ),
                        },
                    )
                )
        # Merge independent writes within this transaction, retaining node/edge phases.
        for (spec, create), drafts in grouped(
            new_nodes,
            key=lambda draft: (
                draft.spec,
                existing_nodes[(draft.spec, draft.ref)] is None
                or draft.spec in rebuilt_specs,
            ),
        ):
            batch = tuple(drafts)
            if create:
                operations.append(StoreOperation.project_nodes(batch))
                continue
            patches = []
            for draft in batch:
                current = existing_nodes[(spec, draft.ref)]
                changes = {
                    name: value
                    for name, value in draft.output().items()
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
        for (spec, create), drafts in grouped(
            new_edges,
            key=lambda draft: (
                draft.spec,
                existing_edges[(draft.spec, draft.source_ref, draft.target_ref)] is None
                or draft.spec in rebuilt_specs,
            ),
        ):
            batch = tuple(drafts)
            if create:
                # Metadata refs are deterministic keys prepared from the logical KeySpec.
                operations.append(
                    StoreOperation(
                        "create_edges",
                        {
                            "fields": (),
                            "spec": spec,
                            "edges": tuple(
                                (draft.source_ref, draft.target_ref, draft.output())
                                for draft in batch
                            ),
                        },
                    )
                )
                continue
            patches = []
            for draft in batch:
                current = existing_edges[(spec, draft.source_ref, draft.target_ref)]
                changes = {
                    name: value
                    for name, value in draft.output().items()
                    if spec.property(name).editable
                    and current.properties.get(name) != value
                }
                if changes:
                    patches.append((current.source_key, current.target_key, changes))
            if patches:
                operations.append(
                    StoreOperation(
                        "patch_edges",
                        {"spec": spec, "patches": tuple(patches)},
                    )
                )
        return MetadataImportPlan(
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            owner_id=scope.write_owner_id,
            datasource_id=scope.datasource_id,
            nodes=new_nodes,
            edges=new_edges,
            warnings=warnings,
            transaction=TransactionPlan(tuple(operations)),
        )

    def _read_rebuild_records(
        self,
        spec: NodeSpec | EdgeSpec,
        scope: ScopeBinding,
    ) -> tuple[NodeRecord | EdgeRecord, ...]:
        """Read only the write owner's FULL targets, excluding visible shared records."""
        records: list[NodeRecord | EdgeRecord] = []
        read = (
            self._store.list_nodes
            if isinstance(spec, NodeSpec)
            else self._store.list_edges
        )
        while True:
            page = tuple(
                read(
                    spec,
                    filters={"owner_id": scope.write_owner_id},
                    limit=1000,
                    offset=len(records),
                    scope=scope,
                )
            )
            records.extend(page)
            if len(page) < 1000:
                return tuple(records)

    def _datasource(
        self,
        payload: DatasourcePayload,
        scope: ScopeBinding,
    ) -> NodeDraft:
        """Project catalog identity and display metadata, never connection config."""
        values: dict[str, object] = {
            "datasource_name": payload.datasource_name,
            "datasource_type": payload.datasource_type,
            "description": payload.description or None,
        }
        return NodeDraft.build(
            self._store.schema,
            DATA_SOURCE,
            self._node_key(DATA_SOURCE, {}, scope),
            values,
        )

    def _table(
        self, table: TablePayload, scope: ScopeBinding, *, datasource_visible: bool
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Project one flattened physical table and its columns."""
        name = require_trimmed_text(table.name, "table name")
        table_key = self._node_key(TABLE, {"name": name}, scope)
        yield NodeDraft.build(
            self._store.schema,
            TABLE,
            table_key,
            {
                "name": name,
                "comment": table.comment or None,
                "partition_keys": list(table.partition_keys),
            },
        )
        if datasource_visible:
            yield self._edge(
                HAS_TABLE, self._node_key(DATA_SOURCE, {}, scope), table_key
            )
        for column in table.columns:
            column_name = require_trimmed_text(column.name, "column name")
            column_key = self._node_key(
                COLUMN, {"table": name, "name": column_name}, scope
            )
            yield NodeDraft.build(
                self._store.schema,
                COLUMN,
                column_key,
                {
                    "table": name,
                    "name": column_name,
                    "data_type": column.data_type,
                    "is_primary": column.is_primary,
                    "is_nullable": column.is_nullable,
                    "comment": column.comment or None,
                    "sample_values": list(column.sample_values),
                },
            )
            yield self._edge(TABLE_HAS_COLUMN_COLUMN, table_key, column_key)

    def _physical_joins(
        self, joins: Sequence[PhysicalJoinPayload], scope: ScopeBinding
    ) -> Iterator[EdgeDraft]:
        """Project declared physical joins between this datasource's tables."""
        for join in joins:
            left_table = require_trimmed_text(join.left_table, "left table")
            right_table = require_trimmed_text(join.right_table, "right table")
            left_key = self._node_key(TABLE, {"name": left_table}, scope)
            right_key = self._node_key(TABLE, {"name": right_table}, scope)
            yield self._edge(
                JOINS,
                left_key,
                right_key,
                {
                    "key": f"pjn:{scope.datasource_id}:{left_table}:{'$$$'.join(join.left_columns)}:{right_table}:{'$$$'.join(join.right_columns)}",
                    "source": "weave_p4",
                    "left_columns": list(join.left_columns),
                    "right_columns": list(join.right_columns),
                    "join_type": join.join_type or None,
                },
            )

    def _domain(
        self, domain: DomainPayload, scope: ScopeBinding
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Project one domain and its v2.1 logical aggregate.

        ①a：``domain_id`` 是身份（图 key 的 domain 段取值），缺省回落 ``name``；
        ``name`` 仅为显示名。
        """
        identity = require_trimmed_text(domain.domain_id or domain.name, "domain id")
        display = require_trimmed_text(domain.name, "domain name")
        domain_key = self._node_key(DOMAIN, {"domain_id": identity}, scope)
        yield NodeDraft.build(
            self._store.schema,
            DOMAIN,
            domain_key,
            {
                "domain_id": identity,
                "name": display,
                "description": domain.description or None,
                "datasource_scope": list(domain.datasource_scope),
                "source": _SOURCE,
            },
        )
        for dataset in domain.datasets:
            yield from self._dataset(identity, domain_key, dataset, scope)
        for dimension in domain.dimensions:
            yield from self._dimension(identity, domain_key, dimension, scope)
        for metric in domain.metrics:
            yield from self._metric(identity, domain_key, metric, scope)
        for query in domain.verified_queries:
            yield from self._verified_query(identity, domain_key, query, scope)

    def _dataset(
        self,
        domain: str,
        domain_key: str,
        dataset: DatasetPayload,
        scope: ScopeBinding,
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Project one dataset and its logical fields."""
        name = require_trimmed_text(dataset.name, "dataset name")
        dataset_key = self._node_key(DATASET, {"domain": domain, "name": name}, scope)
        yield NodeDraft.build(
            self._store.schema,
            DATASET,
            dataset_key,
            {
                "domain": domain,
                "name": name,
                "description": dataset.description or None,
                "source": dataset.source,
            },
        )
        yield self._edge(HAS_DATASET, domain_key, dataset_key)
        yield self._edge(
            AVAILABLE_IN, dataset_key, self._node_key(DATA_SOURCE, {}, scope)
        )
        for dataset_field in dataset.dataset_fields:
            field_name = require_trimmed_text(dataset_field.name, "dataset field name")
            field_key = self._node_key(
                DATASET_FIELD,
                {"domain": domain, "dataset": name, "name": field_name},
                scope,
            )
            yield NodeDraft.build(
                self._store.schema,
                DATASET_FIELD,
                field_key,
                {
                    "domain": domain,
                    "dataset": name,
                    "name": field_name,
                    "description": dataset_field.description or None,
                    "data_type": dataset_field.data_type or "text",
                    "is_primary": dataset_field.is_primary,
                    "is_nullable": dataset_field.is_nullable,
                },
            )
            yield self._edge(
                DATASET_HAS_COLUMN_DATASET_FIELD,
                dataset_key,
                field_key,
            )

    def _dimension(
        self,
        domain: str,
        domain_key: str,
        dimension: DimensionPayload,
        scope: ScopeBinding,
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Project one dimension and its dataset bindings."""
        name = require_trimmed_text(dimension.name, "dimension name")
        dimension_key = self._node_key(
            DIMENSION, {"domain": domain, "name": name}, scope
        )
        yield NodeDraft.build(
            self._store.schema,
            DIMENSION,
            dimension_key,
            {
                "domain": domain,
                "name": name,
                "synonyms": list(dimension.aliases),
                "description": dimension.description or None,
                "dimension_role": dimension_role(dimension.dimension_type),
                "child_dimensions": list(dimension.child_dimensions),
                "priority": priority(dimension.priority),
                "source": _SOURCE,
            },
        )
        yield self._edge(HAS_DIMENSION, domain_key, dimension_key)
        for binding in dimension.dimension_bindings:
            yield from self._dimension_binding(domain, dimension_key, binding, scope)

    def _dimension_binding(
        self,
        domain: str,
        dimension_key: str,
        binding: DimensionBindingPayload,
        scope: ScopeBinding,
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Map a dimension expression to develop's inferred DatasetField anchor."""
        dataset = require_trimmed_text(binding.dataset, "dimension dataset")
        placeholder = self._dataset_placeholder(domain, dataset, scope)
        yield placeholder
        column = dimension_column(binding.expr)
        if not column:
            return
        values = {"domain": domain, "dataset": dataset, "name": column}
        field_key = self._node_key(DATASET_FIELD, values, scope)
        yield NodeDraft.build(
            self._store.schema,
            DATASET_FIELD,
            field_key,
            values,
            transient={"reference_only": True},
        )
        yield self._edge(DATASET_HAS_COLUMN_DATASET_FIELD, placeholder.ref, field_key)
        yield self._edge(
            MAPS_TO_FIELD,
            dimension_key,
            field_key,
            binding_properties(binding.expr, binding.value_example),
        )

    def _metric(
        self,
        domain: str,
        domain_key: str,
        metric: MetricPayload,
        scope: ScopeBinding,
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Project one metric and its dataset-bound calibers."""
        name = require_trimmed_text(metric.name, "metric name")
        metric_key = self._node_key(METRIC, {"domain": domain, "name": name}, scope)
        yield NodeDraft.build(
            self._store.schema,
            METRIC,
            metric_key,
            {
                "domain": domain,
                "name": name,
                "synonyms": list(metric.aliases),
                "description": metric.description or None,
                "priority": priority(metric.priority),
                "source": _SOURCE,
            },
        )
        yield self._edge(HAS_METRIC, domain_key, metric_key)
        for caliber in metric.metric_calibers:
            dataset = require_trimmed_text(caliber.dataset, "caliber dataset")
            time_window = (caliber.time_window or "").strip()
            caliber_key = self._node_key(
                METRIC_CALIBER,
                {
                    "domain": domain,
                    "metric_name": name,
                    "dataset": dataset,
                    "time_window": time_window,
                },
                scope,
            )
            placeholder = self._dataset_placeholder(domain, dataset, scope)
            yield placeholder
            dataset_key = placeholder.ref
            yield NodeDraft.build(
                self._store.schema,
                METRIC_CALIBER,
                caliber_key,
                {
                    "domain": domain,
                    "metric_name": name,
                    "dataset": dataset,
                    "expr": caliber.expr,
                    "time_window": time_window,
                    "additivity": additivity(caliber.additivity),
                    "is_default": caliber.is_default,
                    "source": _SOURCE,
                },
            )
            yield self._edge(HAS_CALIBER, metric_key, caliber_key)
            yield self._edge(CALIBER_ON, caliber_key, dataset_key)

    def _verified_query(
        self,
        domain: str,
        domain_key: str,
        query: VerifiedQueryPayload,
        scope: ScopeBinding,
    ) -> Iterator[NodeDraft | EdgeDraft]:
        """Project one verified query and anchor it to its domain."""
        question = require_trimmed_text(query.question, "verified query question")
        verified_sql = require_trimmed_text(query.verified_sql, "verified SQL")
        question_hash = hashlib.sha256(question.encode("utf-8")).hexdigest()[:16]
        query_key = self._node_key(
            VERIFIED_QUERY, {"domain": domain, "question_hash": question_hash}, scope
        )
        yield NodeDraft.build(
            self._store.schema,
            VERIFIED_QUERY,
            query_key,
            {
                "domain": domain,
                "question": question,
                "question_hash": question_hash,
                "verified_sql": verified_sql,
                "source": query.source_channel or None,
            },
        )
        yield self._edge(ANCHORED_TO, query_key, domain_key)

    def _edge(
        self,
        spec: EdgeSpec,
        source_ref: str,
        target_ref: str,
        values: Mapping[str, object] | None = None,
    ) -> EdgeDraft:
        """Project one edge; reads and operation selection belong to plan preparation."""
        return EdgeDraft.build(
            self._store.schema,
            spec,
            source_ref,
            target_ref,
            {
                name: value
                for name, value in (values or {}).items()
                if value is not None
            },
        )

    @staticmethod
    def _node_key(
        spec: NodeSpec, values: Mapping[str, object], scope: ScopeBinding
    ) -> str:
        """Use the declared deterministic key for lookup and plan-local references."""
        key_spec = cast(KeySpec, spec.property("key"))
        return key_spec.generate(
            {
                "workspace_id": scope.workspace_id,
                "owner_id": scope.write_owner_id,
                "datasource_id": scope.datasource_id,
                **values,
            }
        )

    def _dataset_placeholder(
        self, domain: str, name: str, scope: ScopeBinding
    ) -> NodeDraft:
        """Describe an edge-only dataset; preparation keeps existing or explicit facts."""
        values = {
            "domain": domain,
            "name": name,
            "source": "",
        }
        return NodeDraft.build(
            self._store.schema,
            DATASET,
            self._node_key(DATASET, values, scope),
            values,
            transient={"reference_only": True},
        )


__all__ = ["MetadataImportPlan", "MetadataImportService"]

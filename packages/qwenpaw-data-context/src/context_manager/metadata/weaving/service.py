"""Execute snapshot weaving and committed editor changes through scoped Store operations."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial
from itertools import islice
from uuid import uuid4

from ...model.usage import bind_usage_context
from ...process import EmbeddingEnricher
from ...schema import (
    DATA_SOURCE,
    TABLE,
    COLUMN,
    DOMAIN,
    DATASET,
    DATASET_FIELD,
    METRIC,
    DIMENSION,
    VERIFIED_QUERY,
    MAPS_TO_FIELD,
    DATASET_HAS_COLUMN_DATASET_FIELD,
    HAS_TABLE,
    TABLE_HAS_COLUMN_COLUMN,
    NodeSpec,
    PropertyOwner,
    Zone,
)
from ...executor import BoundedExecutor
from ...auth import GLOBAL_OWNER_ID, ScopeBinding
from ...store import NodeDraft
from ...transaction import StoreOperation, TransactionPlan
from ...transaction.recovery import RecoveryService, RecoveryStackSession
from ...utils.batches import grouped
from ..contract import SemanticImportRequest
from ..writer import MetadataWriter
from .integration import complete_snapshot
from .projection import Projection, project

log = logging.getLogger(__name__)
SHARED_NODES = (DOMAIN, METRIC, DIMENSION, VERIFIED_QUERY)


@dataclass(frozen=True)
class MetadataWeavingPlan:
    """One compiled transaction with the issued scope for each operation."""

    transaction: TransactionPlan
    operation_scopes: tuple[ScopeBinding, ...]
    warnings: tuple[str, ...] = ()


class MetadataWeavingService:
    """Publish editor commits and weave datasource snapshots into authoritative MG."""

    def __init__(
        self,
        store,
        models,
        importer,
        repository,
        *,
        physical_mapping,
        executor: BoundedExecutor,
        narrow_write_scope: Callable[..., ScopeBinding],
        recovery: RecoveryService,
    ):
        self._store = store
        self._models = models
        self._importer = importer
        self._repository = repository
        self.physical_mapping = physical_mapping
        self._executor = executor
        self._narrow_write_scope = narrow_write_scope
        self._recovery = recovery
        self._writer = MetadataWriter(store, executor=executor)

    async def publish_changes(self, before, after, scope, *, entity):
        """Publish committed editor changes before the request returns."""
        self._require_scope(scope)
        task_id = f"SYNC_{uuid4().hex}"
        error, changes = None, ()
        skipped_dataset_ids = frozenset()
        # TODO: coordinate publication order across instances and reconcile SQL/graph commits.
        try:

            def project_changes():
                old = project(before, self._store.schema, scope.write_owner_id)
                new = project(after, self._store.schema, scope.write_owner_id)
                return old, new, self._changes(old, new)

            old, new, changes = await self._executor.run(project_changes)
            skipped_dataset_ids = new.datasets_without_datasource
            with bind_usage_context(
                source="语义资产导入",
                source_step=f"semantic_asset_import#{task_id}",
                session_id="",
                chat_id="",
                probe=False,
            ):
                await self._writer.consume(
                    await self._prepare_changes(old, new, scope),
                    self._execute_plan,
                    scope,
                )
        except Exception as exc:
            error = exc
            log.exception(
                "Metadata publication failed task=%s entity=%s", task_id, entity
            )
        try:
            await self._executor.run(
                self._repository.record,
                task_id,
                entity,
                scope,
                changes,
                str(error) if error is not None else None,
                skipped_dataset_ids=skipped_dataset_ids,
            )
        except Exception:
            log.exception(
                "Metadata publication result could not be recorded task=%s", task_id
            )
        if error is not None:
            raise error
        return task_id

    def _require_scope(self, scope):
        if (
            scope.write_owner_id != GLOBAL_OWNER_ID
            or scope.zone != "metadata"
            or scope.datasource_id is not None
        ):
            raise PermissionError(
                "weaving requires the authoritative workspace Metadata scope"
            )

    @staticmethod
    def _changes(old, new):
        changes = []
        for identity, node in old.nodes.items():
            if identity not in new.nodes and (
                node.spec is DATASET_FIELD or not node.transient.get("reference_only")
            ):
                changes.append(("delete", node.spec.name, identity))
        for identity, edge in old.edges.items():
            if identity not in new.edges and not edge.transient.get("reference_only"):
                changes.append(
                    (
                        "delete",
                        edge.spec.relationship,
                        f"{edge.source_ref}->{edge.target_ref}",
                    )
                )
        for identity, node in new.nodes.items():
            if (
                identity not in old.nodes
                or node.properties != old.nodes[identity].properties
                or new.datasources[identity] != old.datasources[identity]
            ):
                changes.append(("upsert", node.spec.name, identity))
        for identity, edge in new.edges.items():
            if (
                identity not in old.edges
                or edge.properties != old.edges[identity].properties
            ):
                changes.append(
                    (
                        "upsert",
                        edge.spec.relationship,
                        f"{edge.source_ref}->{edge.target_ref}",
                    )
                )
        return tuple(changes)

    def _read_graph_page(self, spec, scope, offset):
        """Read reconciliation facts without fetching old vectors."""
        if isinstance(spec, NodeSpec):
            return tuple(
                self._store.list_nodes(
                    spec,
                    scope=scope,
                    limit=self._writer.max_rows,
                    offset=offset,
                    fields=tuple(
                        prop.name
                        for prop in spec.properties
                        if prop.name != "embedding"
                    ),
                )
            )
        return self._store.list_edges(
            spec, scope=scope, limit=self._writer.max_rows, offset=offset
        )

    async def _read_graph(self, scope):
        """Read independent pages, yielding admission between them before any deletes.

        FULL needs the complete topology to preserve shared objects and fix its
        deletion set. No write transaction or consistent snapshot spans pages.
        """
        nodes, edges = {}, {}
        for spec in (*self._store.schema.nodes, *self._store.schema.edges):
            node = isinstance(spec, NodeSpec)
            if (
                spec.zone is not Zone.METADATA
                if node
                else spec.source.zone is not Zone.METADATA
                or spec.target.zone is not Zone.METADATA
            ):
                continue
            offset = 0
            while True:
                rows = await self._executor.run(
                    self._read_graph_page, spec, scope, offset
                )
                if node:
                    nodes.update((row.key, row) for row in rows)
                else:
                    edges.update(
                        ((spec, row.source_key, row.target_key), row) for row in rows
                    )
                if len(rows) < self._writer.max_rows:
                    break
                offset += len(rows)
        return nodes, edges

    async def _prepare_changes(self, old: Projection, new: Projection, scope):
        current, edges = await self._read_graph(scope)

        def compare():
            changed = {
                key
                for key, node in new.nodes.items()
                if key not in old.nodes
                or node.properties != old.nodes[key].properties
                or new.datasources[key] != old.datasources[key]
            }
            changed_edges = {
                key
                for key, edge in new.edges.items()
                if key not in old.edges or edge.properties != old.edges[key].properties
            }
            selected = changed | {
                ref for _, source, target in changed_edges for ref in (source, target)
            }
            # Include missing parents/anchors without overwriting unrelated existing objects.
            while True:
                extra = set()
                for identity, edge in new.edges.items():
                    if (
                        edge.source_ref in selected and edge.source_ref not in current
                    ) or (
                        edge.target_ref in selected and edge.target_ref not in current
                    ):
                        changed_edges.add(identity)
                        extra.update((edge.source_ref, edge.target_ref))
                extra -= selected
                if not extra:
                    break
                selected |= extra
            incoming = Projection(
                {
                    key: value
                    for key, value in new.nodes.items()
                    if key in changed or key in selected and key not in current
                },
                {
                    key: value
                    for key, value in new.edges.items()
                    if key in changed_edges
                },
                new.datasources,
            )
            moved = {
                key
                for key in changed
                if key in old.nodes and old.datasources[key] != new.datasources[key]
            }
            removed_edges = {
                key
                for key, edge in old.edges.items()
                if key not in new.edges and not edge.transient.get("reference_only")
            }
            changed_bindings = (old.bindings - new.bindings) | {
                edge.transient["binding"]
                for identity, edge in old.edges.items()
                if "binding" in edge.transient
                and (
                    identity not in new.edges
                    or edge.properties != new.edges[identity].properties
                )
            }
            # Reconcile edited bindings, including mappings retained by earlier releases.
            for dimension, dataset in changed_bindings:
                parent = old.nodes[dataset]
                removed_edges.update(
                    identity
                    for identity, edge in edges.items()
                    if identity not in new.edges
                    and edge.spec is MAPS_TO_FIELD
                    and edge.source_key == dimension
                    and current[edge.target_key].properties.get("domain")
                    == parent.properties["domain"]
                    and current[edge.target_key].properties.get("dataset")
                    == parent.properties["name"]
                )
            removed_nodes = {
                key
                for key, node in old.nodes.items()
                if key not in new.nodes and not node.transient.get("reference_only")
            } | moved
            removed_datasets = {
                (node.properties["domain"], node.properties["name"])
                for key, node in old.nodes.items()
                if key not in new.nodes and node.spec is DATASET
            }
            fields = {
                key
                for key, node in old.nodes.items()
                if key not in new.nodes and node.spec is DATASET_FIELD
            }
            fields.update(
                target
                for spec, _, target in removed_edges
                if spec is MAPS_TO_FIELD and target not in new.nodes
            )
            # Dataset removal also collects orphan fields left by older publications.
            removed_nodes.update(
                key
                for key, node in current.items()
                if node.spec is DATASET_FIELD
                and (node.properties["domain"], node.properties["dataset"])
                in removed_datasets
            )
            referenced = {
                key
                for identity, edge in edges.items()
                if identity not in removed_edges
                and edge.spec is not DATASET_HAS_COLUMN_DATASET_FIELD
                and edge.source_key not in removed_nodes
                and edge.target_key not in removed_nodes
                for key in (edge.source_key, edge.target_key)
            }
            removed_nodes.update(fields - referenced)
            return incoming, current, edges, removed_nodes, removed_edges, moved

        (
            incoming,
            current,
            edges,
            removed_nodes,
            removed_edges,
            moved,
        ) = await self._executor.run(compare)
        return self._compile(
            incoming,
            scope,
            current,
            edges,
            scopes={scope.datasource_id: scope},
            remove_nodes=removed_nodes,
            remove_edges=removed_edges,
            moved=moved,
            all_new_edges=new.edges,
        )

    async def _prepare_batches(self, request, scope):
        """Read the snapshot in pages and return lazy batch preparation."""

        async def complete():
            validated = await self._executor.run_sync(
                SemanticImportRequest.model_validate, request
            )
            return await complete_snapshot(validated, scope)

        request = await self._executor.run(complete)

        current, edges = await self._read_graph(scope)
        datasource_scope = self._narrow_write_scope(
            scope, datasource_id=request.datasource_id
        )
        drafts = self._importer(scope.user_id).project_snapshot(
            request, datasource_scope
        )
        nodes, projected_edges, datasources = {}, {}, {}

        def project_snapshot():
            batch = tuple(islice(drafts, self._writer.max_rows))
            for draft in batch:
                if isinstance(draft, NodeDraft):
                    if draft.transient.get("reference_only") and draft.ref in nodes:
                        continue
                    nodes[draft.ref] = draft
                    datasources[draft.ref] = (
                        None if draft.spec in SHARED_NODES else request.datasource_id
                    )
                else:
                    projected_edges[
                        (draft.spec, draft.source_ref, draft.target_ref)
                    ] = draft
            return len(batch)

        while True:
            count = await self._executor.run(project_snapshot)
            if count < self._writer.max_rows:
                break

        def reconcile_snapshot():
            removed_nodes, removed_edges = set(), set()
            if request.drop_semantic_first:
                domain_ids = {
                    str(domain.domain_id or domain.name)
                    for domain in request.semantic.domains
                }
                candidates = {
                    key
                    for key, row in current.items()
                    if row.spec not in (DATA_SOURCE, TABLE, COLUMN)
                    and (
                        row.properties.get("datasource_id") == request.datasource_id
                        or row.spec in SHARED_NODES
                        and (
                            row.properties.get("domain") in domain_ids
                            or row.spec is DOMAIN
                            and row.properties.get("domain_id") in domain_ids
                        )
                    )
                }
                # Shared domain/dictionary objects remain when connected to another datasource.
                foreign = {
                    key
                    for key, row in current.items()
                    if row.properties.get("datasource_id")
                    not in (None, request.datasource_id)
                }
                preserved = set(foreign)
                while True:
                    parents = {
                        source
                        for _, source, target in edges
                        if target in preserved
                        and source in current
                        and current[source].spec in SHARED_NODES
                    } - preserved
                    if not parents:
                        break
                    preserved |= parents
                removed_nodes = candidates - nodes.keys() - preserved
                removed_edges = {
                    identity
                    for identity, edge in edges.items()
                    if identity not in projected_edges
                    # FULL replaces semantic projections, not discovered physical structure.
                    and edge.spec not in (HAS_TABLE, TABLE_HAS_COLUMN_COLUMN)
                    and (
                        edge.properties.get("datasource_id") == request.datasource_id
                        or edge.source_key in removed_nodes
                        or edge.target_key in removed_nodes
                        or edge.spec.relationship
                        in {
                            "DIMENSION_BINDING",
                            "MAPS_TO_FIELD",
                            "AVAILABLE_IN",
                            "JOINS",
                        }
                        and edge.target_key in current
                        and current[edge.target_key].properties.get("datasource_id")
                        == request.datasource_id
                    )
                }
            return (
                Projection(nodes, projected_edges, datasources),
                current,
                edges,
                removed_nodes,
                removed_edges,
            )

        (
            incoming,
            current,
            edges,
            removed_nodes,
            removed_edges,
        ) = await self._executor.run(reconcile_snapshot)
        return request, self._compile(
            incoming,
            scope,
            current,
            edges,
            scopes={
                scope.datasource_id: scope,
                request.datasource_id: datasource_scope,
            },
            remove_nodes=removed_nodes,
            remove_edges=removed_edges,
        )

    async def import_snapshot(
        self,
        request,
        scope,
        *,
        before_commit=None,
        recovery_session: RecoveryStackSession | None = None,
    ):
        """Own a recovery run unless the caller supplies its enclosing session."""
        self._require_scope(scope)
        context = (
            nullcontext(recovery_session)
            if recovery_session is not None
            else self._recovery.run(scope, task_type="weave")
        )
        async with context as session:
            request, batches = await self._prepare_batches(request, scope)
            with bind_usage_context(
                source="语义资产导入",
                source_step=f"semantic_asset_import#{request.task_id}"
                if request.task_id
                else "semantic_asset_import",
                session_id="",
                chat_id="",
                probe=False,
            ):
                return await self._writer.consume(
                    batches,
                    partial(self._execute_plan, recovery_session=session),
                    scope,
                    before_commit=before_commit,
                )

    async def _compile(
        self,
        incoming,
        scope,
        current,
        edges,
        *,
        scopes: dict[str | None, ScopeBinding],
        remove_nodes=(),
        remove_edges=(),
        moved=(),
        all_new_edges=None,
    ):
        def prepare():
            deleted = set(remove_nodes)
            removed_edges = set(remove_edges) | {
                identity
                for identity in edges
                if identity[1] in deleted or identity[2] in deleted
            }
            new_edges = dict(incoming.edges)
            # Datasource reassignment rebuilds the node with its key and retains incident relationships.
            for identity in removed_edges:
                if (identity[1] in moved or identity[2] in moved) and identity in (
                    all_new_edges or {}
                ):
                    new_edges[identity] = all_new_edges[identity]
            drafts = []
            for key, draft in incoming.nodes.items():
                existing = current.get(key)
                if (
                    draft.transient.get("reference_only")
                    and existing is not None
                    and key not in deleted
                ):
                    continue  # Develop's anchors only supply facts when the node is absent.
                drafts.append(draft)
            deletions = [
                edges[key]
                for key in sorted(
                    removed_edges,
                    key=lambda key: (
                        key[0].relationship,
                        key[0].source.name,
                        key[0].target.name,
                        key[1],
                        key[2],
                    ),
                )
                if key in edges
            ]
            deletions.extend(row for key, row in current.items() if key in deleted)
            return drafts, deleted, removed_edges, new_edges, deletions

        drafts, deleted, removed_edges, new_edges, deletions = await self._executor.run(
            prepare
        )

        timestamp = datetime.now(timezone.utc).isoformat()

        def prepare_nodes(nodes):
            merged = []
            for draft in nodes:
                existing = current.get(draft.ref)
                inherited = (
                    {}
                    if existing is None
                    else {
                        prop.name: existing.properties[prop.name]
                        for prop in draft.spec.properties
                        if prop.owner is PropertyOwner.CALLER
                        and prop.name in existing.properties
                        and prop.name not in {"embedding", "embedding_hash"}
                    }
                )
                merged.append(
                    replace(
                        draft,
                        properties={
                            **inherited,
                            **draft.properties,
                            "updated_at": timestamp,
                        },
                    )
                )
            return EmbeddingEnricher.prepare(merged)

        def compile_plan(enriched, batch_edges, batch_deletes):
            operations = []
            for spec, group in grouped(batch_deletes, key=lambda row: row.spec):
                if isinstance(spec, NodeSpec):
                    operations.append(
                        (
                            None,
                            StoreOperation(
                                "delete_nodes",
                                {"spec": spec, "keys": tuple(row.key for row in group)},
                            ),
                        )
                    )
                else:
                    operations.append(
                        (
                            None,
                            StoreOperation(
                                "delete_edges",
                                {
                                    "spec": spec,
                                    "endpoints": tuple(
                                        (row.source_key, row.target_key)
                                        for row in group
                                    ),
                                },
                            ),
                        )
                    )
            for (spec, create, datasource), group in grouped(
                enriched,
                key=lambda draft: (
                    draft.spec,
                    draft.ref not in current or draft.ref in deleted,
                    incoming.datasources[draft.ref],
                ),
            ):
                batch = tuple(group)
                if create:
                    operations.append(
                        (
                            datasource,
                            StoreOperation(
                                "create_nodes",
                                {
                                    "spec": spec,
                                    "properties": tuple(
                                        node.output() for node in batch
                                    ),
                                },
                            ),
                        )
                    )
                else:
                    patches = []
                    for draft in batch:
                        changes = {
                            name: value
                            for name, value in draft.output().items()
                            if spec.property(name).editable
                            and current[draft.ref].properties.get(name) != value
                        }
                        if changes:
                            patches.append((draft.ref, changes))
                    if patches:
                        operations.append(
                            (
                                None,
                                StoreOperation(
                                    "patch_nodes",
                                    {"spec": spec, "patches": tuple(patches)},
                                ),
                            )
                        )
            for (spec, create), group in grouped(
                (
                    ((draft.spec, draft.source_ref, draft.target_ref), draft)
                    for draft in batch_edges
                ),
                key=lambda item: (
                    item[0][0],
                    item[0] not in edges or item[0] in removed_edges,
                ),
            ):
                batch = tuple(group)
                if create:
                    operations.append(
                        (
                            None,
                            StoreOperation(
                                "create_edges",
                                {
                                    "fields": (),
                                    "spec": spec,
                                    "edges": tuple(
                                        (
                                            draft.source_ref,
                                            draft.target_ref,
                                            draft.output(),
                                        )
                                        for _, draft in batch
                                    ),
                                },
                            ),
                        )
                    )
                else:
                    patches = []
                    for identity, draft in batch:
                        changes = {
                            name: value
                            for name, value in draft.output().items()
                            if spec.property(name).editable
                            and edges[identity].properties.get(name) != value
                        }
                        if changes:
                            patches.append(
                                (draft.source_ref, draft.target_ref, changes)
                            )
                    if patches:
                        operations.append(
                            (
                                None,
                                StoreOperation(
                                    "patch_edges",
                                    {"spec": spec, "patches": tuple(patches)},
                                ),
                            )
                        )
            for datasource, _ in operations:
                if datasource not in scopes:
                    scopes[datasource] = self._narrow_write_scope(
                        scope, datasource_id=datasource
                    )
            return MetadataWeavingPlan(
                TransactionPlan(tuple(operation for _, operation in operations)),
                tuple(scopes[datasource] for datasource, _ in operations),
            )

        models = self._models(scope.user_id).embedder() if drafts else nullcontext()
        async with models as embedder:
            for nodes, batch_edges, batch_deletes in self._writer.batches(
                drafts, new_edges.values(), deletions
            ):

                async def prepare_batch():
                    prepared, pending = await self._executor.run_sync(
                        prepare_nodes, nodes
                    )
                    vectors = (
                        await embedder.embed(tuple(text for _, text in pending))
                        if pending
                        else ()
                    )

                    def compile_batch():
                        enriched = EmbeddingEnricher.apply(prepared, pending, vectors)
                        return compile_plan(enriched, batch_edges, batch_deletes)

                    return await self._executor.run_sync(compile_batch)

                yield prepare_batch

    async def _execute_plan(
        self, plan, _scope, *, recovery_session: RecoveryStackSession | None = None
    ):
        return await self._writer.execute(
            plan.transaction,
            plan.operation_scopes,
            execute=recovery_session.execute if recovery_session is not None else None,
        )

    async def submit_task(self, db, request, principal, scope):
        """Keep the editor's task lifecycle while injecting CM graph execution."""
        from semantic_config.services import weave_task_service

        self._require_scope(scope)
        return await weave_task_service.submit(
            db,
            request,
            principal,
            executor=self._executor,
            graph_access=(self, scope),
            recovery=self._recovery,
        )

    async def import_excel(
        self,
        db,
        filename,
        content,
        principal,
        scope,
        *,
        datasource_id,
    ):
        """Save the workbook first, then submit a FULL task against committed facts."""
        from semantic_config.services import excel_import_service

        self._require_scope(scope)
        result = await excel_import_service.import_excel(
            db,
            filename,
            content,
            principal,
            datasource_id=datasource_id,
        )
        if result["success"]:
            await db.commit()
            result["weave_task_id"] = await self.trigger_full(
                datasource_id,
                scope,
                principal,
                task_name=f"Excel导入自动织入-{datasource_id}",
            )
        return result

    async def trigger_full(self, datasource_id, scope, principal, *, task_name):
        """Best-effort post-commit trigger; keep the editor/probe outcome independent."""
        from semantic_config.engine import get_session_factory
        from semantic_config.models.weave_task import WeaveTaskSubmit

        try:
            async with get_session_factory()() as db:
                result = await self.submit_task(
                    db,
                    WeaveTaskSubmit(
                        task_name=task_name,
                        datasource_id=datasource_id,
                        weave_mode="FULL",
                    ),
                    principal,
                    scope,
                )
                return result.task_id
        except Exception:
            log.exception(
                "Automatic FULL weaving could not be submitted datasource=%s",
                datasource_id,
            )
            return None

    async def physical_committed(self, datasource_id, scope, principal, *, status):
        """Capability 11 calls this after committing a successful physical discovery."""
        if status not in {"ok", "partial"}:
            return None
        return await self.trigger_full(
            datasource_id,
            scope,
            principal,
            task_name=f"物理层探查自动织入-{datasource_id}",
        )

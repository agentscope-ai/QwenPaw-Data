"""Prepare and execute one-way workspace Metadata pulls for the current user."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from itertools import groupby

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from ...auth import AccessMode, ScopeBinding
from ...heartbeat import lock_scope_sync
from ...schema import (
    COLUMN,
    DATA_SOURCE,
    DATASET,
    DATASET_FIELD,
    DIMENSION,
    DOMAIN,
    METRIC,
    METRIC_CALIBER,
    MG_SYNC_STATE,
    TABLE,
    VERIFIED_QUERY,
    EdgeSpec,
    KeySpec,
    NodeSpec,
    PropertyOwner,
)
from ...store import (
    EdgeDraft,
    EdgeRecord,
    GraphStore,
    NodeDraft,
    NodeRecord,
    StoreError,
)
from ...transaction import (
    StoreOperation,
    TransactionPlan,
    execute_transaction_plan,
)
from .algorithm import (
    MetadataSnapshot,
    MetadataSyncDiff,
    MetadataSyncState,
    compare_metadata,
    content_digest,
    logical_key,
    mirror_properties,
)

METADATA_NODES = (
    DATA_SOURCE,
    DOMAIN,
    DATASET,
    DATASET_FIELD,
    DIMENSION,
    METRIC,
    METRIC_CALIBER,
    VERIFIED_QUERY,
    TABLE,
    COLUMN,
)


class MetadataSyncConflict(ValueError):
    """Report a duplicate append or a conflict decision based on an old source."""


@dataclass(frozen=True, slots=True)
class MetadataSyncPlan:
    """Bind prepared datasource batches and the watermark to one personal graph."""

    tenant_id: str
    workspace_id: str
    user_id: str
    global_digest: str
    diff: MetadataSyncDiff
    operations: tuple[
        tuple[str | None, StoreOperation], ...
    ]  # Datasource and operation, all committed together.


class MetadataSyncService:
    """Pull authoritative MG into one user's graph without writing back."""

    def __init__(self, store: GraphStore, *, database_engine: Engine) -> None:
        self._store = store
        self._database_engine = database_engine

    @contextmanager
    def _serialize(self, scope: ScopeBinding) -> Iterator[None]:
        """Hold the shared SQL lock across reading, planning and graph commit."""
        self._require_personal_scope(scope, AccessMode.WRITE)
        key = "metadata-sync:" + json.dumps(
            [scope.tenant_id, scope.workspace_id, scope.user_id], separators=(",", ":")
        )
        try:
            with self._database_engine.connect() as connection:
                sqlite = connection.dialect.name == "sqlite"
                previous_timeout = None
                try:
                    with connection.begin():
                        if sqlite:
                            previous_timeout = connection.exec_driver_sql(
                                "PRAGMA busy_timeout"
                            ).scalar_one()
                            connection.exec_driver_sql("PRAGMA busy_timeout = 60000")
                        else:
                            connection.execute(text("SET LOCAL lock_timeout = '60s'"))
                        lock_scope_sync(connection, key)
                        yield
                finally:
                    if previous_timeout is not None:
                        connection.exec_driver_sql(
                            f"PRAGMA busy_timeout = {previous_timeout}"
                        )
        except OperationalError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03" or getattr(
                exc.orig, "sqlite_errorcode", None
            ) in {5, 6}:
                raise BlockingIOError("Metadata synchronization is busy") from exc
            raise

    def status(self, scope: ScopeBinding) -> dict[str, object]:
        self._require_personal_scope(scope, AccessMode.READ)
        try:
            state, _ = self._state(scope)
        except StoreError as exc:
            if exc.code not in {"user_space_uninitialized", "user_space_initializing"}:
                raise
            return {
                "space_status": exc.code.removeprefix("user_space_"),
                "global_digest": "",
                "last_synced_at": None,
                "needs_sync": True,
            }
        if state.last_synced_digest is None:
            return {
                "space_status": "ready",
                "global_digest": "",
                "last_synced_at": None,
                "needs_sync": True,
            }
        source = self._snapshot(self._store.access.metadata_source_scope(scope))
        digest = source.digest()
        return {
            "space_status": "ready",
            "global_digest": digest,
            "last_synced_at": state.last_synced_at,
            "needs_sync": state.last_synced_digest != digest,
        }

    def _prepare_plan(self, scope: ScopeBinding) -> MetadataSyncPlan:
        self._store.initialize_user_space(scope)
        source = self._snapshot(self._store.access.metadata_source_scope(scope))
        personal = self._snapshot(scope)
        state, record = self._state(scope)
        return self._build_plan(source, personal, state, record, scope)

    def _build_plan(
        self,
        source: MetadataSnapshot,
        personal: MetadataSnapshot,
        state: MetadataSyncState,
        state_record: NodeRecord | None,
        scope: ScopeBinding,
    ) -> MetadataSyncPlan:
        diff = compare_metadata(source, personal, state)
        digest = source.digest()
        source_nodes, source_edges = source.indexed_nodes(), source.indexed_edges()
        personal_nodes = personal.indexed_nodes()
        blocked = {logical_key(node) for node, _ in diff.conflicts}
        mirrors = (
            (state.mirrored_nodes & source_nodes.keys())
            | {logical_key(node) for node, _ in diff.nodes_to_merge}
            | {logical_key(node) for node in diff.nodes_to_adopt}
        )
        mirrored_edges = (state.mirrored_edges & source_edges.keys()) | {
            identity
            for identity in source_edges
            if identity[0] not in blocked and identity[2] not in blocked
        }
        next_state = MetadataSyncState(
            last_synced_digest=state.last_synced_digest if diff.conflicts else digest,
            last_synced_at=datetime.now(UTC).isoformat(),
            mirrored_nodes=frozenset(mirrors),
            mirrored_edges=frozenset(mirrored_edges),
            kept_user_digests={
                key: value
                for key, value in state.kept_user_digests.items()
                if key in personal_nodes and key not in mirrors
            },
        )
        return MetadataSyncPlan(
            scope.tenant_id,
            scope.workspace_id,
            scope.user_id,
            digest,
            diff,
            self._compile_plan(
                source,
                personal,
                diff,
                next_state,
                state_record,
                scope,
                self._read_incident_edges(diff, scope),
            ),
        )

    def _read_incident_edges(
        self, diff: MetadataSyncDiff, scope: ScopeBinding
    ) -> tuple[EdgeRecord, ...]:
        keys = {node.key for node in diff.nodes_to_delete} | {
            current.key
            for node, current in diff.nodes_to_merge
            if current is not None
            and node.properties.get("datasource_id")
            != current.properties.get("datasource_id")
        }
        if not keys:
            return ()
        return tuple(
            edge
            for spec in self._store.schema.edges
            if spec.source in METADATA_NODES or spec.target in METADATA_NODES
            for edge in self._list(spec, scope)
            if edge.source_key in keys or edge.target_key in keys
        )

    def _compile_plan(
        self,
        source: MetadataSnapshot,
        personal: MetadataSnapshot,
        diff: MetadataSyncDiff,
        state: MetadataSyncState,
        state_record: NodeRecord | None,
        scope: ScopeBinding,
        incident_edges: tuple[EdgeRecord, ...],
    ) -> tuple[tuple[str | None, StoreOperation], ...]:
        batches: list[tuple[str | None, StoreOperation]] = []
        deleted_keys = {node.key for node in diff.nodes_to_delete}
        # Datasource is Scope-owned. Recreate a rebound node with the same key, preserving its edges.
        rebound_nodes = {
            current.key: current
            for node, current in diff.nodes_to_merge
            if current is not None
            and node.properties.get("datasource_id")
            != current.properties.get("datasource_id")
        }
        deleted_edges = {
            (edge.spec, edge.source_key, edge.target_key): edge
            for edge in diff.edges_to_delete
        }
        obsolete_edges = set(deleted_edges)
        edges_to_restore = {}
        for edge in incident_edges:
            identity = (edge.spec, edge.source_key, edge.target_key)
            deleted_edges[identity] = edge
            if (
                identity not in obsolete_edges
                and edge.source_key not in deleted_keys
                and edge.target_key not in deleted_keys
            ):
                edges_to_restore[identity] = edge
        for (spec, datasource), rows in groupby(
            deleted_edges.values(),
            key=lambda edge: (edge.spec, edge.properties.get("datasource_id")),
        ):
            batches.append(
                (
                    datasource,
                    StoreOperation(
                        "delete_edges",
                        {
                            "spec": spec,
                            "endpoints": tuple(
                                (edge.source_key, edge.target_key) for edge in rows
                            ),
                        },
                    ),
                )
            )
        for (spec, datasource), rows in groupby(
            (*diff.nodes_to_delete, *rebound_nodes.values()),
            key=lambda node: (node.spec, node.properties.get("datasource_id")),
        ):
            batches.append(
                (
                    datasource,
                    StoreOperation(
                        "delete_nodes",
                        {
                            "spec": spec,
                            "keys": tuple(node.key for node in rows),
                        },
                    ),
                )
            )
        for (spec, datasource, creating), pairs in groupby(
            diff.nodes_to_merge,
            key=lambda pair: (
                pair[0].spec,
                pair[0].properties.get("datasource_id"),
                pair[1] is None or pair[1].key in rebound_nodes,
            ),
        ):
            pairs = tuple(pairs)
            if creating:
                operation = StoreOperation(
                    "create_nodes",
                    {
                        "spec": spec,
                        "properties": tuple(
                            mirror_properties(node) for node, _ in pairs
                        ),
                    },
                )
            else:
                patches = []
                for node, current in pairs:
                    desired = mirror_properties(node)
                    changes = {
                        prop.name: desired.get(prop.name)
                        for prop in spec.properties
                        if prop.owner is PropertyOwner.CALLER
                        and prop.editable
                        and current.properties.get(prop.name) != desired.get(prop.name)
                    }
                    if changes:
                        patches.append((current.key, changes))
                if not patches:
                    continue
                operation = StoreOperation(
                    "patch_nodes", {"spec": spec, "patches": tuple(patches)}
                )
            batches.append((datasource, operation))

        personal_nodes = personal.indexed_nodes()
        target_keys = {
            node.key: personal_nodes[logical_key(node)].key
            if logical_key(node) in personal_nodes
            else self._private_key(node, scope)
            for node in source.nodes
        }
        final_datasources = {
            node.key: node.properties.get("datasource_id") for node in personal.nodes
        }
        final_datasources.update(
            {
                target_keys[node.key]: node.properties.get("datasource_id")
                for node, _ in diff.nodes_to_merge
            }
        )
        new_edges = dict(edges_to_restore)
        for edge in diff.edges_to_create:
            new_edges[
                (edge.spec, target_keys[edge.source_key], target_keys[edge.target_key])
            ] = EdgeRecord(
                edge.spec,
                target_keys[edge.source_key],
                target_keys[edge.target_key],
                edge.properties,
            )
        edge_batches = []
        for edge in new_edges.values():
            datasource = final_datasources.get(edge.source_key)
            if final_datasources.get(edge.target_key) != datasource:
                datasource = (
                    None  # The personal edge spans different datasource bindings.
                )
            edge_batches.append((edge, datasource))
        for (spec, datasource), rows in groupby(
            edge_batches,
            key=lambda item: (item[0].spec, item[1]),
        ):
            batches.append(
                (
                    datasource,
                    StoreOperation(
                        "create_edges",
                        {
                            "fields": (),
                            "spec": spec,
                            "edges": tuple(
                                (
                                    edge.source_key,
                                    edge.target_key,
                                    {
                                        prop.name: edge.properties[prop.name]
                                        for prop in spec.properties
                                        if prop.owner is PropertyOwner.CALLER
                                        and prop.name in edge.properties
                                    },
                                )
                                for edge, _ in rows
                            ),
                        },
                    ),
                )
            )
        if state_record is None:
            operation = StoreOperation(
                "create_nodes",
                {
                    "spec": MG_SYNC_STATE,
                    "properties": ({"state": state.output()},),
                },
            )
        else:
            operation = StoreOperation(
                "patch_nodes",
                {
                    "spec": MG_SYNC_STATE,
                    "patches": ((state_record.key, {"state": state.output()}),),
                },
            )
        batches.append((None, operation))
        return tuple(batches)

    def _execute_plan(self, plan: MetadataSyncPlan, scope: ScopeBinding) -> None:
        if (plan.tenant_id, plan.workspace_id, plan.user_id) != (
            scope.tenant_id,
            scope.workspace_id,
            scope.user_id,
        ):
            raise PermissionError(
                "Metadata sync plan belongs to another user or workspace"
            )

        def execute(transaction_store: GraphStore) -> None:
            for datasource, operation in plan.operations:
                operation_scope = self._store.access.narrow_write_scope(
                    scope, datasource_id=datasource
                )
                method = getattr(transaction_store, operation.name)
                method(**operation.parameters, scope=operation_scope)

        self._store.atomic(execute)

    def sync(self, scope: ScopeBinding) -> dict[str, object]:
        with self._serialize(scope):
            plan = self._prepare_plan(scope)
            self._execute_plan(plan, scope)
        # Frontend counts describe the diff, not internal detach/recreate operations.
        return {
            "global_digest": plan.global_digest,
            "created": len(plan.diff.edges_to_create),
            "overwritten": len(plan.diff.nodes_to_merge),
            "deleted": len(plan.diff.nodes_to_delete) + len(plan.diff.edges_to_delete),
            "auto_adopted": len(plan.diff.nodes_to_adopt)
            + len(plan.diff.edges_to_adopt),
            "conflict_count": len(plan.diff.conflicts),
            "conflicts": plan.diff.conflict_details(),
        }

    def resolve(
        self,
        global_digest: str,
        resolutions: list[Mapping[str, str]],
        scope: ScopeBinding,
    ) -> dict[str, object]:
        with self._serialize(scope):
            source = self._snapshot(self._store.access.metadata_source_scope(scope))
            if source.digest() != global_digest:
                raise MetadataSyncConflict(
                    "Global MG changed since sync; please pull again"
                )
            personal = self._snapshot(scope)
            state, record = self._state(scope)
            nodes = {node.key: node for node in source.nodes}
            personal_nodes = personal.indexed_nodes()
            mirrors, kept = set(state.mirrored_nodes), dict(state.kept_user_digests)
            selected = {}
            resolved = 0
            for item in resolutions:
                node = nodes.get(item["key"])
                if node is None:
                    continue
                identity = logical_key(node)
                if item["decision"] not in {"global", "user"}:
                    raise ValueError("resolution decision must be global or user")
                resolved += 1
                if identity not in personal_nodes:
                    continue
                if item["decision"] == "global":
                    mirrors.add(identity)
                    kept.pop(identity, None)
                    selected[identity] = (node, personal_nodes[identity])
                else:
                    kept[identity] = content_digest(node)
            state = replace(
                state, mirrored_nodes=frozenset(mirrors), kept_user_digests=kept
            )
            remaining = compare_metadata(source, personal, state).conflicts
            personal_edges = personal.indexed_edges()
            restored_edges = {
                identity: edge
                for identity, edge in source.indexed_edges().items()
                if (identity[0] in selected or identity[2] in selected)
                and identity[0] in personal_nodes
                and identity[2] in personal_nodes
            }
            # A decision is not another pull: only global choices restore their incident edges.
            diff = MetadataSyncDiff(
                nodes_to_merge=tuple(selected.values()),
                nodes_to_delete=(),
                nodes_to_adopt=(),
                edges_to_create=tuple(
                    edge
                    for identity, edge in restored_edges.items()
                    if identity not in personal_edges
                ),
                edges_to_delete=(),
                edges_to_adopt=tuple(
                    personal_edges[identity]
                    for identity in restored_edges
                    if identity in personal_edges
                ),
                conflicts=remaining,
            )
            state = replace(
                state,
                mirrored_edges=state.mirrored_edges | frozenset(restored_edges),
                last_synced_digest=state.last_synced_digest
                if remaining
                else global_digest,
                last_synced_at=state.last_synced_at
                if remaining
                else datetime.now(UTC).isoformat(),
            )
            plan = MetadataSyncPlan(
                scope.tenant_id,
                scope.workspace_id,
                scope.user_id,
                global_digest,
                diff,
                self._compile_plan(
                    source,
                    personal,
                    diff,
                    state,
                    record,
                    scope,
                    self._read_incident_edges(diff, scope),
                ),
            )
            self._execute_plan(plan, scope)
            return {"resolved": resolved, "synced": not plan.diff.conflicts}

    def append_node(
        self,
        label: str,
        properties: Mapping[str, object],
        scope: ScopeBinding,
        *,
        key: str | None = None,
    ) -> str:
        """Append one schema-valid personal node; never overwrite an existing identity."""
        with self._serialize(scope):
            spec = self._store.schema.node(label)
            if spec not in METADATA_NODES:
                raise ValueError("label must be a Metadata node")
            node_scope = self._store.access.narrow_write_scope(
                scope,
                datasource_id=properties.get("datasource_id"),
            )
            reserved = {
                "key",
                "workspace_id",
                "owner_id",
                "zone",
                "datasource_id",
                "mg_origin",
                "mg_keep_user_digest",
            }
            values = {
                name: value
                for name, value in properties.items()
                if name not in reserved
            }
            persisted = {
                **values,
                "owner_id": scope.user_id,
                "workspace_id": scope.workspace_id,
                "datasource_id": node_scope.datasource_id,
            }
            node_key = spec.property("key").generate(persisted)
            if key is not None and key != node_key:
                raise ValueError(
                    "key must match the Schema identity derived from the node properties"
                )
            draft = NodeDraft.build(self._store.schema, spec, node_key, values)
            if (
                self._store.get_nodes(spec, (node_key,), scope=node_scope)[0]
                is not None
            ):
                raise MetadataSyncConflict("Node already exists in your userspace")
            result = execute_transaction_plan(
                self._store,
                TransactionPlan((StoreOperation.project_nodes((draft,)),)),
                node_scope,
            )
            return result.node_keys[draft.ref]

    def append_edge(
        self,
        source_key: str,
        relationship: str,
        target_key: str,
        properties: Mapping[str, object],
        scope: ScopeBinding,
    ) -> EdgeRecord:
        """Append an edge between two existing personal MG nodes, without upsert."""
        with self._serialize(scope):
            endpoints = {}
            for spec in METADATA_NODES:
                for node in self._store.get_nodes(
                    spec, (source_key, target_key), scope=scope
                ):
                    if node is not None:
                        endpoints[node.key] = node
            if source_key not in endpoints or target_key not in endpoints:
                raise ValueError("Both endpoints must exist in your userspace MG")
            source, target = endpoints[source_key], endpoints[target_key]
            spec = self._store.schema.edge(
                (source.spec.name, relationship, target.spec.name)
            )
            if (
                self._store.get_edges(spec, ((source_key, target_key),), scope=scope)[0]
                is not None
            ):
                raise MetadataSyncConflict("Edge already exists in your userspace")
            reserved = {
                "key",
                "workspace_id",
                "owner_id",
                "zone",
                "datasource_id",
                "mg_origin",
                "mg_keep_user_digest",
            }
            values = {
                name: value
                for name, value in properties.items()
                if name not in reserved
            }
            draft = EdgeDraft.build(
                self._store.schema, spec, source_key, target_key, values
            )
            datasource = source.properties.get("datasource_id")
            if target.properties.get("datasource_id") != datasource:
                datasource = None
            edge_scope = self._store.access.narrow_write_scope(
                scope,
                datasource_id=datasource,
            )
            result = execute_transaction_plan(
                self._store,
                TransactionPlan(
                    (
                        StoreOperation(
                            "create_edges",
                            {
                                "fields": (),
                                "spec": spec,
                                "edges": ((source_key, target_key, draft.output()),),
                            },
                        ),
                    )
                ),
                edge_scope,
            )
            return result.operation_results[0][0]

    def _snapshot(self, scope: ScopeBinding) -> MetadataSnapshot:
        nodes = tuple(
            node for spec in METADATA_NODES for node in self._list(spec, scope)
        )
        keys = {node.key for node in nodes}
        edges = tuple(
            edge
            for spec in self._store.schema.edges
            if spec.source in METADATA_NODES and spec.target in METADATA_NODES
            for edge in self._list(spec, scope)
            if edge.source_key in keys and edge.target_key in keys
        )
        return MetadataSnapshot(nodes, edges)

    def _list(self, spec: NodeSpec | EdgeSpec, scope: ScopeBinding):
        listing = (
            self._store.list_nodes
            if isinstance(spec, NodeSpec)
            else self._store.list_edges
        )
        offset = 0
        while True:
            page = listing(spec, scope=scope, limit=500, offset=offset)
            yield from page
            if len(page) < 500:
                return
            offset += len(page)

    def _state(
        self, scope: ScopeBinding
    ) -> tuple[MetadataSyncState, NodeRecord | None]:
        key = MG_SYNC_STATE.property("key").generate({"owner_id": scope.user_id})
        record = self._store.get_nodes(MG_SYNC_STATE, (key,), scope=scope)[0]
        return (
            MetadataSyncState()
            if record is None
            else MetadataSyncState.load(record.properties["state"])
        ), record

    def _require_personal_scope(self, scope: ScopeBinding, mode: AccessMode) -> None:
        self._store.access.require(scope, mode)
        if scope.owner_ids != (scope.user_id,) or scope.datasource_id is not None:
            raise PermissionError(
                "Metadata userspace operations require the current user's whole graph"
            )
        if scope.zone is not None:
            raise PermissionError(
                "Metadata userspace sync requires a cross-zone scope for incident edges"
            )
        if mode is AccessMode.WRITE and scope.write_owner_id != scope.user_id:
            raise PermissionError(
                "Metadata sync cannot write authoritative MG or another user"
            )

    @staticmethod
    def _private_key(node: NodeRecord, scope: ScopeBinding) -> str:
        key_spec = node.spec.property("key")
        assert isinstance(key_spec, KeySpec)
        return key_spec.generate({**node.properties, "owner_id": scope.user_id})

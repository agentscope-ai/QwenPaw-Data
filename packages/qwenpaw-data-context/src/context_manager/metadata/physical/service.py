"""Map physical source facts without replacing manually maintained facts."""

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime
from typing import ClassVar

from ...auth import GLOBAL_OWNER_ID, AccessMode, ScopeBinding
from ...schema import COLUMN, DATA_SOURCE, HAS_TABLE, TABLE, TABLE_HAS_COLUMN_COLUMN
from ...settings import get_settings
from ...sql import DatasourceRepository, RdbConnector
from ...store import (
    GraphStore,
    NodeDraft,
)
from ...transaction import (
    StoreOperation,
    TransactionPlan,
    TransactionResult,
    execute_transaction_plan,
)
from .source import PhysicalMappingSource


class PhysicalMappingService:
    """Map source tables, columns and samples into authoritative MG Store plans.

    Autoexplore retains filtering, fingerprints and manual-column selection. This
    service supplies its source reader and graph writer; no model calls or FULL
    semantic rebuild are part of a physical mapping batch.
    """

    _ROWS: ClassVar[dict[str, str]] = {
        "create_nodes": "properties",
        "patch_nodes": "patches",
        "create_edges": "edges",
    }

    def __init__(
        self,
        store: GraphStore,
        connector: RdbConnector,
        *,
        run_sync: Callable[..., Awaitable],
    ):
        self._store = store
        self._datasources = DatasourceRepository()
        self._connector = connector
        self._run_sync = run_sync
        self._metadata_cache = {}
        self._max_rows = get_settings().graph.write_batch_max_rows

    def _datasource_scope(
        self, scope: ScopeBinding, datasource_id: str
    ) -> ScopeBinding:
        self._store.access.require(scope, AccessMode.WRITE)
        if scope.write_owner_id != GLOBAL_OWNER_ID or scope.zone != "metadata":
            raise PermissionError(
                "physical discovery requires authoritative Metadata write scope"
            )
        return self._store.access.narrow_write_scope(scope, datasource_id=datasource_id)

    def resolve_source(self, datasource_id, scope, *, force_refresh=False):
        scope = self._datasource_scope(scope, datasource_id)
        source = self._datasources.get(
            datasource_id,
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
        )
        target = source.to_rdb_target(
            user_id=scope.user_id, runtime_project=source.config.get("project")
        )
        digest = hashlib.sha256(
            json.dumps(dict(source.config), sort_keys=True).encode()
        ).hexdigest()
        cache_key = (scope.user_id, datasource_id, source.datasource_type, digest)
        reader = PhysicalMappingSource(
            self._connector,
            target,
            self._metadata_cache,
            cache_key,
            force_refresh=force_refresh,
        )
        return reader, {
            "_datasource_type": source.datasource_type,
            "project": source.config.get("project", ""),
        }

    async def explore(
        self,
        db,
        datasource_id,
        scope,
        *,
        force_refresh=False,
        run_id=None,
        execution=None,
    ):
        from semantic_config.services.autoexplore.pipeline import run_explore

        self._datasource_scope(scope, datasource_id)
        return await run_explore(
            db,
            datasource_id=datasource_id,
            tenant_id=scope.tenant_id,
            workspace_id=scope.workspace_id,
            user_id=scope.user_id,
            graph_access=(self, scope),
            run_sync=self._run_sync,
            force_refresh=force_refresh,
            run_id=run_id,
            execution=execution,
        )

    def prepare_plans(
        self, sheet: str, rows: Sequence[Mapping[str, object]], scope: ScopeBinding
    ) -> Iterator[TransactionPlan]:
        """Compile bounded transactions, preserving properties and node-before-edge order."""
        scope = self._datasource_scope(scope, scope.datasource_id)
        if sheet not in {"table", "table_column"}:
            raise ValueError(
                "physical discovery only writes table and table_column rows"
            )
        if any(row["datasource_id"] != scope.datasource_id for row in rows):
            raise ValueError("physical discovery rows belong to a different datasource")
        if not rows:
            return
        operations = []
        timestamp = datetime.now(UTC)

        def key(spec, values):
            return spec.property("key").generate(
                {
                    "owner_id": scope.write_owner_id,
                    "datasource_id": scope.datasource_id,
                    **values,
                }
            )

        source_key = key(DATA_SOURCE, {})
        if self._store.get_nodes(DATA_SOURCE, (source_key,), scope=scope)[0] is None:
            source = self._datasources.get(
                scope.datasource_id,
                tenant_id=scope.tenant_id,
                workspace_id=scope.workspace_id,
            )
            operations.append(
                StoreOperation(
                    "create_nodes",
                    {
                        "spec": DATA_SOURCE,
                        "properties": (
                            {
                                "datasource_name": source.datasource_name,
                                "datasource_type": source.datasource_type,
                            },
                        ),
                    },
                )
            )

        table_values = {}
        column_values = {}
        for row in rows:
            name = row["table_name"]
            table_values.setdefault(name, {"name": name})
            if sheet == "table":
                for field in ("comment", "partition_keys"):
                    if field in row:
                        table_values[name][field] = row[field]
            else:
                values = {"table": name, "name": row["column_name"]}
                for field, prop in (
                    ("data_type", "data_type"),
                    ("is_primary", "is_primary"),
                    ("is_nullable", "is_nullable"),
                    ("column_comment", "comment"),
                    ("sample_values", "sample_values"),
                ):
                    if field in row:
                        values[prop] = row[field]
                column_values[(name, row["column_name"])] = values

        for spec, values in (
            (TABLE, table_values.values()),
            (COLUMN, column_values.values()),
        ):
            drafts = tuple(
                NodeDraft.build(self._store.schema, spec, key(spec, value), value)
                for value in values
            )
            if not drafts:
                continue
            current = self._store.get_nodes(
                spec, tuple(node.ref for node in drafts), scope=scope
            )
            creates, patches = [], []
            for node, existing in zip(drafts, current, strict=True):
                if existing is None:
                    creates.append({**node.output(), "updated_at": timestamp})
                else:
                    changes = {
                        name: value
                        for name, value in node.output().items()
                        if spec.property(name).editable
                        and existing.properties.get(name) != value
                    }
                    if changes:
                        patches.append((node.ref, {**changes, "updated_at": timestamp}))
            if creates:
                operations.append(
                    StoreOperation(
                        "create_nodes", {"spec": spec, "properties": tuple(creates)}
                    )
                )
            if patches:
                operations.append(
                    StoreOperation(
                        "patch_nodes", {"spec": spec, "patches": tuple(patches)}
                    )
                )

        for spec, endpoints in (
            (
                HAS_TABLE,
                tuple(
                    (source_key, key(TABLE, value)) for value in table_values.values()
                ),
            ),
            (
                TABLE_HAS_COLUMN_COLUMN,
                tuple(
                    (key(TABLE, {"name": value["table"]}), key(COLUMN, value))
                    for value in column_values.values()
                ),
            ),
        ):
            if not endpoints:
                continue
            current = self._store.get_edges(spec, endpoints, scope=scope)
            creates = tuple(
                (source, target, {})
                for (source, target), existing in zip(endpoints, current, strict=True)
                if existing is None
            )
            if creates:
                operations.append(
                    StoreOperation(
                        "create_edges", {"fields": (), "spec": spec, "edges": creates}
                    )
                )
        batch, size = [], 0
        for operation in operations:
            parameter = self._ROWS[operation.name]
            values = operation.parameters[parameter]
            offset = 0
            while offset < len(values):
                selected = values[offset : offset + self._max_rows - size]
                batch.append(
                    StoreOperation(
                        operation.name,
                        {**operation.parameters, parameter: selected},
                    )
                )
                size += len(selected)
                offset += len(selected)
                if size == self._max_rows:
                    yield TransactionPlan(tuple(batch))
                    batch, size = [], 0
        if batch:
            yield TransactionPlan(tuple(batch))

    def execute_plan(
        self, plan: TransactionPlan, scope: ScopeBinding
    ) -> TransactionResult:
        """Commit one prepared physical batch with the existing Store executor."""
        self._datasource_scope(scope, scope.datasource_id)
        size = sum(
            len(operation.parameters[self._ROWS[operation.name]])
            for operation in plan.operations
        )
        if size > self._max_rows:
            raise ValueError("Physical mapping plan exceeds one write batch")
        return execute_transaction_plan(self._store, plan, scope)

    def write_rows(self, sheet, rows, datasource_id, scope):
        scope = self._datasource_scope(scope, datasource_id)
        for plan in self.prepare_plans(sheet, rows, scope):
            self.execute_plan(plan, scope)
        return len(rows)

"""Execute one ordinary Plan and its Undo on the same transaction-bound Store."""

from __future__ import annotations

from collections.abc import Mapping

from ...auth import ScopeBinding
from ...schema import (
    CrudOperation,
    KeyStrategy,
    NodeSpec,
    PropertyOwner,
)
from ...settings import get_settings
from ...store import GraphStore
from ..plan import TransactionPlan
from ..transaction import Transaction
from .codec import encode
from .stack import RecoveryStack

_MAX_BATCH_BYTES = 32 * 1024 * 1024
_INPUTS = {
    "create_nodes": "properties",
    "create_edges": "edges",
    "patch_nodes": "patches",
    "patch_edges": "patches",
    "delete_nodes": "keys",
    "delete_edges": "endpoints",
}


class StackedTransaction(Transaction):
    """One-use Transaction with a fixed stack sequence and attempt-local Undo.

    The inherited executor owns reference resolution, order, validation and
    result statistics. Only the per-operation call and in-transaction boundary
    add recovery behavior; ordinary Store primitives remain unchanged.
    """

    def __init__(
        self, stack: RecoveryStack, scope: ScopeBinding, sequence: int
    ) -> None:
        super().__init__(stack.store)
        self.stack = stack
        self.scope = scope
        self.sequence = sequence
        self._entries: list[dict[str, object]] = []
        self._input_bytes = 0

    def _check_plan(self, plan: TransactionPlan) -> None:
        rows = 0
        self._input_bytes = 0
        for operation in plan.operations:
            if operation.name not in _INPUTS:
                raise ValueError(
                    "stacked Plans support only the six finite CRUD writes"
                )
            spec = operation.parameters["spec"]
            if (isinstance(spec, NodeSpec) and spec.internal) or (
                not isinstance(spec, NodeSpec)
                and (spec.source.internal or spec.target.internal)
            ):
                raise PermissionError(
                    "business Plans cannot modify internal recovery objects"
                )
            if operation.name.startswith("create_"):
                self._store.schema.validate_operation(spec, CrudOperation.DELETE)
            elif operation.name.startswith("delete_"):
                self._store.schema.validate_operation(spec, CrudOperation.CREATE)
            items = operation.parameters[_INPUTS[operation.name]]
            rows += len(items)
            self._input_bytes += len(encode(items).encode("utf-8"))
        if rows > get_settings().graph.write_batch_max_rows:
            raise ValueError("stacked Plan exceeds the configured row budget")
        if self._input_bytes > _MAX_BATCH_BYTES:
            raise ValueError("stacked Plan exceeds the byte budget")

    def _execute_in_transaction(self, tx, plan, scopes, validate):
        self._entries = []
        try:
            self._check_plan(plan)
            self.stack.begin_batch(tx, self.sequence)
            result = super()._execute_in_transaction(tx, plan, scopes, validate)
            payload = encode(self._entries)
            if len(payload.encode("utf-8")) + self._input_bytes > _MAX_BATCH_BYTES:
                raise ValueError("business input and Undo exceed the batch byte budget")
            self.stack.append(tx, self.sequence, payload)
            return result
        finally:
            self._entries = []

    def _check_scope(self, scope: ScopeBinding, spec) -> None:
        """Keep each operation within the run; Store checks capability authenticity."""
        if (scope.tenant_id, scope.workspace_id, scope.user_id) != (
            self.scope.tenant_id,
            self.scope.workspace_id,
            self.scope.user_id,
        ):
            raise PermissionError(
                "batch scope is outside the recovery requester or target"
            )
        if self.scope.owner_ids and scope.write_owner_id not in self.scope.owner_ids:
            raise PermissionError("batch owner is outside the recovery scope")
        if (
            self.scope.datasource_id is not None
            and scope.datasource_id != self.scope.datasource_id
        ):
            raise PermissionError("batch datasource is outside the recovery scope")
        zones = (
            {spec.zone.value}
            if isinstance(spec, NodeSpec)
            else {spec.source.zone.value, spec.target.zone.value}
        )
        if self.scope.zone is not None and zones != {self.scope.zone}:
            raise PermissionError("batch type is outside the recovery zone")

    def _execute_operation(
        self,
        tx: GraphStore,
        name: str,
        parameters: Mapping[str, object],
        scope: ScopeBinding,
    ):
        spec = parameters["spec"]
        self._check_scope(scope, spec)
        node = name.endswith("_nodes")
        items = parameters[_INPUTS[name]]
        description = {
            "owner_id": scope.write_owner_id,
            "datasource_id": scope.datasource_id,
        }
        if name.startswith("create_"):
            if not node:
                # Undo addresses an edge by endpoints. A duplicate would make
                # rollback delete a pre-existing edge with the same identity.
                existing = tx.get_edges(
                    spec,
                    tuple((source, target) for source, target, _ in items),
                    fields=(),
                    scope=self.stack.read_scope,
                )
                if any(edge is not None for edge in existing):
                    raise ValueError(
                        "stacked create_edges requires an unused relationship identity"
                    )
            result = super()._execute_operation(tx, name, parameters, scope)
            for value in result:
                identity = value if node else (value.source_key, value.target_key)
                self._entries.append(
                    {
                        "name": "delete_nodes" if node else "delete_edges",
                        "spec": spec.name if node else spec.triple,
                        "identity": identity,
                        "scope": description,
                    }
                )
            return result

        patch = name.startswith("patch_")
        identities = (
            tuple((item[0] if node else tuple(item[:2])) for item in items)
            if patch
            else tuple(items)
        )
        fields = (
            tuple(dict.fromkeys(field for item in items for field in item[-1]))
            if patch
            else None
        )
        old = (
            tx.get_nodes(spec, identities, fields=fields, scope=scope)
            if node
            else tx.get_edges(spec, identities, fields=fields, scope=scope)
        )
        for identity, record, item in zip(identities, old, items, strict=True):
            if record is None:
                continue
            if patch:
                properties = {field: record.properties.get(field) for field in item[-1]}
                entry_scope = description
            else:
                properties, entry_scope = self._restore_properties(spec, record, scope)
            self._entries.append(
                {
                    "name": name
                    if patch
                    else ("create_nodes" if node else "create_edges"),
                    "spec": spec.name if node else spec.triple,
                    "identity": identity,
                    "scope": entry_scope,
                    "properties": properties,
                }
            )
        return super()._execute_operation(tx, name, parameters, scope)

    def _restore_properties(
        self, spec, record, scope: ScopeBinding
    ) -> tuple[dict, dict]:
        """Validate an exact, ordinary-create restoration before deleting an object."""
        properties = {
            name: value
            for name, value in record.properties.items()
            if spec.property(name).owner is PropertyOwner.CALLER
        }
        self._store.schema.validate_values(spec, record.properties, complete=True)
        if isinstance(spec, NodeSpec):
            key = spec.property("key")
            if (
                key.strategy is not KeyStrategy.DETERMINISTIC
                or key.generate(record.properties) != record.key
            ):
                raise ValueError(
                    "deleted node cannot be restored with its original identity"
                )
        description = {
            "owner_id": scope.write_owner_id,
            "datasource_id": scope.datasource_id,
        }
        description.update(
            {
                name: record.properties[name]
                for name in ("owner_id", "datasource_id")
                if name in record.properties
            }
        )
        return properties, description

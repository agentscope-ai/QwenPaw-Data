"""Execute prepared graph plans within one database transaction."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import cast

from ..auth import ScopeBinding
from ..store import GraphStore, StoreError
from .plan import TransactionPlan, TransactionResult


class Transaction:
    """Execute one prepared Plan atomically, with an authorized scope per operation.

    A single scope applies to the whole Plan. Ordered scopes allow bounded
    multi-owner or datasource batches without changing their commit boundary.
    Subclasses can extend execution on the same transaction-bound Store.
    """

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    def execute(
        self,
        plan: TransactionPlan,
        scope: ScopeBinding | Sequence[ScopeBinding],
        *,
        validate: Callable[[TransactionResult], None] | None = None,
    ) -> TransactionResult:
        if type(plan) is not TransactionPlan:
            raise TypeError("plan must be a TransactionPlan")
        if type(scope) is ScopeBinding:
            scopes = (scope,) * len(plan.operations)
        else:
            scopes = tuple(scope)
            if len(scopes) != len(plan.operations):
                raise ValueError("each Plan operation requires one scope")
        return self._store.atomic(
            lambda tx: self._execute_in_transaction(tx, plan, scopes, validate)
        )

    def _execute_operation(
        self,
        transaction_store: GraphStore,
        name: str,
        parameters: Mapping[str, object],
        scope: ScopeBinding,
    ) -> object:
        return getattr(transaction_store, name)(**parameters, scope=scope)

    def _execute_in_transaction(
        self,
        transaction_store: GraphStore,
        plan: TransactionPlan,
        scopes: tuple[ScopeBinding, ...],
        validate: Callable[[TransactionResult], None] | None,
    ) -> TransactionResult:
        results: list[object] = []
        node_keys: dict[str, str] = {}
        stats = {
            kind: {
                action: 0
                for action in (
                    "created",
                    "read",
                    "updates_submitted",
                    "deletes_submitted",
                )
            }
            for kind in ("nodes", "edges")
        }
        for operation, scope in zip(plan.operations, scopes):
            parameters = dict(operation.parameters)
            batch_parameters: set[str] = set()
            for parameter, ref in operation.node_key_refs.items():
                key = node_keys[ref]
                if isinstance(parameter, str):
                    parameters[parameter] = key
                else:
                    argument = parameter[0]
                    if argument not in batch_parameters:
                        # Copy each referenced batch once; the frozen Plan remains reusable.
                        parameters[argument] = [
                            list(item) if isinstance(item, tuple) else item
                            for item in parameters[argument]
                        ]
                        batch_parameters.add(argument)
                    items = parameters[argument]
                    if len(parameter) == 2:
                        items[parameter[1]] = key
                    else:
                        items[parameter[1]][parameter[2]] = key
            for argument in batch_parameters:
                parameters[argument] = tuple(
                    tuple(item) if isinstance(item, list) else item
                    for item in parameters[argument]
                )
            result = self._execute_operation(
                transaction_store, operation.name, parameters, scope
            )
            if operation.node_refs:
                for node_ref, node in zip(operation.node_refs, result, strict=True):
                    if node_ref is None:
                        continue
                    if operation.name == "create_nodes":
                        node_keys[node_ref] = node
                    elif node is None:
                        raise StoreError(
                            "Store operation did not find the required node",
                            operation=operation.name,
                            code="transaction_result_invalid",
                        )
                    else:
                        node_keys[node_ref] = node.key
            results.append(result)
            match operation.name:
                case "create_nodes" | "create_edges":
                    kind = "nodes" if operation.name == "create_nodes" else "edges"
                    stats[kind]["created"] += len(cast(Sequence[object], result))
                case "get_nodes" | "get_edges":
                    kind = "nodes" if operation.name == "get_nodes" else "edges"
                    stats[kind]["read"] += sum(value is not None for value in result)
                case "patch_nodes" | "patch_edges":
                    kind = "nodes" if operation.name == "patch_nodes" else "edges"
                    stats[kind]["updates_submitted"] += len(parameters["patches"])
                case "delete_nodes" | "delete_edges":
                    kind = "nodes" if operation.name == "delete_nodes" else "edges"
                    stats[kind]["deletes_submitted"] += len(
                        parameters["keys" if kind == "nodes" else "endpoints"]
                    )
                case (
                    "list_nodes"
                    | "search_fulltext"
                    | "search_vector"
                    | "expand_neighbors"
                ):
                    stats["nodes"]["read"] += len(cast(Sequence[object], result))
                case "list_edges":
                    stats["edges"]["read"] += len(cast(Sequence[object], result))
        result = TransactionResult(tuple(results), node_keys, stats)
        if validate is not None:
            validate(result)
        return result


def execute_transaction_plan(
    store: GraphStore,
    plan: TransactionPlan,
    scope: ScopeBinding,
    *,
    validate: Callable[[TransactionResult], None] | None = None,
) -> TransactionResult:
    """Execute an existing single-scope Plan using the shared transaction executor."""
    return Transaction(store).execute(plan, scope, validate=validate)

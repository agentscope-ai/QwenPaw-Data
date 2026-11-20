"""Bound authoritative MG preparation and execute one prepared transaction."""

from collections.abc import Callable, Iterator, Sequence
from contextlib import aclosing
from itertools import chain, islice

from ..auth import ScopeBinding
from ..executor import BoundedExecutor
from ..settings import get_settings
from ..store import GraphStore
from ..transaction import (
    Transaction,
    TransactionPlan,
    TransactionResult,
)


class MetadataWriter:
    """Batch resolved MG mutations by rows, preserving operation and result order.

    The supplied execution entry owns transaction and recovery semantics.
    """

    def __init__(self, store: GraphStore, *, executor: BoundedExecutor):
        self._executor = executor
        self._transaction = Transaction(store)
        # Keep preparation and commit on one configuration snapshot.
        self.max_rows = get_settings().graph.write_batch_max_rows

    def batches(self, nodes, edges, deletions=()) -> Iterator[tuple]:
        """Select at most one transaction's facts before embedding or compiling.

        FULL deletes retain their edge-before-node order. All node writes precede
        edge writes, including when an endpoint was committed in an earlier batch.
        """
        rows = chain(
            (("delete", row) for row in deletions),
            (("node", row) for row in nodes),
            (("edge", row) for row in edges),
        )
        while batch := tuple(islice(rows, self.max_rows)):
            selected = {"delete": [], "node": [], "edge": []}
            for kind, row in batch:
                selected[kind].append(row)
            yield (
                tuple(selected["node"]),
                tuple(selected["edge"]),
                tuple(selected["delete"]),
            )

    async def execute(
        self,
        plan: TransactionPlan,
        scope: ScopeBinding | Sequence[ScopeBinding],
        *,
        execute: Callable[
            [TransactionPlan, ScopeBinding | Sequence[ScopeBinding]], TransactionResult
        ]
        | None = None,
    ) -> TransactionResult:
        return await self._executor.run_sync(
            execute if execute is not None else self._transaction.execute,
            plan,
            scope,
        )

    async def consume(self, batches, execute, scope, *, before_commit=None):
        """Prepare and commit each batch before admitting the next one."""

        async def work(prepare):
            plan = await prepare()
            if before_commit is not None:
                await before_commit(plan)
            return await execute(plan, scope)

        results, node_keys = [], {}
        stats = {
            kind: dict.fromkeys(
                ("created", "read", "updates_submitted", "deletes_submitted"), 0
            )
            for kind in ("nodes", "edges")
        }
        async with aclosing(batches):
            async for prepare in batches:
                result = await self._executor.run(work, prepare)
                results.extend(result.operation_results)
                node_keys.update(result.node_keys)
                for kind, counts in result.stats.items():
                    for action, count in counts.items():
                        stats[kind][action] += count
        return await self._executor.run(
            lambda: TransactionResult(tuple(results), node_keys, stats),
        )

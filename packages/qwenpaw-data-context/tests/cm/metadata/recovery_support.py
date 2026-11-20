"""Inspect business facts and durable receipts in weaving recovery scenarios."""

from context_manager.schema import (
    RECOVERY_STACK_HEAD,
    UNDO_BATCH,
    Zone,
)
from context_manager.schema.relational import recovery_runs
from sqlalchemy import select


def rows(w, table=recovery_runs):
    with w.engine.connect() as connection:
        return tuple(connection.execute(select(table)).mappings())


def facts(w):
    return (
        {
            (spec.name, node.key): dict(node.properties)
            for spec in w.store.schema.nodes
            if spec.zone is Zone.METADATA
            for node in w.store.list_nodes(spec, scope=w.scope, limit=1000)
        },
        {
            (spec.relationship, edge.source_key, edge.target_key): dict(edge.properties)
            for spec in w.store.schema.edges
            if spec.source.zone is spec.target.zone is Zone.METADATA
            for edge in w.store.list_edges(spec, scope=w.scope, limit=1000)
        },
    )


def assert_closed(w, *statuses):
    receipts = rows(w)
    assert sorted(row["result_status"] for row in receipts) == sorted(statuses)
    assert all(row["recovery_state"] == "CLOSED" for row in receipts)
    assert not w.store.list_nodes(RECOVERY_STACK_HEAD, scope=w.control)
    assert not w.store.list_nodes(UNDO_BATCH, scope=w.control)

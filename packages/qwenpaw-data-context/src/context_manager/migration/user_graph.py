"""Migrate canonical keys and relationship scopes on existing Neo4j UG data."""

from __future__ import annotations

from collections.abc import Mapping

from ..schema import GRAPH_SCHEMA, USER, USER_MEMORY
from ..store.neo4j import Neo4jRuntime
from ..store.runtime import GraphRuntime
from ..utils.validation import require_trimmed_text

_NODES = (USER, USER_MEMORY)
_EDGES = ((USER, "HAS"), (USER_MEMORY, "REFERS_TO"))
_REFERENCE_TARGETS = {
    edge.target.name: edge.target
    for edge in GRAPH_SCHEMA.edges
    if edge.source is USER_MEMORY and edge.relationship == "REFERS_TO"
}
_OPERATION = "migration.user_graph"


class UserGraphMigrationError(ValueError):
    """Carry preflight issues when a requested migration cannot be applied."""

    def __init__(self, report: dict[str, object]) -> None:
        super().__init__("User Graph migration has preflight issues")
        self.report = report


def migrate_user_graph(
    runtime: Neo4jRuntime,
    *,
    dry_run: bool = True,
) -> dict[str, object]:
    """Inspect one explicit database/workspace, or backfill it atomically.

    Call during a maintenance window with UG writers stopped. The caller owns
    the runtime and driver; execution re-reads the graph inside its transaction.
    """
    if runtime.target.database is None:
        raise ValueError("User Graph migration requires an explicit database")
    if dry_run:
        return _migrate(runtime, dry_run=True)
    return runtime.write_work(
        lambda transaction: _migrate(transaction, dry_run=False),
        operation=_OPERATION,
    )


def _migrate(runtime: GraphRuntime, *, dry_run: bool) -> dict[str, object]:
    workspace = runtime.target.workspace_id
    report = {
        "tenant_id": runtime.target.tenant_id,
        "workspace_id": workspace,
        "database": runtime.target.database,
        "dry_run": dry_run,
        "ready": True,
        "applied": False,
        "nodes": {},
        "edges": {},
        "issues": [],
    }
    writes = []
    for spec in _NODES:
        rows = runtime.read(
            f"MATCH (n:{spec.name}) "
            "WHERE n.workspace_id = $workspace_id OR n.workspace_id IS NULL "
            "RETURN elementId(n) AS element_id, properties(n) AS properties",
            {"workspace_id": workspace},
            operation=f"{_OPERATION}.scan.{spec.name}",
        )
        patches, keys = [], {}
        for row in rows:
            values = row["properties"]
            try:
                _node_scope(values, workspace, spec.zone.value)
                if spec is USER and values["owner_id"] != values.get("user_id"):
                    raise ValueError("User owner_id differs from user_id")
                key_spec = spec.property("key")
                for part in key_spec.parts:
                    require_trimmed_text(values.get(part), part)
                key = key_spec.generate(values)
                if key in keys:
                    raise ValueError(f"duplicate key with node {keys[key]}")
                keys[key] = row["element_id"]
                if spec is USER_MEMORY and values.get("key") == f"mem:{values['id']}":
                    missing = {"key": key}
                else:
                    missing = _missing(values, {"key": key})
                if missing:
                    patches.append(
                        {
                            "element_id": row["element_id"],
                            "previous_key": values.get("key"),
                            "properties": missing,
                        }
                    )
            except (TypeError, ValueError) as exc:
                report["issues"].append(
                    {
                        "kind": spec.name,
                        "element_id": row["element_id"],
                        "message": str(exc),
                    }
                )
        report["nodes"][spec.name] = {
            "scanned": len(rows),
            "pending": len(patches),
            "updated": 0,
        }
        writes.append(
            (
                "nodes",
                spec.name,
                patches,
                f"MATCH (n:{spec.name}) WHERE elementId(n) = row.element_id "
                "AND n.workspace_id = $workspace_id "
                "AND ((row.previous_key IS NULL AND n.key IS NULL) OR n.key = row.previous_key) "
                "SET n += row.properties RETURN count(n) AS updated",
            )
        )

    for source_spec, relationship in _EDGES:
        rows = runtime.read(
            f"MATCH (s:{source_spec.name})-[r:{relationship}]->(t) "
            "WHERE s.workspace_id = $workspace_id "
            "RETURN elementId(r) AS element_id, properties(r) AS properties, "
            "properties(s) AS source, labels(t) AS target_labels, properties(t) AS target",
            {"workspace_id": workspace},
            operation=f"{_OPERATION}.scan.{relationship}",
        )
        patches = []
        for row in rows:
            source, target = row["source"], row["target"]
            try:
                _node_scope(source, workspace, source_spec.zone.value)
                if relationship == "HAS":
                    if USER_MEMORY.name not in row["target_labels"]:
                        raise ValueError("HAS target must be UserMemory")
                    _node_scope(target, workspace, USER_MEMORY.zone.value)
                    if source["owner_id"] != target["owner_id"]:
                        raise ValueError("HAS endpoints have different owners")
                else:
                    targets = [
                        spec
                        for label, spec in _REFERENCE_TARGETS.items()
                        if label in row["target_labels"]
                    ]
                    if len(targets) != 1:
                        raise ValueError(
                            "REFERS_TO target must have one supported MG/KG label"
                        )
                    _node_scope(target, workspace, targets[0].zone.value)
                    require_trimmed_text(target.get("key"), "REFERS_TO target key")
                missing = _missing(
                    row["properties"],
                    {
                        "workspace_id": workspace,
                        "owner_id": source["owner_id"],
                        "zone": source_spec.zone.value,
                    },
                )
                if missing:
                    patches.append(
                        {"element_id": row["element_id"], "properties": missing}
                    )
            except (TypeError, ValueError) as exc:
                report["issues"].append(
                    {
                        "kind": relationship,
                        "element_id": row["element_id"],
                        "message": str(exc),
                    }
                )
        report["edges"][relationship] = {
            "scanned": len(rows),
            "pending": len(patches),
            "updated": 0,
        }
        writes.append(
            (
                "edges",
                relationship,
                patches,
                f"MATCH (s:{source_spec.name})-[r:{relationship}]->() "
                "WHERE elementId(r) = row.element_id AND s.workspace_id = $workspace_id "
                "AND all(field IN keys(row.properties) WHERE r[field] IS NULL) "
                "SET r += row.properties RETURN count(r) AS updated",
            )
        )

    report["ready"] = not report["issues"]
    if dry_run:
        return report
    if not report["ready"]:
        raise UserGraphMigrationError(report)
    for group, name, patches, query in writes:
        if not patches:
            continue
        rows = runtime.write(
            "UNWIND $rows AS row " + query,
            {"workspace_id": workspace, "rows": patches},
            operation=f"{_OPERATION}.apply.{name}",
        )
        updated = rows[0]["updated"]
        if updated != len(patches):
            raise RuntimeError(
                "User Graph changed during migration; stop writers and retry"
            )
        report[group][name]["updated"] = updated
    report["applied"] = True
    return report


def _node_scope(values: Mapping[str, object], workspace: str, zone: str) -> None:
    """Require reliable persisted scope before deriving keys or edge ownership."""
    if values.get("workspace_id") != workspace or values.get("zone") != zone:
        raise ValueError(
            "node workspace_id or zone does not match the migration target"
        )
    require_trimmed_text(values.get("owner_id"), "node owner_id")


def _missing(
    values: Mapping[str, object],
    expected: Mapping[str, object],
) -> dict[str, object]:
    """Fill absent properties and report conflicting existing values."""
    result = {}
    for field, value in expected.items():
        if values.get(field) is None:
            result[field] = value
        elif values[field] != value:
            raise ValueError(f"existing {field} differs from the expected value")
    return result

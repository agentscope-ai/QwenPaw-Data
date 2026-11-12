from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "migrate"
    / "migrate_workspace_graph.py"
)


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("migrate_workspace_graph", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


migration = _load_script()


class SnapshotSession:
    def __init__(
        self, nodes: list[dict[str, Any]], relationships: list[dict[str, Any]]
    ) -> None:
        self.nodes = nodes
        self.relationships = relationships

    def run(self, query: str, **_: Any) -> list[dict[str, Any]]:
        if "RETURN elementId(n) AS migration_id" in query:
            return self.nodes
        if "RETURN elementId(a) AS source_id" in query:
            return self.relationships
        raise AssertionError(query)


class InventorySession:
    def run(self, query: str, **_: Any) -> list[dict[str, Any]]:
        if "RETURN n.workspace_id AS workspace_id, count(n)" in query:
            return [{"workspace_id": "w1", "count": 1}]
        if "RETURN a.workspace_id AS source_workspace_id" in query:
            return [
                {
                    "source_workspace_id": "w2",
                    "target_workspace_id": "w1",
                    "count": 1,
                }
            ]
        if "WHERE n.workspace_id IS NULL" in query:
            return [
                {
                    "element_id": "orphan",
                    "labels": ["Claim"],
                    "owner_id": "private-user",
                }
            ]
        if "WHERE a.workspace_id IS NULL" in query:
            return []
        if "RETURN elementId(n) AS element_id" in query:
            return [{"element_id": "n1", "labels": ["Claim"], "owner_id": None}]
        if "WHERE (a.workspace_id = $ws" in query:
            return [
                {
                    "relationship_id": "r1",
                    "relationship_type": "LINKS",
                    "source_id": "outside",
                    "source_workspace_id": "w2",
                    "target_id": "n1",
                    "target_workspace_id": "w1",
                }
            ]
        if "RETURN elementId(n) AS migration_id" in query:
            return [
                {
                    "migration_id": "n1",
                    "labels": ["Claim"],
                    "properties": {"workspace_id": "w1", "owner_id": None},
                }
            ]
        if "RETURN elementId(a) AS source_id" in query:
            return []
        raise AssertionError(query)


def test_stable_hash_ignores_order_ids_and_migration_metadata() -> None:
    source = SnapshotSession(
        [
            {
                "migration_id": "source-2",
                "labels": ["Metric", "Reviewed"],
                "properties": {"workspace_id": "w1", "owner_id": "u1", "key": "b"},
            },
            {
                "migration_id": "source-1",
                "labels": ["Metric"],
                "properties": {"key": "a", "owner_id": "u1", "workspace_id": "w1"},
            },
        ],
        [
            {
                "source_id": "source-1",
                "target_id": "source-2",
                "rel_type": "DEPENDS_ON",
                "properties": {"weight": 1},
            }
        ],
    )
    target = SnapshotSession(
        [
            {
                "migration_id": "target-9",
                "labels": ["Reviewed", "Metric"],
                "properties": {
                    "key": "b",
                    "workspace_id": "w1",
                    "owner_id": "u1",
                    "_dataaxon_migration_id": "source-2",
                },
            },
            {
                "migration_id": "target-3",
                "labels": ["Metric"],
                "properties": {
                    "workspace_id": "w1",
                    "owner_id": "u1",
                    "key": "a",
                    "_dataaxon_migration_id": "source-1",
                },
            },
        ],
        [
            {
                "source_id": "target-3",
                "target_id": "target-9",
                "rel_type": "DEPENDS_ON",
                "properties": {"weight": 1},
            }
        ],
    )

    assert migration.graph_snapshot(source, "w1") == migration.graph_snapshot(
        target, "w1"
    )


def test_inventory_quarantines_incoming_cross_edge_without_reassigning_owner() -> None:
    report, manifest = migration.inventory(InventorySession(), "w1", {"u1"})

    assert report["cross_workspace_relationships"] == 1
    assert report["invalid_owner_nodes"] == 1
    assert report["owner_label_counts"] == {"invalid": {"Claim": 1}}
    assert manifest["cross_workspace_relationships"][0]["source_workspace_id"] == "w2"
    assert manifest["invalid_owner_nodes"][0]["owner_id"] is None
    assert manifest["missing_workspace_nodes"][0]["owner_id"] == "private-user"


@pytest.mark.parametrize(
    ("owner_id", "valid", "expected"),
    [
        (None, None, "missing_owner_id"),
        (" ", None, "missing_owner_id"),
        ("bad owner", None, "malformed_owner_id"),
        ("__global__", set(), None),
        ("u1", {"u1"}, None),
        ("u2", {"u1"}, "unknown_owner_id"),
    ],
)
def test_owner_validation(
    owner_id: Any, valid: set[str] | None, expected: str | None
) -> None:
    assert migration.owner_issue(owner_id, valid) == expected


def _control_plane(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE workspaces (
            id TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            endpoint_ref TEXT,
            secret_ref TEXT,
            instance_status TEXT,
            instance_version TEXT,
            status TEXT
        );
        CREATE TABLE users (id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL);
        INSERT INTO workspaces VALUES ('w1', 't1', 'old', 'old-secret', 'pending', NULL, 'active');
        INSERT INTO users VALUES ('u1', 't1');
        INSERT INTO users VALUES ('u2', 't2');
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{path}"


def test_control_plane_owner_scope_and_atomic_update(tmp_path: Path) -> None:
    database = tmp_path / "control.db"
    dsn = _control_plane(database)
    assert migration.control_plane_owners(dsn, "w1") == {"u1"}

    with pytest.raises(RuntimeError):
        migration.update_control_plane(dsn, "missing", "new", "secret", "v1")

    conn = sqlite3.connect(database)
    assert conn.execute(
        "SELECT endpoint_ref, instance_status FROM workspaces WHERE id = 'w1'"
    ).fetchone() == ("old", "pending")
    conn.close()

    migration.update_control_plane(dsn, "w1", "new", "secret", "v1")
    conn = sqlite3.connect(database)
    assert conn.execute(
        """
        SELECT endpoint_ref, secret_ref, instance_status, instance_version
        FROM workspaces WHERE id = 'w1'
        """
    ).fetchone() == ("new", "secret", "queued", "v1")
    conn.close()

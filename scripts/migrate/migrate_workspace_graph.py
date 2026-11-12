#!/usr/bin/env python3
"""Audit or migrate one Workspace graph into an isolated Neo4j instance.

The command is dry-run by default.  It never mutates the source graph.
Credentials are read from environment variables so they do not appear in the
process list.

The optional control-plane access (``--control-plane-dsn``) follows the
unified database model: SQLite and Hologres/PostgreSQL DSNs are both reached
through ``qwenpaw_data_auth.database``, so the same command works regardless of the
active relational backend.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Mapping

from qwenpaw_data_auth.database import get_db
from neo4j import GraphDatabase


NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OWNER_ID = re.compile(r"^[^\s\x00-\x1f\x7f]{1,256}$")
MIGRATION_PROPERTY = "_dataaxon_migration_id"


def identifier(value: str) -> str:
    if not NAME.fullmatch(value):
        raise ValueError(f"Unsafe Neo4j identifier: {value!r}")
    return f"`{value}`"


def required_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"Missing environment variable: {name}")
    return value


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row)


def _canonical(value: Any) -> Any:
    """Convert Neo4j values to deterministic, JSON-serializable values."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value:
            return {"$float": "nan"}
        if value == float("inf"):
            return {"$float": "inf"}
        if value == float("-inf"):
            return {"$float": "-inf"}
        return value
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (datetime, date, time)):
        return {"$temporal": value.isoformat()}
    if isinstance(value, Mapping):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        items = [_canonical(item) for item in value]
        return sorted(items, key=_json_bytes)
    if hasattr(value, "iso_format"):
        return {"$temporal": str(value.iso_format())}
    # Neo4j spatial values expose stable coordinates and an SRID.
    if hasattr(value, "srid") and hasattr(value, "__iter__"):
        return {
            "$point": {
                "srid": int(value.srid),
                "coordinates": [_canonical(item) for item in value],
            }
        }
    return {"$type": type(value).__name__, "$value": str(value)}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def owner_issue(owner_id: Any, valid_owner_ids: set[str] | None) -> str | None:
    if owner_id is None or (isinstance(owner_id, str) and not owner_id.strip()):
        return "missing_owner_id"
    if not isinstance(owner_id, str) or not OWNER_ID.fullmatch(owner_id):
        return "malformed_owner_id"
    if owner_id == "__global__":
        return None
    if valid_owner_ids is not None and owner_id not in valid_owner_ids:
        return "unknown_owner_id"
    return None


def graph_snapshot(session: Any, workspace_id: str) -> dict[str, Any]:
    """Return counts, label distribution and a stable content/connectivity hash."""
    node_rows = [
        _row_dict(row)
        for row in session.run(
            """
            MATCH (n)
            WHERE n.workspace_id = $ws
            RETURN elementId(n) AS migration_id, labels(n) AS labels,
                   properties(n) AS properties
            """,
            ws=workspace_id,
        )
    ]
    node_payloads: dict[str, dict[str, Any]] = {}
    label_counts: dict[str, int] = defaultdict(int)
    canonical_nodes: list[dict[str, Any]] = []
    for row in node_rows:
        properties = dict(row["properties"])
        properties.pop(MIGRATION_PROPERTY, None)
        labels = sorted(str(label) for label in row["labels"])
        payload = {"labels": labels, "properties": _canonical(properties)}
        node_payloads[str(row["migration_id"])] = payload
        canonical_nodes.append(payload)
        for label in labels:
            label_counts[label] += 1

    rel_rows = [
        _row_dict(row)
        for row in session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE a.workspace_id = $ws AND b.workspace_id = $ws
            RETURN elementId(a) AS source_id, elementId(b) AS target_id,
                   type(r) AS rel_type, properties(r) AS properties
            """,
            ws=workspace_id,
        )
    ]
    canonical_relationships = []
    for row in rel_rows:
        source_payload = node_payloads.get(str(row["source_id"]))
        target_payload = node_payloads.get(str(row["target_id"]))
        if source_payload is None or target_payload is None:
            raise RuntimeError("Snapshot relationship endpoint is outside Workspace")
        canonical_relationships.append(
            {
                "source": _digest(source_payload),
                "target": _digest(target_payload),
                "type": str(row["rel_type"]),
                "properties": _canonical(dict(row["properties"])),
            }
        )
    canonical_nodes.sort(key=_json_bytes)
    canonical_relationships.sort(key=_json_bytes)
    return {
        "nodes": len(canonical_nodes),
        "relationships": len(canonical_relationships),
        "label_counts": dict(sorted(label_counts.items())),
        "content_hash": _digest(
            {"nodes": canonical_nodes, "relationships": canonical_relationships}
        ),
    }


def inventory(
    session: Any,
    workspace_id: str,
    valid_owner_ids: set[str] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    workspace_nodes = [
        _row_dict(row)
        for row in session.run(
            """
            MATCH (n)
            RETURN n.workspace_id AS workspace_id, count(n) AS count
            ORDER BY workspace_id
            """
        )
    ]
    relationship_distribution = [
        _row_dict(row)
        for row in session.run(
            """
            MATCH (a)-[r]->(b)
            RETURN a.workspace_id AS source_workspace_id,
                   b.workspace_id AS target_workspace_id, count(r) AS count
            ORDER BY source_workspace_id, target_workspace_id
            """
        )
    ]
    missing_nodes = [
        _row_dict(row)
        for row in session.run(
            """
            MATCH (n)
            WHERE n.workspace_id IS NULL OR trim(toString(n.workspace_id)) = ''
            RETURN elementId(n) AS element_id, labels(n) AS labels,
                   n.owner_id AS owner_id
            ORDER BY element_id
            """
        )
    ]
    missing_workspace_relationships = [
        {
            **_row_dict(row),
            "relationship_id": str(row["relationship_id"]),
            "source_id": str(row["source_id"]),
            "target_id": str(row["target_id"]),
            "source_workspace_id": _canonical(row["source_workspace_id"]),
            "target_workspace_id": _canonical(row["target_workspace_id"]),
        }
        for row in session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE a.workspace_id IS NULL OR trim(toString(a.workspace_id)) = ''
               OR b.workspace_id IS NULL OR trim(toString(b.workspace_id)) = ''
            RETURN elementId(r) AS relationship_id, type(r) AS relationship_type,
                   elementId(a) AS source_id, a.workspace_id AS source_workspace_id,
                   elementId(b) AS target_id, b.workspace_id AS target_workspace_id
            ORDER BY relationship_id
            """
        )
    ]
    selected_nodes = [
        _row_dict(row)
        for row in session.run(
            """
            MATCH (n)
            WHERE n.workspace_id = $ws
            RETURN elementId(n) AS element_id, labels(n) AS labels,
                   n.owner_id AS owner_id
            ORDER BY element_id
            """,
            ws=workspace_id,
        )
    ]
    invalid_owner_nodes = []
    owner_label_counts: dict[str, dict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for row in selected_nodes:
        issue = owner_issue(row.get("owner_id"), valid_owner_ids)
        category = (
            "global"
            if row.get("owner_id") == "__global__"
            else "user"
            if issue is None
            else "invalid"
        )
        for label in sorted(str(value) for value in row["labels"]):
            owner_label_counts[category][label] += 1
        if issue:
            invalid_owner_nodes.append(
                {
                    "element_id": str(row["element_id"]),
                    "labels": sorted(str(value) for value in row["labels"]),
                    "owner_id": _canonical(row.get("owner_id")),
                    "reason": issue,
                }
            )
    cross_relationships = [
        {
            **_row_dict(row),
            "relationship_id": str(row["relationship_id"]),
            "source_id": str(row["source_id"]),
            "target_id": str(row["target_id"]),
        }
        for row in session.run(
            """
            MATCH (a)-[r]->(b)
            WHERE (a.workspace_id = $ws AND
                   (b.workspace_id IS NULL OR b.workspace_id <> $ws))
               OR (b.workspace_id = $ws AND
                   (a.workspace_id IS NULL OR a.workspace_id <> $ws))
            RETURN elementId(r) AS relationship_id, type(r) AS relationship_type,
                   elementId(a) AS source_id, a.workspace_id AS source_workspace_id,
                   elementId(b) AS target_id, b.workspace_id AS target_workspace_id
            ORDER BY relationship_id
            """,
            ws=workspace_id,
        )
    ]
    snapshot = graph_snapshot(session, workspace_id)
    report = {
        **snapshot,
        "workspace_node_counts": [
            {
                "workspace_id": _canonical(row["workspace_id"]),
                "count": int(row["count"]),
            }
            for row in workspace_nodes
        ],
        "workspace_relationship_counts": [
            {
                "source_workspace_id": _canonical(row["source_workspace_id"]),
                "target_workspace_id": _canonical(row["target_workspace_id"]),
                "count": int(row["count"]),
            }
            for row in relationship_distribution
        ],
        "missing_workspace_nodes": len(missing_nodes),
        "missing_workspace_relationships": len(missing_workspace_relationships),
        "invalid_owner_nodes": len(invalid_owner_nodes),
        "cross_workspace_relationships": len(cross_relationships),
        # Keep the original field for consumers of the old JSON output.
        "cross_workspace_relationships_from_workspace": len(cross_relationships),
        "owner_label_counts": {
            category: dict(sorted(counts.items()))
            for category, counts in sorted(owner_label_counts.items())
        },
        "owner_validation": (
            "control_plane" if valid_owner_ids is not None else "structural_only"
        ),
    }
    manifest = {
        "format_version": 1,
        "workspace_id": workspace_id,
        "missing_workspace_nodes": [
            {
                "element_id": str(row["element_id"]),
                "labels": sorted(str(value) for value in row["labels"]),
                "owner_id": _canonical(row.get("owner_id")),
                "reason": "missing_workspace_id",
            }
            for row in missing_nodes
        ],
        "missing_workspace_relationships": missing_workspace_relationships,
        "invalid_owner_nodes": invalid_owner_nodes,
        "cross_workspace_relationships": cross_relationships,
    }
    return report, manifest


def migrate_nodes(source: Any, target: Any, workspace_id: str, batch_size: int) -> int:
    migrated = 0
    after = ""
    while True:
        rows = list(
            source.run(
                """
                MATCH (n)
                WHERE n.workspace_id = $ws AND elementId(n) > $after
                RETURN elementId(n) AS migration_id, labels(n) AS labels,
                       properties(n) AS properties
                ORDER BY migration_id
                LIMIT $limit
                """,
                ws=workspace_id,
                after=after,
                limit=batch_size,
            )
        )
        if not rows:
            break
        grouped: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            labels = tuple(sorted(str(label) for label in row["labels"]))
            for label in labels:
                identifier(label)
            grouped[labels].append(
                {
                    "migration_id": row["migration_id"],
                    "properties": dict(row["properties"]),
                }
            )
        for labels, items in grouped.items():
            label_expr = "".join(f":{identifier(label)}" for label in labels)
            target.run(
                f"""
                UNWIND $items AS item
                CREATE (n{label_expr})
                SET n = item.properties,
                    n._dataaxon_migration_id = item.migration_id
                """,
                items=items,
            ).consume()
        migrated += len(rows)
        after = rows[-1]["migration_id"]
    return migrated


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def control_plane_owners(dsn: str, workspace_id: str) -> set[str]:
    with get_db(dsn) as conn:
        workspace = conn.execute(
            "SELECT tenant_id FROM workspaces WHERE id = ?", (workspace_id,)
        ).fetchone()
        if workspace is None:
            raise ValueError(f"Workspace {workspace_id!r} is absent from control plane")
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT id FROM users WHERE tenant_id = ?", (workspace[0],)
            )
        }


def update_control_plane(
    dsn: str,
    workspace_id: str,
    endpoint_ref: str,
    secret_ref: str,
    instance_version: str | None,
) -> None:
    """Queue schema preparation on the verified target before admitting traffic."""
    with get_db(dsn) as conn:
        try:
            cursor = conn.execute(
                """
                UPDATE workspaces
                SET endpoint_ref = ?, secret_ref = ?, instance_status = 'queued',
                    status = 'provisioning',
                    instance_version = COALESCE(?, instance_version)
                WHERE id = ? AND instance_status NOT IN ('queued', 'provisioning')
                """,
                (endpoint_ref, secret_ref, instance_version, workspace_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Control-plane Workspace {workspace_id!r} was not updated"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _safe_manifest_name(workspace_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", workspace_id)
    return f"quarantine-{safe or 'workspace'}.json"


def migrate_relationships(
    source: Any, target: Any, workspace_id: str, batch_size: int
) -> int:
    migrated = 0
    after = ""
    while True:
        rows = list(
            source.run(
                """
                MATCH (a)-[r]->(b)
                WHERE a.workspace_id = $ws AND b.workspace_id = $ws
                  AND elementId(r) > $after
                RETURN elementId(r) AS migration_id,
                       elementId(a) AS source_id,
                       elementId(b) AS target_id,
                       type(r) AS rel_type,
                       properties(r) AS properties
                ORDER BY migration_id
                LIMIT $limit
                """,
                ws=workspace_id,
                after=after,
                limit=batch_size,
            )
        )
        if not rows:
            break
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            rel_type = str(row["rel_type"])
            identifier(rel_type)
            grouped[rel_type].append(
                {
                    "source_id": row["source_id"],
                    "target_id": row["target_id"],
                    "properties": dict(row["properties"]),
                }
            )
        for rel_type, items in grouped.items():
            target.run(
                f"""
                UNWIND $items AS item
                MATCH (a {{_dataaxon_migration_id: item.source_id}})
                MATCH (b {{_dataaxon_migration_id: item.target_id}})
                CREATE (a)-[r:{identifier(rel_type)}]->(b)
                SET r = item.properties
                """,
                items=items,
            ).consume()
        migrated += len(rows)
        after = rows[-1]["migration_id"]
    return migrated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--allow-nonempty-target", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--quarantine-manifest",
        help="Manifest path (default: quarantine-<workspace-id>.json)",
    )
    parser.add_argument(
        "--control-plane-dsn",
        default=os.environ.get("QWENPAW_DATA_DB_URL", ""),
        help=(
            "Optional control-plane DSN used to validate owner IDs "
            "(sqlite:/// or postgresql://... for Hologres)"
        ),
    )
    parser.add_argument("--update-control-plane", action="store_true")
    parser.add_argument("--target-endpoint-ref")
    parser.add_argument("--target-secret-ref")
    parser.add_argument("--instance-version")
    args = parser.parse_args()
    if args.batch_size < 1 or args.batch_size > 5000:
        parser.error("--batch-size must be between 1 and 5000")

    if args.update_control_plane:
        if not args.execute:
            parser.error("--update-control-plane requires --execute")
        if not args.control_plane_dsn:
            parser.error("--update-control-plane requires --control-plane-dsn")
        if not args.target_endpoint_ref or not args.target_secret_ref:
            parser.error(
                "--update-control-plane requires --target-endpoint-ref and "
                "--target-secret-ref"
            )
    valid_owner_ids = (
        control_plane_owners(args.control_plane_dsn, args.workspace_id)
        if args.control_plane_dsn
        else None
    )
    manifest_path = Path(
        args.quarantine_manifest or _safe_manifest_name(args.workspace_id)
    )

    source = GraphDatabase.driver(
        required_env("SOURCE_NEO4J_URI"),
        auth=(required_env("SOURCE_NEO4J_USER"), required_env("SOURCE_NEO4J_PASSWORD")),
    )
    try:
        with source.session() as source_session:
            before, manifest = inventory(
                source_session, args.workspace_id, valid_owner_ids
            )
            _write_manifest(manifest_path, manifest)
            print(
                json.dumps(
                    {
                        "workspace_id": args.workspace_id,
                        "source": before,
                        "quarantine_manifest": str(manifest_path),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            if not args.execute:
                return 0
            if before["cross_workspace_relationships"]:
                raise RuntimeError(
                    "Cross-workspace relationships found; resolve them before migration"
                )
            if before["invalid_owner_nodes"]:
                raise RuntimeError(
                    "Missing or invalid owner_id values found; see quarantine manifest"
                )

            target = GraphDatabase.driver(
                required_env("TARGET_NEO4J_URI"),
                auth=(
                    required_env("TARGET_NEO4J_USER"),
                    required_env("TARGET_NEO4J_PASSWORD"),
                ),
            )
            try:
                with target.session() as target_session:
                    existing = target_session.run(
                        "MATCH (n) RETURN count(n) AS count"
                    ).single()["count"]
                    if existing and not args.allow_nonempty_target:
                        raise RuntimeError("Target graph is not empty")
                    node_count = migrate_nodes(
                        source_session,
                        target_session,
                        args.workspace_id,
                        args.batch_size,
                    )
                    rel_count = migrate_relationships(
                        source_session,
                        target_session,
                        args.workspace_id,
                        args.batch_size,
                    )
                    after = graph_snapshot(target_session, args.workspace_id)
                    if (
                        node_count != before["nodes"]
                        or rel_count != before["relationships"]
                    ):
                        raise RuntimeError("Migrated counts do not match source counts")
                    verification_fields = (
                        "nodes",
                        "relationships",
                        "label_counts",
                        "content_hash",
                    )
                    mismatches = [
                        field
                        for field in verification_fields
                        if after[field] != before[field]
                    ]
                    if mismatches:
                        raise RuntimeError(
                            "Target verification failed for: " + ", ".join(mismatches)
                        )
                    target_session.run(
                        f"""
                        MATCH (n)
                        WHERE n.{MIGRATION_PROPERTY} IS NOT NULL
                        REMOVE n.{MIGRATION_PROPERTY}
                        """
                    ).consume()
                    final_target = graph_snapshot(target_session, args.workspace_id)
                    if final_target != after:
                        raise RuntimeError(
                            "Target changed while removing migration metadata"
                        )
                    source_after = graph_snapshot(source_session, args.workspace_id)
                    if source_after != {
                        field: before[field]
                        for field in (
                            "nodes",
                            "relationships",
                            "label_counts",
                            "content_hash",
                        )
                    }:
                        raise RuntimeError(
                            "Source Workspace changed during migration; refusing cutover"
                        )
                    print(
                        json.dumps(
                            {
                                "workspace_id": args.workspace_id,
                                "migrated_nodes": node_count,
                                "migrated_relationships": rel_count,
                                "target": final_target,
                                "verified": True,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                    )
            finally:
                target.close()
            if args.update_control_plane:
                update_control_plane(
                    args.control_plane_dsn,
                    args.workspace_id,
                    args.target_endpoint_ref,
                    args.target_secret_ref,
                    args.instance_version,
                )
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

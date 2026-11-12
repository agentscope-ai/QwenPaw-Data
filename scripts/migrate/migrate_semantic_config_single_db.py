#!/usr/bin/env python3
"""Validate and repair one datasource during split-DB to single-DB migration.

The command is dry-run by default.  ``--apply`` creates SQLite-consistent
backups, copies only datasource connection metadata into the existing target
row, validates row-level scope, and archives the legacy split database.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import time
from pathlib import Path


SCOPED_TABLES = (
    "datasource",
    "biz_domain",
    "dataset_meta",
    "dataset_column_meta",
    "dimension",
    "dataset_dimension",
    "dataset_dimension_value",
    "metric_lib",
    "metric_formula_lib",
    "weave_task",
)


def backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.execute("PRAGMA wal_checkpoint(FULL)")
        src.backup(dst)


def fetch_datasource(con: sqlite3.Connection, datasource_id: str) -> sqlite3.Row:
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT * FROM datasource WHERE datasource_id = ? AND is_deleted = 0",
        (datasource_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"Datasource not found: {datasource_id}")
    return row


def validate_target(
    con: sqlite3.Connection,
    *,
    tenant_id: str,
    workspace_id: str,
    datasource_id: str,
) -> None:
    row = fetch_datasource(con, datasource_id)
    if row["tenant_id"] != tenant_id or row["workspace_id"] != workspace_id:
        raise RuntimeError("Target datasource scope does not match arguments")

    for table in SCOPED_TABLES:
        columns = {
            item[1] for item in con.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not {"tenant_id", "workspace_id"}.issubset(columns):
            raise RuntimeError(f"{table} is missing tenant/workspace columns")
        bad_scope = con.execute(
            f"""
            SELECT COUNT(*) FROM {table}
            WHERE datasource_id = ?
              AND (tenant_id IS NULL OR workspace_id IS NULL
                   OR tenant_id <> ? OR workspace_id <> ?)
            """,
            (datasource_id, tenant_id, workspace_id),
        ).fetchone()[0]
        if bad_scope:
            raise RuntimeError(f"{table} has {bad_scope} rows with invalid scope")
        if table != "datasource":
            non_text = con.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE datasource_id = ? AND typeof(datasource_id) <> 'text'
                """,
                (datasource_id,),
            ).fetchone()[0]
            if non_text:
                raise RuntimeError(
                    f"{table} has {non_text} non-TEXT datasource references"
                )

    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise RuntimeError(f"SQLite integrity_check failed: {integrity}")


def archive_legacy(legacy_db: Path, archive_dir: Path) -> None:
    archive_dir.mkdir(parents=True, exist_ok=True)
    for candidate in (
        legacy_db,
        Path(f"{legacy_db}-wal"),
        Path(f"{legacy_db}-shm"),
    ):
        if candidate.exists():
            shutil.move(str(candidate), archive_dir / candidate.name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--legacy-db", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--legacy-id", required=True)
    parser.add_argument("--backup-root", type=Path, default=Path("data/backup"))
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    db = args.db.expanduser().resolve()
    legacy_db = args.legacy_db.expanduser().resolve()
    if not db.is_file() or not legacy_db.is_file():
        raise SystemExit("Both --db and --legacy-db must exist")

    with sqlite3.connect(db) as target, sqlite3.connect(legacy_db) as legacy:
        target.row_factory = sqlite3.Row
        legacy.row_factory = sqlite3.Row
        target_row = fetch_datasource(target, args.target_id)
        legacy_row = fetch_datasource(legacy, args.legacy_id)
        validate_target(
            target,
            tenant_id=args.tenant_id,
            workspace_id=args.workspace_id,
            datasource_id=args.target_id,
        )
        print(
            "Validated target:",
            args.target_id,
            f"scope={args.tenant_id}/{args.workspace_id}",
            f"config_present={bool(target_row['config'])}",
            f"legacy_config_present={bool(legacy_row['config'])}",
        )

    if not args.apply:
        print("Dry run only; pass --apply after stopping semantic-config writers.")
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup_dir = args.backup_root.expanduser().resolve() / f"semantic-single-{stamp}"
    backup_database(db, backup_dir / db.name)
    backup_database(legacy_db, backup_dir / legacy_db.name)

    with (
        sqlite3.connect(db, timeout=30) as target,
        sqlite3.connect(legacy_db) as legacy,
    ):
        target.row_factory = sqlite3.Row
        legacy.row_factory = sqlite3.Row
        legacy_row = fetch_datasource(legacy, args.legacy_id)
        if not legacy_row["config"]:
            raise RuntimeError("Legacy datasource has no config to migrate")
        target.execute("BEGIN IMMEDIATE")
        try:
            changed = target.execute(
                """
                UPDATE datasource
                SET datasource_name = ?, datasource_type = ?, config = ?,
                    updated_at = datetime('now')
                WHERE datasource_id = ? AND tenant_id = ? AND workspace_id = ?
                  AND is_deleted = 0
                """,
                (
                    legacy_row["datasource_name"],
                    legacy_row["datasource_type"],
                    legacy_row["config"],
                    args.target_id,
                    args.tenant_id,
                    args.workspace_id,
                ),
            ).rowcount
            if changed != 1:
                raise RuntimeError(f"Expected one target row, updated {changed}")
            validate_target(
                target,
                tenant_id=args.tenant_id,
                workspace_id=args.workspace_id,
                datasource_id=args.target_id,
            )
            target.commit()
        except Exception:
            target.rollback()
            raise

    archive_legacy(
        legacy_db,
        args.backup_root.expanduser().resolve() / "semantic-config-split" / stamp,
    )
    print(f"Migration completed; backups: {backup_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

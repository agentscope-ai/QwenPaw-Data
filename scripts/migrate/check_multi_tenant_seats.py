#!/usr/bin/env python3
"""Read-only check of tenant-seat uniqueness on the control plane.

A user row is one tenant seat.
Auth refuses to start if the data violates any of the rules below, because the
matching index cannot be built — this script reports the same violations
without touching anything.

Rules checked:

1. ``(tenant_id, username)`` is unique — names only have to be distinct inside
   one tenant.
2. ``username`` is unique among rows with a non-empty ``password_hash`` — local
   sign-in resolves an account by username alone, with no tenant to narrow it
   down, so password accounts must stay globally unambiguous.

Usage::

    python scripts/migrate/check_multi_tenant_seats.py
    python scripts/migrate/check_multi_tenant_seats.py --dsn postgresql://...

Exits 0 when the database is ready, 1 when a violation would block start-up.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "packages" / "qwenpaw-data-auth" / "src"))

from sqlalchemy.engine import make_url  # noqa: E402

from qwenpaw_data_auth.database import get_db, query_all  # noqa: E402


# Each entry is (rule label, SQL returning one row per offending group).
BLOCKING_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "duplicate (tenant_id, username)",
        """
        SELECT tenant_id, username, COUNT(*) AS cnt
        FROM users
        GROUP BY tenant_id, username
        HAVING COUNT(*) > 1
        ORDER BY tenant_id, username
        """,
    ),
    (
        "username shared by several password accounts",
        """
        SELECT username, COUNT(*) AS cnt
        FROM users
        WHERE password_hash != ''
        GROUP BY username
        HAVING COUNT(*) > 1
        ORDER BY username
        """,
    ),
)

# Not violations — a username may be reused across tenants.
INFO_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "usernames reused across tenants",
        """
        SELECT username, COUNT(DISTINCT tenant_id) AS cnt
        FROM users
        GROUP BY username
        HAVING COUNT(DISTINCT tenant_id) > 1
        ORDER BY username
        """,
    ),
)


def _backend(dsn: str) -> str:
    try:
        return make_url(dsn).get_backend_name()
    except Exception:  # noqa: BLE001 - bare paths are valid SQLite DSNs
        return "sqlite"


def _describe(row: dict) -> str:
    parts = [f"{key}={value!r}" for key, value in row.items() if key != "cnt"]
    return f"{', '.join(parts)} -> {row['cnt']} rows"


def _run(
    conn, checks: tuple[tuple[str, str], ...], limit: int
) -> dict[str, list[dict]]:
    findings: dict[str, list[dict]] = {}
    for label, sql in checks:
        rows = query_all(conn, sql)
        if rows:
            findings[label] = rows[:limit]
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default="",
        help="Control-plane DSN; defaults to $QWENPAW_DATA_DB_URL.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum offending groups printed per rule (default: 20).",
    )
    args = parser.parse_args()

    dsn = args.dsn or os.environ.get("QWENPAW_DATA_DB_URL", "")
    if not dsn:
        print("No DSN: pass --dsn or set QWENPAW_DATA_DB_URL.", file=sys.stderr)
        return 2

    backend = _backend(dsn)
    sqlite = backend == "sqlite"

    with get_db(dsn) as conn:
        total = query_all(conn, "SELECT COUNT(*) AS cnt FROM users")[0]["cnt"]
        blocking = _run(conn, BLOCKING_CHECKS, args.limit)
        info = _run(conn, INFO_CHECKS, args.limit)
        indexes = (
            query_all(
                conn,
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name LIKE 'idx_users%'
                ORDER BY name
                """,
            )
            if sqlite
            else []
        )

    print(f"backend: {backend}")
    print(f"users rows: {total}")
    if sqlite:
        print(
            "enforcement: database indexes + application checks\n"
            f"  current users indexes: {', '.join(r['name'] for r in indexes) or 'none'}\n"
            "  init_schema builds the per-tenant indexes."
        )
    else:
        print(
            "enforcement: application checks only\n"
            "  this backend supports no UNIQUE constraints, so init_schema "
            "builds no indexes here."
        )

    for label, rows in info.items():
        print(f"\n[info] {label}: {len(rows)}")
        for row in rows:
            print(f"  {_describe(row)}")

    if not blocking:
        print("\nOK — every uniqueness rule already holds.")
        if not sqlite:
            print("This backend builds no uniqueness indexes.")
        return 0

    for label, rows in blocking.items():
        print(f"\n[BLOCKING] {label}: {len(rows)}")
        for row in rows:
            print(f"  {_describe(row)}")
    if sqlite:
        print(
            "\nAuth will refuse to start until these are resolved. Rename the "
            "colliding usernames, or merge the duplicated seats, then re-run."
        )
    else:
        print(
            "\nThis backend cannot enforce uniqueness, so start-up will not "
            "refuse — but the rows above break assumptions the application "
            "relies on. Resolve them before deploying."
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

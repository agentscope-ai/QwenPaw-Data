#!/usr/bin/env python3
"""No-op on Neo4j Community (single database).

Community Edition only has the default ``neo4j`` database; ``CREATE DATABASE``
is not supported. This script simply verifies connectivity and returns 0.

Usage::

    python scripts/ensure_neo4j_databases.py

Environment: ``NEO4J_URI``, ``NEO4J_USER``, ``NEO4J_PASSWORD`` (same as context_manager).

Exit codes: ``0`` — Neo4j reachable; ``1`` — connection failed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from datapaw.context.env import load_datapaw_env

load_datapaw_env(override=False)


def main() -> int:
    try:
        from neo4j import GraphDatabase
    except ImportError:
        print("neo4j driver not installed; skip ensure_neo4j_databases", file=sys.stderr)
        return 0

    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    pwd = os.getenv("NEO4J_PASSWORD", "graphrag123")

    driver = GraphDatabase.driver(uri, auth=(user, pwd))
    try:
        driver.verify_connectivity()
        print("[neo4j] Community Edition: using default 'neo4j' database (no multi-db).")
    except Exception as e:  # noqa: BLE001
        print(f"[neo4j] connection failed: {e}", file=sys.stderr)
        return 1
    finally:
        driver.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

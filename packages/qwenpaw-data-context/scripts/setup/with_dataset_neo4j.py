#!/usr/bin/env python3
"""Run a setup script with ``NEO4J_DATABASE`` set to that dataset's logical DB.

Community Edition: only the default ``neo4j`` database exists, so this script
simply sets ``NEO4J_DATABASE=neo4j`` and execs the target script.

Usage::

    python scripts/setup/with_dataset_neo4j.py appdata -- scripts/setup/build_topology.py --dataset appdata
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from qwenpaw_data.context.env import load_qwenpaw_data_env

load_qwenpaw_data_env(override=False)

# Community Edition: all datasets share the single 'neo4j' database.
DATASET_TO_DB = {
    "appdata": "neo4j",
}


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[2] != "--":
        print(
            "Usage: with_dataset_neo4j.py <appdata> -- "
            "<script.py> [args...]",
            file=sys.stderr,
        )
        return 2

    dataset = sys.argv[1]
    if dataset not in DATASET_TO_DB:
        print(f"Unknown dataset key: {dataset}", file=sys.stderr)
        return 2

    script_and_args = sys.argv[3:]
    script_path = Path(script_and_args[0])
    if not script_path.is_file():
        print(f"Script not found: {script_path}", file=sys.stderr)
        return 2

    db = DATASET_TO_DB[dataset]
    target_uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")

    env = os.environ.copy()
    env["NEO4J_DATABASE"] = db
    env["NEO4J_URI"] = target_uri

    os.execvpe(sys.executable, [sys.executable, *[str(p) for p in script_and_args]], env)


if __name__ == "__main__":
    raise SystemExit(main())

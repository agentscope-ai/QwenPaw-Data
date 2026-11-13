#!/usr/bin/env python3
"""Run the Workspace graph provisioning queue.

The default worker script is selected by ``GRAPH_BACKEND``:
``neo4j`` starts a per-Workspace Docker container; ``holo_age`` provisioning is
not included in the open-source distribution, so the worker fails with a clear
error and the Workspace graph must be configured manually.

Auth only writes queue state to the control-plane database. The control plane
may be SQLite or Hologres (PostgreSQL wire protocol); both are reached through
``qwenpaw_data_auth.database``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from qwenpaw_data_auth.database import get_db, query_all, query_one


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

HOLO_AGE_UNAVAILABLE = (
    "Holo AGE workspace provisioning is not included in the open-source "
    "distribution; configure the Workspace graph manually"
)


def enqueue_initialization(dsn: str, workspace_id: str | None = None) -> list[str]:
    """Requeue registered graphs for explicit schema initialization."""
    with get_db(dsn) as conn:
        rows = query_all(
            conn,
            """
            SELECT w.id, w.tenant_id FROM workspaces w
            JOIN tenants t ON t.id = w.tenant_id
            WHERE COALESCE(w.endpoint_ref, '') <> ''
              AND w.status IN ('active', 'provisioning')
              AND w.instance_status IN ('ready', 'active', 'running', 'failed')
              AND t.status IN ('active', 'provisioning')
        """
            + (" AND w.id = ?" if workspace_id is not None else ""),
            (workspace_id,) if workspace_id is not None else (),
        )
        queued = []
        for row in rows:
            updated = conn.execute(
                """
                UPDATE workspaces SET instance_status = 'queued', status = 'provisioning'
                WHERE id = ? AND instance_status IN ('ready', 'active', 'running', 'failed')
                  AND status IN ('active', 'provisioning')
            """,
                (row["id"],),
            )
            if updated.rowcount == 1:
                conn.execute(
                    """
                    UPDATE tenants SET onboarding_state = 'pending', onboarding_error = NULL
                    WHERE id = ? AND status = 'provisioning' AND onboarding_state = 'failed'
                """,
                    (row["tenant_id"],),
                )
                queued.append(row["id"])
        conn.commit()
        return queued


def graph_backend() -> str:
    """Return the active graph backend. Unknown values fail closed."""
    backend = os.environ.get("GRAPH_BACKEND", "neo4j").strip().lower() or "neo4j"
    if backend in {"holoage", "holo-age"}:
        backend = "holo_age"
    if backend not in {"neo4j", "holo_age"}:
        raise ValueError(f"GRAPH_BACKEND must be neo4j or holo_age, got: {backend!r}")
    return backend


def _default_provision_script(script_dir: Path) -> Path:
    """按 GRAPH_BACKEND 选择默认开通脚本。

    - neo4j（默认）：provision_workspace_neo4j.py（拉起独立容器）
    - holo_age：开源版不含 Holo AGE 开通脚本，直接报错，需手动配置 Workspace 图
    """
    if graph_backend() == "holo_age":
        raise RuntimeError(HOLO_AGE_UNAVAILABLE)
    return script_dir / "provision_workspace_neo4j.py"


def claim_next(dsn: str) -> dict[str, str] | None:
    with get_db(dsn) as conn:
        # Two provisioning sources share the queue: tenant onboarding
        # (onboarding_state='pending') and workspace-level provisioning
        # requested by the tenant_admin on an already-active tenant.
        workspace = query_one(
            conn,
            """
            SELECT w.id AS id, w.tenant_id AS tenant_id,
                   w.instance_version AS instance_version, w.endpoint_ref
            FROM workspaces w
            JOIN tenants t ON t.id = w.tenant_id
            WHERE w.instance_status = 'queued'
              AND w.status = 'provisioning'
              AND t.status IN ('active', 'provisioning')
              AND (t.onboarding_state = 'pending' OR t.status = 'active')
            ORDER BY w.created_at
            LIMIT 1
            """,
        )
        if not workspace:
            conn.rollback()
            return None
        # The guarded UPDATE (rowcount == 1) is the atomic claim: a competing
        # worker that grabbed the same row first flips it out of 'queued' and
        # this UPDATE affects zero rows.
        updated = conn.execute(
            """
            UPDATE workspaces
            SET instance_status = 'provisioning', status = 'provisioning'
            WHERE id = ? AND instance_status = 'queued' AND status = 'provisioning'
            """,
            (workspace["id"],),
        )
        if updated.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return {
            "id": workspace["id"],
            "tenant_id": workspace["tenant_id"],
            "instance_version": workspace.get("instance_version") or "",
            "endpoint_ref": workspace.get("endpoint_ref") or "",
        }


def mark_failed(dsn: str, workspace: dict[str, str], error: str) -> None:
    detail = error.strip()[-2000:] or "Workspace provisioning failed"
    with get_db(dsn) as conn:
        conn.execute(
            """
            UPDATE workspaces
            SET instance_status = 'failed', status = 'provisioning'
            WHERE id = ? AND instance_status = 'provisioning'
            """,
            (workspace["id"],),
        )
        # Only demote the tenant while it is still onboarding; a failed extra
        # workspace of an active tenant must not knock the tenant offline.
        conn.execute(
            """
            UPDATE tenants
            SET status = 'provisioning', onboarding_state = 'failed',
                onboarding_error = ?
            WHERE id = ? AND onboarding_state = 'pending'
            """,
            (detail, workspace["tenant_id"]),
        )
        conn.commit()


def process_one(
    dsn: str,
    provision_script: Path | None,
    runner: CommandRunner = subprocess.run,
    *,
    holo_script: Path | None = None,
) -> bool:
    workspace = claim_next(dsn)
    if not workspace:
        return False

    script = provision_script
    if (
        workspace.get("instance_version") or ""
    ) == "holo_age" and holo_script is not None:
        script = holo_script
    elif (
        provision_script is None
        and workspace["endpoint_ref"]
        and workspace["instance_version"] != "holo_age"
    ):
        script = Path(__file__).with_name("provision_workspace_neo4j.py")
    try:
        script = script or _default_provision_script(Path(__file__).parent)
    except RuntimeError as exc:
        mark_failed(dsn, workspace, str(exc))
        print(
            f"Workspace {workspace['id']} provisioning failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return True
    command: Sequence[str] = (
        sys.executable,
        str(script),
        "--workspace-id",
        workspace["id"],
    )
    try:
        result = runner(
            command,
            check=False,
            env={**os.environ, "QWENPAW_DATA_DB_URL": dsn},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            mark_failed(dsn, workspace, result.stderr or result.stdout)
            print(
                f"Workspace {workspace['id']} provisioning failed: "
                f"{(result.stderr or result.stdout).strip()}",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(
                f"Workspace {workspace['id']} provisioning completed: "
                f"{result.stdout.strip()}",
                flush=True,
            )
    except Exception as exc:
        mark_failed(dsn, workspace, str(exc))
        print(
            f"Workspace {workspace['id']} provisioning failed: {exc}",
            file=sys.stderr,
            flush=True,
        )
    return True


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dsn",
        default=os.environ.get("QWENPAW_DATA_DB_URL", ""),
        help="Control-plane DSN (sqlite:/// or postgresql://... for Hologres)",
    )
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--once", action="store_true")
    initialize = parser.add_mutually_exclusive_group()
    initialize.add_argument(
        "--initialize-workspace",
        metavar="ID",
        help="Queue schema initialization for an existing Workspace and exit",
    )
    initialize.add_argument(
        "--initialize-all",
        action="store_true",
        help="Queue schema initialization for all eligible registered Workspaces and exit",
    )
    parser.add_argument(
        "--provision-script",
        type=Path,
        help="Override the default provisioning script (reads QWENPAW_DATA_DB_URL)",
    )
    parser.add_argument(
        "--holo-script",
        type=Path,
        default=None,
        help="Holo AGE provisioning script (not included in the open-source distribution)",
    )
    args = parser.parse_args()
    if not args.dsn:
        parser.error("--dsn or QWENPAW_DATA_DB_URL is required")
    if args.initialize_workspace or args.initialize_all:
        queued = enqueue_initialization(args.dsn, args.initialize_workspace)
        print(f"Queued schema initialization: {', '.join(queued) or '(none)'}")
        return 0 if queued or args.initialize_all else 1

    backend = graph_backend()
    if backend == "holo_age" and args.provision_script is None:
        raise SystemExit(HOLO_AGE_UNAVAILABLE)
    print(
        f"Workspace provisioner: GRAPH_BACKEND={backend} "
        f"script={(args.provision_script or _default_provision_script(script_dir)).name}",
        flush=True,
    )

    while True:
        processed = process_one(
            args.dsn, args.provision_script, holo_script=args.holo_script
        )
        if args.once:
            return 0
        if not processed:
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())

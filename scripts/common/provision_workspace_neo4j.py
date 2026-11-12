#!/usr/bin/env python3
"""Provision one isolated local Neo4j Community container for a Workspace.

This is the local/development implementation of WorkspaceGraphProvisioner.
It is intentionally a trusted operator CLI: web processes never receive the
Docker socket. Production deployments should replace it with their orchestrator.

The control plane (workspace/tenant/user metadata) may be SQLite or Hologres
(PostgreSQL wire protocol); it is reached through ``qwenpaw_data_auth.database`` so
the same provisioner works for both. The graph itself is always a per-workspace
Neo4j container regardless of the control-plane backend.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator


# Container deployments have no Docker daemon, so the image starts a fixed set of
# Neo4j processes up front and records them here; provisioning then only claims
# one. Set by the deployment that pre-starts the pool.
POOL_MANIFEST_ENV = "DP_NEO4J_POOL_MANIFEST"
LOCK_RETRY_SECONDS = 0.2
LOCK_TIMEOUT_SECONDS = 30.0

from qwenpaw_data_auth.database import get_db, query_one


def prepare_graph(workspace: Any, endpoint: str, secret: dict) -> None:
    """Call the Store initializer before publishing the registered route."""
    from context_manager.store.neo4j import GraphTarget, Neo4jRuntime
    from context_manager.store.neo4j.physical_schema import ensure_schema
    from neo4j import GraphDatabase

    target = GraphTarget(
        workspace["tenant_id"], workspace["id"], database=secret.get("database")
    )
    with closing(
        GraphDatabase.driver(endpoint, auth=(secret["user"], secret["password"]))
    ) as driver:
        ensure_schema(Neo4jRuntime(driver, target))


def resolve_env(reference: str) -> str:
    from context_manager.settings import get_settings

    for prefix in ("env://", "env:"):
        if reference.startswith(prefix):
            value = get_settings().environment_value(reference.removeprefix(prefix))
            if not value:
                raise RuntimeError("workspace reference is unavailable")
            return value
    return reference


def provision_registered(dsn: str, workspace: Any) -> int:
    """Prepare an existing instance without allocating or rotating credentials."""

    endpoint = resolve_env(workspace["endpoint_ref"])
    reference = workspace["secret_ref"] or ""
    if reference.startswith("file://"):
        raw = Path(reference.removeprefix("file://")).read_text(encoding="utf-8")
    elif reference.startswith(("env://", "env:")):
        raw = resolve_env(reference)
    else:
        raise RuntimeError("workspace secret reference is unsupported")
    secret = json.loads(raw)
    secret["database"] = secret.get("database") or workspace["neo4j_database"]
    prepare_graph(workspace, endpoint, secret)
    with get_db(dsn) as conn:
        activate_workspace(
            conn,
            workspace,
            workspace_id=workspace["id"],
            endpoint=workspace["endpoint_ref"],
            secret_ref=reference,
            instance_version=workspace["instance_version"],
        )
    print(json.dumps({"workspace_id": workspace["id"], "status": "ready"}))
    return 0


def run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout.strip() if capture else ""


def safe_name(workspace_id: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_.-]+", "-", workspace_id).strip("-.")
    if not normalized:
        raise ValueError("workspace_id cannot be converted to a safe container name")
    return normalized[:48].lower()


def wait_until_ready(container: str, user: str, password: str, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "cypher-shell",
                "-u",
                user,
                "-p",
                password,
                "RETURN 1",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise TimeoutError(f"Neo4j container {container} did not become ready")


# ────────────────────────────────────────────────────────────── #
#  Shared activation
# ────────────────────────────────────────────────────────────── #


def activate_workspace(
    conn: Any,
    workspace: Any,
    *,
    workspace_id: str,
    endpoint: str,
    secret_ref: str,
    instance_version: str,
) -> None:
    """Flip Workspace, tenant and pending users to active in one transaction.

    Shared by both provisioning modes so the Docker path and the pool path can
    never drift into activating a Workspace differently.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    # SQLite needs IMMEDIATE to serialize writers. PostgreSQL/Hologres reject it.
    if conn._c.dialect.name == "sqlite":
        conn.execute("BEGIN IMMEDIATE")
    updated = conn.execute(
        """
        UPDATE workspaces
        SET endpoint_ref = ?, secret_ref = ?, instance_status = 'ready',
            instance_version = ?, status = 'active'
        WHERE id = ? AND instance_status = 'provisioning' AND status = 'provisioning'
        """,
        (endpoint, secret_ref, instance_version, workspace_id),
    )
    if updated.rowcount != 1:
        raise RuntimeError("workspace is no longer provisioning")
    conn.execute(
        """
        UPDATE tenants
        SET status = 'active', onboarding_state = 'ready',
            onboarding_error = NULL
        WHERE id = ? AND status = 'provisioning' AND onboarding_state = 'pending'
        """,
        (workspace["tenant_id"],),
    )
    conn.execute(
        """
        UPDATE users
        SET status = 'active', token_revoked_before = ?
        WHERE tenant_id = ? AND status = 'pending'
        """,
        (now, workspace["tenant_id"]),
    )
    conn.commit()


# ────────────────────────────────────────────────────────────── #
#  Pre-started instance pool (no Docker)
# ────────────────────────────────────────────────────────────── #
#
# A container without a Docker daemon can pre-start N Neo4j processes on fixed
# ports and write a manifest describing them.
# Provisioning degenerates to a metadata claim: one Workspace takes one instance.
# Because the ports are fixed rather than Docker-assigned, there is no ephemeral
# remapping to repair in this mode.


@contextmanager
def manifest_lock(manifest_path: Path) -> Iterator[None]:
    """Serialise manifest updates so two claims cannot take the same instance."""
    lock_path = manifest_path.with_name(manifest_path.name + ".lock")
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            handle = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"could not acquire {lock_path} within {LOCK_TIMEOUT_SECONDS:.0f}s"
                ) from None
            time.sleep(LOCK_RETRY_SECONDS)
            continue
        break
    try:
        os.close(handle)
        yield
    finally:
        lock_path.unlink(missing_ok=True)


def read_manifest(manifest_path: Path) -> dict:
    """Load the pool manifest, failing loudly when absent or malformed."""
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(
            f"Neo4j pool manifest not found: {manifest_path}. It is written by "
            f"the deployment that pre-starts the pool."
        ) from None
    except ValueError as exc:
        raise RuntimeError(
            f"Neo4j pool manifest {manifest_path} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload.get("instances"), list):
        raise RuntimeError(
            f"Neo4j pool manifest {manifest_path} has no 'instances' list"
        )
    return payload


def write_manifest(manifest_path: Path, manifest: dict) -> None:
    """Persist the manifest, keeping the claim record readable for operators."""
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def claim_pool_instance(manifest_path: Path, workspace_id: str) -> dict:
    """Reserve one pool instance for this Workspace, reusing a prior claim.

    Re-running provisioning for a Workspace that already holds an instance must
    reuse it rather than consume a second one, so an existing claim wins over
    the first free entry — that is what keeps a rerun idempotent.
    """
    with manifest_lock(manifest_path):
        manifest = read_manifest(manifest_path)
        instances = manifest["instances"]
        chosen = next(
            (
                item
                for item in instances
                if str(item.get("claimed_by") or "") == workspace_id
            ),
            None,
        )
        if chosen is None:
            chosen = next(
                (item for item in instances if not str(item.get("claimed_by") or "")),
                None,
            )
            if chosen is None:
                raise RuntimeError(
                    f"Neo4j pool is exhausted: all {len(instances)} instance(s) are "
                    f"claimed. Add instances to the pool manifest."
                )
            chosen["claimed_by"] = workspace_id
            write_manifest(manifest_path, manifest)
        return dict(chosen)


def release_pool_instance(manifest_path: Path, workspace_id: str) -> None:
    """Return a claimed instance to the pool after a failed provision."""
    try:
        with manifest_lock(manifest_path):
            manifest = read_manifest(manifest_path)
            for item in manifest["instances"]:
                if str(item.get("claimed_by") or "") == workspace_id:
                    item["claimed_by"] = None
            write_manifest(manifest_path, manifest)
    except (OSError, RuntimeError, TimeoutError):
        # Best effort only: the provision already failed, and a leaked claim
        # merely costs one slot, which the next container start rebuilds.
        pass


def provision_from_pool(
    conn: Any,
    workspace: Any,
    *,
    workspace_id: str,
    manifest_path: Path,
    secret_path: Path,
) -> int:
    """Activate one Workspace against a pre-started pool instance."""
    instance = claim_pool_instance(manifest_path, workspace_id)
    port = str(instance.get("port") or "").strip()
    password = str(instance.get("password") or "")
    if not port.isdigit() or not password:
        release_pool_instance(manifest_path, workspace_id)
        raise RuntimeError(
            f"pool instance claimed for {workspace_id} has no usable port/password: "
            f"{ {key: value for key, value in instance.items() if key != 'password'} }"
        )
    endpoint = f"bolt://127.0.0.1:{port}"
    secret = {
        "user": str(instance.get("user") or "neo4j"),
        "password": password,
        "database": str(instance.get("database") or "neo4j"),
    }

    try:
        secret_path.write_text(
            json.dumps(secret),
            encoding="utf-8",
        )
        secret_path.chmod(0o600)
        prepare_graph(workspace, endpoint, secret)
        activate_workspace(
            conn,
            workspace,
            workspace_id=workspace_id,
            endpoint=endpoint,
            secret_ref=f"file://{secret_path}",
            instance_version=str(instance.get("version") or "neo4j-pool"),
        )
    except Exception:
        conn.rollback()
        secret_path.unlink(missing_ok=True)
        release_pool_instance(manifest_path, workspace_id)
        raise

    print(
        json.dumps(
            {
                "workspace_id": workspace_id,
                "source": "pool",
                "endpoint_ref": endpoint,
                "secret_ref": f"file://{secret_path}",
                "status": "ready",
            },
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("QWENPAW_DATA_DB_URL", ""),
        help="Control-plane DSN (sqlite:/// or postgresql://... for Hologres)",
    )
    parser.add_argument(
        "--image",
        default=os.environ.get("NEO4J_COMMUNITY_IMAGE", "neo4j:5.20-community"),
    )
    parser.add_argument(
        "--data-root",
        default=os.environ.get("QWENPAW_DATA_WORKSPACE_DATA_ROOT", "data/workspaces"),
    )
    parser.add_argument(
        "--from-pool",
        default=os.environ.get(POOL_MANIFEST_ENV, ""),
        help=(
            "claim a pre-started Neo4j from this pool manifest instead of running "
            "Docker; required where no Docker daemon exists (container deployments)"
        ),
    )
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    if not args.dsn:
        parser.error("--dsn or QWENPAW_DATA_DB_URL is required")
    pool_manifest = (
        Path(args.from_pool).expanduser() if str(args.from_pool).strip() else None
    )
    with get_db(args.dsn) as conn:
        workspace = query_one(
            conn,
            """
            SELECT w.*, t.neo4j_database, t.status AS tenant_status
            FROM workspaces w JOIN tenants t ON t.id = w.tenant_id
            WHERE w.id = ?
            """,
            (args.workspace_id,),
        )
    if not workspace:
        raise SystemExit(f"Workspace not found: {args.workspace_id}")
    if workspace["instance_status"] != "provisioning":
        raise RuntimeError("workspace must be claimed by workspace_provisioner first")
    if workspace["endpoint_ref"] and not resolve_env(
        workspace["endpoint_ref"]
    ).startswith("holo-age://"):
        return provision_registered(args.dsn, workspace)
    if pool_manifest is None:
        run("docker", "info", capture=True)

    slug = safe_name(args.workspace_id)
    container = f"dataaxon-ws-{slug}"
    user = "neo4j"
    password = secrets.token_urlsafe(32)
    root = Path(args.data_root).expanduser().resolve()
    secret_dir = root / ".secrets"
    secret_dir.mkdir(parents=True, exist_ok=True)
    secret_path = secret_dir / f"{slug}.json"

    if pool_manifest is not None:
        with get_db(args.dsn) as conn:
            return provision_from_pool(
                conn,
                workspace,
                workspace_id=args.workspace_id,
                manifest_path=pool_manifest,
                secret_path=secret_path,
            )

    # Docker mode owns a per-Workspace data/logs volume pair; the pool mode does
    # not, because the pool deployment already placed each instance's directories.
    workspace_root = root / slug
    data_dir = workspace_root / "neo4j-data"
    logs_dir = workspace_root / "neo4j-logs"
    for directory in (data_dir, logs_dir):
        directory.mkdir(parents=True, exist_ok=True)

    existing = (
        subprocess.run(
            ["docker", "inspect", container],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )
    started_here = False
    try:
        if not existing:
            run(
                "docker",
                "run",
                "-d",
                "--name",
                container,
                "--restart",
                "unless-stopped",
                "--label",
                f"dataaxon.workspace={args.workspace_id}",
                "-p",
                "127.0.0.1::7687",
                "-v",
                f"{data_dir}:/data",
                "-v",
                f"{logs_dir}:/logs",
                "-e",
                f"NEO4J_AUTH={user}/{password}",
                "-e",
                "NEO4J_server_http_enabled=false",
                args.image,
            )
            started_here = True
        else:
            raise RuntimeError(
                f"Container {container} already exists; refusing to rotate its credential implicitly"
            )

        wait_until_ready(container, user, password, args.timeout)
        mapping = run("docker", "port", container, "7687/tcp", capture=True)
        port = mapping.rsplit(":", 1)[-1].strip()
        if not port.isdigit():
            raise RuntimeError(f"Unexpected Docker port mapping: {mapping}")
        endpoint = f"bolt://127.0.0.1:{port}"

        secret_path.write_text(
            json.dumps({"user": user, "password": password, "database": "neo4j"}),
            encoding="utf-8",
        )
        secret_path.chmod(0o600)
        prepare_graph(
            workspace,
            endpoint,
            {"user": user, "password": password, "database": "neo4j"},
        )

        with get_db(args.dsn) as conn:
            activate_workspace(
                conn,
                workspace,
                workspace_id=args.workspace_id,
                endpoint=endpoint,
                secret_ref=f"file://{secret_path}",
                instance_version=args.image,
            )
        print(
            json.dumps(
                {
                    "workspace_id": args.workspace_id,
                    "container": container,
                    "endpoint_ref": endpoint,
                    "secret_ref": f"file://{secret_path}",
                    "status": "ready",
                },
                ensure_ascii=False,
            )
        )
        return 0
    except Exception:
        secret_path.unlink(missing_ok=True)
        if started_here:
            subprocess.run(
                ["docker", "rm", "-f", container],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        subprocess.CalledProcessError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as exc:
        print(f"provision failed: {exc}", file=sys.stderr)
        raise SystemExit(1)

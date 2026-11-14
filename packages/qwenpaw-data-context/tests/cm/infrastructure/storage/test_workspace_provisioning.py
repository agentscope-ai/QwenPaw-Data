"""Verify shared Workspace claims and explicit schema preparation."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from qwenpaw_data_auth.database import get_db, init_schema, query_one

REPO_ROOT = Path(__file__).resolve().parents[6]
SPEC = importlib.util.spec_from_file_location(
    "workspace_provisioner", REPO_ROOT / "scripts/common/workspace_provisioner.py"
)
workspace_provisioner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace_provisioner)


def queued_workspace(tmp_path: Path) -> str:
    dsn = f"sqlite:///{tmp_path / 'control-plane.db'}"
    init_schema(dsn)
    with get_db(dsn) as conn:
        conn.execute("""
            INSERT INTO tenants (id, name, neo4j_database, status, onboarding_state)
            VALUES ('tenant-a', 'Tenant A', 'neo4j', 'active', 'ready')
        """)
        conn.execute("""
            INSERT INTO workspaces (id, tenant_id, name, status, instance_status)
            VALUES ('ws-b', 'tenant-a', 'Default', 'provisioning', 'queued')
        """)
        conn.commit()
    return dsn


def test_competing_workers_claim_workspace_only_once(tmp_path: Path):
    dsn = queued_workspace(tmp_path)
    start = Barrier(2)

    def claim():
        start.wait(timeout=5)
        return workspace_provisioner.claim_next(dsn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("fail_schema", [False, True])
@pytest.mark.parametrize("override", [None, Path("custom.py")])
def test_existing_graph_uses_queue_and_only_becomes_ready_after_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_schema: bool,
    override: Path | None,
):
    monkeypatch.setenv("GRAPH_BACKEND", "holo_age")
    dsn = queued_workspace(tmp_path)
    secret_path = tmp_path / "secret.json"
    secret_path.write_text('{"user":"neo4j","password":"test","database":"neo4j"}')
    with get_db(dsn) as conn:
        conn.execute(
            """
            UPDATE workspaces SET instance_status = 'ready', status = 'active',
                endpoint_ref = 'bolt://existing:7687', secret_ref = ?, instance_version = 'neo4j'
            WHERE id = 'ws-b'
        """,
            (f"file://{secret_path}",),
        )
        conn.commit()
    assert workspace_provisioner.enqueue_initialization(dsn, "ws-b") == ["ws-b"]
    assert workspace_provisioner.enqueue_initialization(dsn, "ws-b") == []
    spec = importlib.util.spec_from_file_location(
        "provision_neo4j", REPO_ROOT / "scripts/common/provision_workspace_neo4j.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []

    def prepare(workspace, endpoint, secret):
        calls.append(endpoint)
        assert secret["database"] == "neo4j"
        with get_db(dsn) as conn:
            row = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-b'")
        assert row["status"] == row["instance_status"] == "provisioning"
        assert workspace_provisioner.enqueue_initialization(dsn, "ws-b") == []
        if fail_schema:
            raise RuntimeError("schema failed")

    monkeypatch.setattr(module, "prepare_graph", prepare)

    def runner(command, **kwargs):
        expected = override or REPO_ROOT / "scripts/common/provision_workspace_neo4j.py"
        assert Path(command[1]) == expected
        assert list(command[2:]) == ["--workspace-id", "ws-b"]
        assert kwargs["env"]["QWENPAW_DATA_DB_URL"] == dsn
        monkeypatch.setenv("QWENPAW_DATA_DB_URL", kwargs["env"]["QWENPAW_DATA_DB_URL"])
        monkeypatch.setattr(sys, "argv", list(command[1:]))
        try:
            return subprocess.CompletedProcess(command, module.main(), "", "")
        except RuntimeError as exc:
            return subprocess.CompletedProcess(command, 1, "", str(exc))

    assert workspace_provisioner.process_one(dsn, override, runner)
    assert calls == ["bolt://existing:7687"]
    with get_db(dsn) as conn:
        row = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-b'")
    assert row["instance_status"] == ("failed" if fail_schema else "ready")
    assert row["secret_ref"] == f"file://{secret_path}"
    if fail_schema:
        assert workspace_provisioner.enqueue_initialization(dsn, "ws-b") == ["ws-b"]


def test_retry_as_neo4j_does_not_reuse_failed_holo_route(tmp_path, monkeypatch):
    dsn = queued_workspace(tmp_path)
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE workspaces SET endpoint_ref = 'holo-age://old_graph' WHERE id = 'ws-b'"
        )
        conn.commit()
    assert workspace_provisioner.claim_next(dsn) is not None
    spec = importlib.util.spec_from_file_location(
        "provision_neo4j_retry",
        REPO_ROOT / "scripts/common/provision_workspace_neo4j.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    allocated = []

    def allocate(conn, workspace, **kwargs):
        allocated.append(workspace["id"])
        return 0

    monkeypatch.setattr(module, "provision_from_pool", allocate)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "provision_workspace_neo4j.py",
            "--workspace-id",
            "ws-b",
            "--dsn",
            dsn,
            "--from-pool",
            str(tmp_path / "pool.json"),
            "--data-root",
            str(tmp_path / "data"),
        ],
    )
    assert module.main() == 0
    assert allocated == ["ws-b"]

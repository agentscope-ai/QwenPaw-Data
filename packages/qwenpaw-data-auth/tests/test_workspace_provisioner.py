from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest
from qwenpaw_data_auth.database import get_db, init_schema, query_one


REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "workspace_provisioner",
    REPO_ROOT / "scripts" / "common" / "workspace_provisioner.py",
)
assert SPEC and SPEC.loader
workspace_provisioner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workspace_provisioner)


def queued_workspace(tmp_path: Path) -> str:
    dsn = f"sqlite:///{tmp_path / 'control-plane.db'}"
    init_schema(dsn)
    with get_db(dsn) as conn:
        conn.execute(
            """
            INSERT INTO tenants
                (id, name, neo4j_database, status, onboarding_state)
            VALUES ('tenant-a', 'Tenant A', 'tenant_a', 'provisioning', 'pending')
            """
        )
        conn.execute(
            """
            INSERT INTO workspaces
                (id, tenant_id, name, status, instance_status)
            VALUES ('ws-a', 'tenant-a', 'Default', 'provisioning', 'queued')
            """
        )
        conn.commit()
    return dsn


def queued_workspace_on_active_tenant(tmp_path: Path) -> str:
    """An extra workspace queued by the tenant tenant_admin after onboarding."""
    dsn = f"sqlite:///{tmp_path / 'control-plane.db'}"
    init_schema(dsn)
    with get_db(dsn) as conn:
        conn.execute(
            """
            INSERT INTO tenants
                (id, name, neo4j_database, status, onboarding_state)
            VALUES ('tenant-a', 'Tenant A', 'tenant_a', 'active', 'ready')
            """
        )
        conn.execute(
            """
            INSERT INTO workspaces
                (id, tenant_id, name, status, instance_status)
            VALUES ('ws-b', 'tenant-a', 'Analytics', 'provisioning', 'queued')
            """
        )
        conn.commit()
    return dsn


def test_worker_claims_and_completes_queued_workspace(tmp_path: Path):
    dsn = queued_workspace(tmp_path)

    def successful_runner(*_args, **_kwargs):
        with get_db(dsn) as conn:
            conn.execute(
                """
                UPDATE workspaces
                SET status = 'active', instance_status = 'ready'
                WHERE id = 'ws-a'
                """
            )
            conn.execute(
                """
                UPDATE tenants
                SET status = 'active', onboarding_state = 'ready'
                WHERE id = 'tenant-a'
                """
            )
            conn.commit()
        return subprocess.CompletedProcess([], 0, '{"status":"ready"}', "")

    assert workspace_provisioner.process_one(
        dsn, Path("provision.py"), successful_runner
    )
    with get_db(dsn) as conn:
        workspace = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-a'")
    assert workspace["instance_status"] == "ready"
    assert not workspace_provisioner.process_one(
        dsn, Path("provision.py"), successful_runner
    )


def test_worker_records_failure_and_allows_retry(tmp_path: Path):
    dsn = queued_workspace(tmp_path)

    def failed_runner(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "docker failed")

    assert workspace_provisioner.process_one(dsn, Path("provision.py"), failed_runner)
    with get_db(dsn) as conn:
        workspace = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-a'")
        tenant = query_one(conn, "SELECT * FROM tenants WHERE id = 'tenant-a'")
    assert workspace["instance_status"] == "failed"
    assert tenant["onboarding_state"] == "failed"
    assert tenant["onboarding_error"] == "docker failed"


def test_worker_claims_workspace_of_active_tenant(tmp_path: Path):
    """tenant_admin 在已激活租户下新建的 workspace 也能被领取拉起。"""
    dsn = queued_workspace_on_active_tenant(tmp_path)

    def successful_runner(*_args, **_kwargs):
        with get_db(dsn) as conn:
            conn.execute(
                """
                UPDATE workspaces
                SET status = 'active', instance_status = 'ready'
                WHERE id = 'ws-b'
                """
            )
            conn.commit()
        return subprocess.CompletedProcess([], 0, '{"status":"ready"}', "")

    assert workspace_provisioner.process_one(
        dsn, Path("provision.py"), successful_runner
    )
    with get_db(dsn) as conn:
        workspace = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-b'")
    assert workspace["instance_status"] == "ready"


@pytest.mark.parametrize("holo_backend", ["holo_age", "holoage", " HOLO-AGE "])
def test_default_provision_script_follows_graph_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, holo_backend: str
):
    script_dir = REPO_ROOT / "scripts" / "common"
    monkeypatch.setenv("GRAPH_BACKEND", "neo4j")
    assert (
        workspace_provisioner._default_provision_script(script_dir).name
        == "provision_workspace_neo4j.py"
    )
    monkeypatch.setenv("GRAPH_BACKEND", holo_backend)
    # The Holo AGE provisioning script is not part of the open-source tree.
    with pytest.raises(RuntimeError, match="not included"):
        workspace_provisioner._default_provision_script(script_dir)
    monkeypatch.setenv("GRAPH_BACKEND", "mysql")
    with pytest.raises(ValueError, match="holo_age"):
        workspace_provisioner.graph_backend()


def test_worker_dispatches_holo_script_when_queued_as_holo(tmp_path: Path):
    dsn = queued_workspace(tmp_path)
    with get_db(dsn) as conn:
        conn.execute(
            "UPDATE workspaces SET instance_version = 'holo_age' WHERE id = 'ws-a'"
        )
        conn.commit()
    seen: list[str] = []

    def capturing_runner(command, **_kwargs):
        seen.append(str(command[1]))
        with get_db(dsn) as conn:
            conn.execute(
                """
                UPDATE workspaces
                SET status = 'active', instance_status = 'ready'
                WHERE id = 'ws-a'
                """
            )
            conn.commit()
        return subprocess.CompletedProcess(command, 0, '{"status":"ready"}', "")

    assert workspace_provisioner.process_one(
        dsn,
        Path("provision_neo4j.py"),
        capturing_runner,
        holo_script=Path("provision_holo.py"),
    )
    assert seen == ["provision_holo.py"]


def test_failed_extra_workspace_does_not_demote_active_tenant(tmp_path: Path):
    dsn = queued_workspace_on_active_tenant(tmp_path)

    def failed_runner(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "", "docker failed")

    assert workspace_provisioner.process_one(dsn, Path("provision.py"), failed_runner)
    with get_db(dsn) as conn:
        workspace = query_one(conn, "SELECT * FROM workspaces WHERE id = 'ws-b'")
        tenant = query_one(conn, "SELECT * FROM tenants WHERE id = 'tenant-a'")
    assert workspace["instance_status"] == "failed"
    # 已激活租户不被额外 workspace 的失败拖下线
    assert tenant["status"] == "active"
    assert tenant["onboarding_state"] == "ready"
    assert tenant["onboarding_error"] is None

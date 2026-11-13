"""Portable local lifecycle helper regression tests."""

from __future__ import annotations

import importlib.util
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_common() -> ModuleType:
    path = ROOT / "scripts" / "_local_common.py"
    spec = importlib.util.spec_from_file_location("qwenpaw_data_local_common_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_neo4j_provisioner() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "qpd_neo4j_provisioner_test", ROOT / "scripts/common/provision_workspace_neo4j.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repository_root_is_resolved() -> None:
    common = _load_common()
    assert common.repository_root() == ROOT
    assert common.repository_root().is_absolute()


def test_load_environment_preserves_explicit_values(tmp_path, monkeypatch) -> None:
    common = _load_common()
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nDEMO_ONE=from-file\nDEMO_TWO='quoted value'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEMO_ONE", "from-process")
    monkeypatch.delenv("DEMO_TWO", raising=False)

    common.load_environment(env_file)

    assert os.environ["DEMO_ONE"] == "from-process"
    assert os.environ["DEMO_TWO"] == "quoted value"


def test_venv_executable_uses_platform_layout(tmp_path, monkeypatch) -> None:
    common = _load_common()
    monkeypatch.setattr(common.os, "name", "nt")
    assert (
        common.venv_executable(tmp_path, "python")
        == tmp_path / "Scripts" / "python.exe"
    )
    assert common.venv_executable(tmp_path, "npm") == tmp_path / "Scripts" / "npm.cmd"


def test_lifecycle_entrypoints_offer_help() -> None:
    for name in ("init_local.py", "start_local.py"):
        source = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        compile(source, name, "exec")


def test_start_entrypoint_has_explicit_neo4j_escape_hatch() -> None:
    source = (ROOT / "scripts" / "start_local.py").read_text(encoding="utf-8")

    assert '"--skip-neo4j"' in source
    assert 'backend == "neo4j" and not args.skip_neo4j' in source


def test_managed_neo4j_launchers_disable_anonymous_usage_reporting() -> None:
    compose = (ROOT / "packages/qwenpaw-data-context/docker-compose.yml").read_text()
    provisioner = (ROOT / "scripts/common/provision_workspace_neo4j.py").read_text()
    assert 'NEO4J_dbms_usage__report_enabled: "false"' in compose
    assert '"NEO4J_dbms_usage__report_enabled=false"' in provisioner
    assert "NEO4J_PLUGINS: '[\"apoc\"]'" in compose
    assert "'NEO4J_PLUGINS=[\"apoc\"]'" in provisioner


@pytest.mark.parametrize("capture", [False, True])
def test_provisioner_does_not_leak_failed_command_secrets(capsys, capture):
    module = _load_neo4j_provisioner()
    secret = "test-secret-not-for-logs"
    with pytest.raises(RuntimeError, match="failed \\(exit 7\\)") as caught:
        module.run(
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1], file=sys.stderr); sys.exit(7)",
            secret,
            capture=capture,
        )
    assert secret not in str(caught.value)
    assert secret not in capsys.readouterr().err


def test_workspace_secret_creation_is_private_and_refuses_existing_files(tmp_path):
    import json
    import stat

    module = _load_neo4j_provisioner()
    target = tmp_path / "workspace.json"
    secret = {"user": "neo4j", "password": "test-secret", "database": "neo4j"}
    module.write_workspace_secret(target, secret)
    assert json.loads(target.read_text()) == secret
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        module.write_workspace_secret(target, {"password": "replacement"})
    assert json.loads(target.read_text()) == secret
    if os.name != "nt":
        link = tmp_path / "secret-link"
        link.symlink_to(target)
        with pytest.raises(RuntimeError, match="already exists"):
            module.write_workspace_secret(link, {"password": "replacement"})
        assert json.loads(target.read_text()) == secret


def test_pool_failure_preserves_existing_workspace_secret(tmp_path, monkeypatch):
    module = _load_neo4j_provisioner()
    target = tmp_path / "workspace.json"
    target.write_text("existing credential")
    releases = []
    monkeypatch.setattr(module, "claim_pool_instance", lambda *args: ({"port": 7687, "password": "new-secret"}, True))
    monkeypatch.setattr(module, "release_pool_instance", lambda *args: releases.append(args))

    class Connection:
        def rollback(self):
            pass

    with pytest.raises(RuntimeError, match="already exists"):
        module.provision_from_pool(
            Connection(), {}, workspace_id="workspace", manifest_path=tmp_path / "pool.json", secret_path=target
        )
    assert target.read_text() == "existing credential"
    assert len(releases) == 1


@pytest.mark.parametrize("preexisting_secret", [False, True])
def test_pool_retry_preserves_preexisting_claim(tmp_path, monkeypatch, preexisting_secret):
    import json

    module = _load_neo4j_provisioner()
    manifest = tmp_path / "pool.json"
    module.write_workspace_secret(manifest, {"instances": [{
        "port": 7687, "password": "test-secret", "claimed_by": "workspace"
    }]})
    secret = tmp_path / "workspace.json"
    if preexisting_secret:
        module.write_workspace_secret(secret, {"password": "existing"})
    monkeypatch.setattr(module, "prepare_graph", lambda *args: (_ for _ in ()).throw(RuntimeError("graph unavailable")))

    class Connection:
        def rollback(self):
            pass

    with pytest.raises(RuntimeError):
        module.provision_from_pool(
            Connection(), {}, workspace_id="workspace", manifest_path=manifest, secret_path=secret
        )
    assert json.loads(manifest.read_text())["instances"][0]["claimed_by"] == "workspace"
    if preexisting_secret:
        assert json.loads(secret.read_text())["password"] == "existing"
    else:
        assert not secret.exists()


@pytest.mark.parametrize("graph_fails", [False, True])
def test_pool_retry_reuses_only_matching_secret(tmp_path, monkeypatch, graph_fails):
    import json

    module = _load_neo4j_provisioner()
    manifest = tmp_path / "pool.json"
    saved = {"user": "neo4j", "password": "test-secret", "database": "neo4j"}
    module.write_workspace_secret(manifest, {"instances": [{
        "port": 7687, "password": saved["password"], "claimed_by": "workspace"
    }]})
    secret = tmp_path / "workspace.json"
    module.write_workspace_secret(secret, saved)
    original = secret.stat()
    activated = []

    def prepare(*args):
        if graph_fails:
            raise RuntimeError("graph unavailable")

    monkeypatch.setattr(module, "prepare_graph", prepare)
    monkeypatch.setattr(module, "activate_workspace", lambda *args, **kwargs: activated.append(True))

    class Connection:
        def rollback(self):
            pass

    if graph_fails:
        with pytest.raises(RuntimeError, match="graph unavailable"):
            module.provision_from_pool(
                Connection(), {}, workspace_id="workspace", manifest_path=manifest, secret_path=secret
            )
    else:
        assert module.provision_from_pool(
            Connection(), {}, workspace_id="workspace", manifest_path=manifest, secret_path=secret
        ) == 0
    assert bool(activated) is not graph_fails
    assert json.loads(secret.read_text()) == saved
    assert secret.stat().st_ino == original.st_ino
    assert json.loads(manifest.read_text())["instances"][0]["claimed_by"] == "workspace"
    if os.name != "nt":
        link = tmp_path / "linked-secret"
        link.symlink_to(secret)
        with pytest.raises(module.WorkspaceSecretExists):
            module.ensure_pool_secret(link, saved)
        assert json.loads(secret.read_text()) == saved


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX named pipes")
def test_pool_secret_rejects_fifo_without_blocking(tmp_path):
    import stat

    fifo = tmp_path / "workspace.json"
    os.mkfifo(fifo)
    source = ROOT / "scripts/common/provision_workspace_neo4j.py"
    code = (
        "import importlib.util, pathlib, sys; "
        "spec = importlib.util.spec_from_file_location('provision', sys.argv[1]); "
        "module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module); "
        "module.ensure_pool_secret(pathlib.Path(sys.argv[2]), {'password': 'test'})"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(source), str(fifo)],
        text=True, capture_output=True, timeout=5, check=False,
    )
    assert result.returncode != 0
    assert "cannot be safely reused" in result.stderr
    assert stat.S_ISFIFO(fifo.stat().st_mode)


@pytest.mark.skipif(
    not os.getenv("CM_MCP_NEO4J_URI"), reason="requires disposable local Neo4j"
)
@pytest.mark.parametrize("valid_credentials", [True, False])
@pytest.mark.parametrize("preexisting", [False, True])
def test_pool_provisioning_with_real_sqlite_and_neo4j(tmp_path, capsys, valid_credentials, preexisting):
    import json
    import stat
    from urllib.parse import urlparse

    from context_manager.store import StoreError
    from neo4j.exceptions import AuthError
    from qwenpaw_data_auth.database import get_db, init_schema, query_one

    module = _load_neo4j_provisioner()
    uri = urlparse(os.environ["CM_MCP_NEO4J_URI"])
    if uri.scheme != "bolt" or uri.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("a disposable direct loopback Bolt URI is required")
    password = os.environ["CM_MCP_NEO4J_PASSWORD"]
    pool_password = password if valid_credentials else "incorrect-test-password"
    dsn = f"sqlite:///{tmp_path / 'control.db'}"
    init_schema(dsn)
    manifest = tmp_path / "pool.json"
    module.write_workspace_secret(manifest, {"instances": [{
        "port": uri.port or 7687,
        "user": "neo4j",
        "password": pool_password,
        "database": "neo4j",
        "claimed_by": "ws-a" if preexisting else None,
    }]})
    secret_path = tmp_path / "workspace.json"
    if preexisting:
        module.write_workspace_secret(secret_path, {"user": "neo4j", "password": pool_password, "database": "neo4j"})
    with get_db(dsn) as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, neo4j_database, status, onboarding_state) "
            "VALUES ('tenant-a', 'Tenant A', 'neo4j', 'provisioning', 'pending')"
        )
        conn.execute(
            "INSERT INTO workspaces (id, tenant_id, name, status, instance_status) "
            "VALUES ('ws-a', 'tenant-a', 'Workspace', 'provisioning', 'provisioning')"
        )
        conn.commit()
        workspace = {"id": "ws-a", "tenant_id": "tenant-a"}
        if valid_credentials:
            assert module.provision_from_pool(
                conn, workspace, workspace_id="ws-a", manifest_path=manifest, secret_path=secret_path
            ) == 0
        else:
            with pytest.raises(StoreError) as caught:
                module.provision_from_pool(
                    conn, workspace, workspace_id="ws-a", manifest_path=manifest, secret_path=secret_path
                )
            assert caught.value.code == "permission_denied"
            assert isinstance(caught.value.__cause__, AuthError)
        row = query_one(conn, "SELECT status, instance_status, secret_ref FROM workspaces WHERE id = ?", ("ws-a",))
    claimed = json.loads(manifest.read_text())["instances"][0].get("claimed_by")
    if valid_credentials:
        assert (row["status"], row["instance_status"], claimed) == ("active", "ready", "ws-a")
        assert row["secret_ref"] == f"file://{secret_path}"
        assert json.loads(secret_path.read_text())["password"] == password
        if os.name != "nt":
            assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
        assert password not in capsys.readouterr().out
    else:
        assert (row["status"], row["instance_status"], claimed) == (
            "provisioning", "provisioning", "ws-a" if preexisting else None
        )
        if preexisting:
            assert json.loads(secret_path.read_text())["password"] == pool_password
        else:
            assert not secret_path.exists()


@pytest.mark.parametrize(("backend", "expected_neo4j"), [
    (None, True), ("holo_age", False), ("holoage", False), (" HOLO-AGE ", False),
])
def test_cross_platform_launcher_selects_graph(tmp_path, monkeypatch, backend, expected_neo4j):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("qpd_start_local_test", ROOT / "scripts/start_local.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if backend is None:
        monkeypatch.delenv("GRAPH_BACKEND", raising=False)
    else:
        monkeypatch.setenv("GRAPH_BACKEND", backend)
    for name in ("HOLO_AGE_HOST", "HOLO_AGE_USER", "HOLO_AGE_PASSWORD", "HOLO_AGE_DATABASE"):
        if backend is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, "test")
    monkeypatch.setenv("CONTEXT_PYTHON", sys.executable)
    monkeypatch.setattr(module, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(module, "ensure_environment", lambda root: tmp_path / ".env")
    monkeypatch.setattr(module, "port_reachable", lambda *args: False)
    neo4j = []
    monkeypatch.setattr(module, "start_neo4j", lambda *args: neo4j.append(True))
    monkeypatch.setattr(module, "terminate_process_tree", lambda process: None)
    monkeypatch.setattr(module.signal, "signal", lambda *args: None)

    class ExitedChild:
        def poll(self):
            return 1

    monkeypatch.setattr(module, "start_child", lambda *args, **kwargs: ExitedChild())
    assert module.main(["--skip-frontend"]) == 1
    assert bool(neo4j) is expected_neo4j


def test_cross_platform_launcher_rejects_invalid_graph_backend(tmp_path, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("qpd_start_local_invalid_test", ROOT / "scripts/start_local.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("GRAPH_BACKEND", "unknown")
    monkeypatch.setattr(module, "repository_root", lambda: tmp_path)
    monkeypatch.setattr(module, "ensure_environment", lambda root: tmp_path / ".env")
    assert module.main(["--skip-frontend"]) == 1
    assert "GRAPH_BACKEND" in capsys.readouterr().err


def test_powershell_entrypoints_are_present() -> None:
    for name in ("init_local.ps1", "start_local.ps1"):
        path = ROOT / "scripts" / name
        assert path.is_file()
        assert "@args" in path.read_text(encoding="utf-8")


@pytest.mark.skipif(shutil.which("lsof") is None, reason="requires lsof")
def test_start_local_does_not_stop_existing_listener(tmp_path: Path) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result = subprocess.run(
            [
                "bash",
                str(ROOT / "scripts" / "start_local.sh"),
                "--context-port",
                str(port),
                "--skip-frontend",
            ],
            env={**os.environ, "QWENPAW_DATA_ENV_FILE": str(tmp_path / "missing.env")},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 1
        assert f"port {port} is already in use" in result.stderr
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass


@pytest.mark.skipif(os.name == "nt", reason="shell launcher test requires POSIX")
@pytest.mark.parametrize(("backend", "skip_neo4j"), [
    ("holo_age", False), ("holoage", False), (" HOLO-AGE ", False), ("neo4j", True),
])
def test_databridge_external_graph_does_not_start_neo4j(tmp_path: Path, backend: str, skip_neo4j: bool) -> None:
    root = tmp_path / "repo"
    scripts = root / "scripts"
    (scripts / "common").mkdir(parents=True)
    (root / "packages/qwenpaw-data-context").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/start_databridge.sh", scripts / "start_databridge.sh")
    shutil.copyfile(ROOT / "scripts/common/env.sh", scripts / "common/env.sh")
    marker = tmp_path / "api-started"
    docker_marker = tmp_path / "docker-called"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python = bin_dir / "python"
    python.write_text('#!/bin/bash\ntouch "$QPD_TEST_API_MARKER"\nwhile :; do sleep 1; done\n')
    python.chmod(0o755)
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/bash\ntouch "$QPD_TEST_DOCKER_MARKER"\nexit 1\n')
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "QWENPAW_DATA_ENV_FILE": str(tmp_path / "missing.env"),
        "CONTEXT_PYTHON": str(python),
        "GRAPH_BACKEND": backend,
        "HOLO_AGE_HOST": "127.0.0.1",
        "HOLO_AGE_USER": "test",
        "HOLO_AGE_PASSWORD": "test-only",
        "HOLO_AGE_DATABASE": "test",
        "QPD_TEST_API_MARKER": str(marker),
        "QPD_TEST_DOCKER_MARKER": str(docker_marker),
    }
    args = ["bash", str(scripts / "start_databridge.sh"), "--skip-frontend"]
    if skip_neo4j:
        args.append("--skip-neo4j")
    process = subprocess.Popen(
        args,
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not marker.exists() and process.poll() is None:
            time.sleep(0.05)
        assert marker.exists()
        assert process.poll() is None
        assert not docker_marker.exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)


@pytest.mark.parametrize(("host_fails", "frontend_fails", "missing_core", "skip_neo4j"), [
    (False, False, False, False), (True, False, False, False),
    (False, True, False, False), (False, False, True, False), (False, False, False, True),
])
@pytest.mark.skipif(os.name == "nt", reason="shell launcher test requires POSIX")
def test_start_all_waits_for_host_before_context(tmp_path: Path, host_fails: bool, frontend_fails: bool, missing_core: bool, skip_neo4j: bool) -> None:
    root = tmp_path / "repo"
    marker = tmp_path / "markers"
    marker.mkdir()

    def executable(path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")
        path.chmod(0o755)

    scripts = root / "scripts"
    (scripts / "common").mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts" / "start_all.sh", scripts / "start_all.sh")
    shutil.copyfile(ROOT / "scripts" / "common" / "env.sh", scripts / "common" / "env.sh")
    executable(
        scripts / "start_local.sh",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" > "$QPD_TEST_MARKERS/context-args"\n'
        'if [[ ! -f "$QPD_TEST_MARKERS/host-ready" ]]; then\n'
        '  touch "$QPD_TEST_MARKERS/context-too-early"; exit 1\n'
        "fi\n"
        'touch "$QPD_TEST_MARKERS/context-ready"\n'
        "while :; do sleep 1; done\n",
    )
    python = (
        "#!/bin/bash\n"
        'if [[ "${1:-}" == "-c" ]]; then\n'
        '  if [[ "$QPD_TEST_MISSING_CORE" == 1 && "${2:-}" == *sqlalchemy* ]]; then exit 1; fi\n'
        '  exit 0\n'
        'fi\n'
        'case "${3:-}" in\n'
        "  qwenpaw_data.host.core.api.app:app)\n"
        '    if [[ "$QPD_TEST_HOST_FAILS" == 1 ]]; then exit 2; fi\n'
        '    sleep 0.3; touch "$QPD_TEST_MARKERS/host-ready" ;;\n'
        '  qwenpaw_data_auth.server:app) touch "$QPD_TEST_MARKERS/auth-ready" ;;\n'
        "esac\n"
        "while :; do sleep 1; done\n"
    )
    executable(root / ".venv" / "bin" / "python", python)
    executable(root / "packages" / "qwenpaw-data-context" / ".venv" / "bin" / "python", python)
    vite = ('#!/bin/bash\n'
            'if [[ "$QPD_TEST_FRONTEND_FAILS" == 1 ]]; then exit 2; fi\n'
            'touch "$QPD_TEST_MARKERS/frontend-ready"\nwhile :; do sleep 1; done\n')
    for package in ("qwenpaw-data-context", "qwenpaw-data-console"):
        executable(root / "packages" / package / "frontend" / "node_modules" / ".bin" / "vite", vite)

    bin_dir = tmp_path / "bin"
    executable(bin_dir / "uv", "#!/bin/bash\nexit 0\n")
    executable(bin_dir / "docker", '#!/bin/bash\ntouch "$QPD_TEST_MARKERS/docker-called"\nexit 0\n')
    executable(bin_dir / "lsof", "#!/bin/bash\nexit 1\n")
    executable(
        bin_dir / "curl",
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$QPD_TEST_MARKERS/curl-args"\n'
        'case "${*: -1}" in\n'
        '  *:8089/*) test -f "$QPD_TEST_MARKERS/host-ready" ;;\n'
        '  *:8088/*) test -f "$QPD_TEST_MARKERS/auth-ready" ;;\n'
        '  *:8765/*) test -f "$QPD_TEST_MARKERS/context-ready" ;;\n'
        '  *:3000|*:3100) test -f "$QPD_TEST_MARKERS/frontend-ready" ;;\n'
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    env_file = tmp_path / "stack.env"
    env_file.write_text("QWENPAW_DATA_ENV=production\n", encoding="utf-8")
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "QWENPAW_DATA_ENV_FILE": str(env_file),
        "QPD_TEST_MARKERS": str(marker),
        "QPD_TEST_HOST_FAILS": str(int(host_fails)),
        "QPD_TEST_FRONTEND_FAILS": str(int(frontend_fails)),
        "QPD_TEST_MISSING_CORE": str(int(missing_core)),
        "UV": "qpd-nonexistent-uv",
    }
    args = ["bash", str(scripts / "start_all.sh"), "--skip-install"]
    if skip_neo4j:
        args.append("--skip-neo4j")
    process = subprocess.Popen(
        args,
        cwd=root,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None or (
                not frontend_fails and (marker / "context-ready").exists()
            ):
                break
            time.sleep(0.05)
        assert not (marker / "context-too-early").exists()
        if missing_core:
            assert process.wait(timeout=3) != 0
            assert not (marker / "host-ready").exists()
            assert not (marker / "context-ready").exists()
        elif host_fails:
            assert "--max-time 3" in (marker / "curl-args").read_text()
            assert process.wait(timeout=3) != 0
            assert not (marker / "context-ready").exists()
        elif frontend_fails:
            assert "--max-time 3" in (marker / "curl-args").read_text()
            assert process.wait(timeout=3) != 0
            assert (marker / "context-ready").exists()
        else:
            assert "--max-time 3" in (marker / "curl-args").read_text()
            assert (marker / "host-ready").exists()
            assert (marker / "context-ready").exists()
            assert process.poll() is None
            if skip_neo4j:
                assert "--skip-neo4j" in (marker / "context-args").read_text()
                assert not (marker / "docker-called").exists()
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)

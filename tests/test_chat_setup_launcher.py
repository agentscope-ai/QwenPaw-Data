"""Exercise the shell entrypoint without databases, network or real accounts."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest


def executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)


@pytest.fixture
def stack(tmp_path):
    source = Path(__file__).resolve().parents[1]
    for name in (
        "scripts/start.sh",
        "scripts/setup_chat.sh",
        "scripts/chat_setup/fixtures/config.example.json",
        "scripts/common/env.sh",
        "scripts/common/service_manifest.py",
    ):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
    # Shorten the wait only in the isolated copy, without a production env knob.
    launcher = tmp_path / "scripts/start.sh"
    launcher.write_text(
        launcher.read_text().replace(
            "startup_timeout=300", 'startup_timeout="${FIXTURE_START_TIMEOUT:-10}"'
        )
    )
    # Stand-ins keep the real launcher's process ownership and readiness logic.
    stub = tmp_path / "bin/python-stub"
    executable(
        stub,
        f"#!{sys.executable}\n"
        "import os, pathlib, signal, sys, time, json\n"
        "root = pathlib.Path(os.environ['FIXTURE_ROOT'])\n"
        "args = sys.argv[1:]\n"
        "if '-c' in args: sys.exit(0)\n"
        "if args and args[0].endswith('service_manifest.py'):\n"
        f"    os.execv({sys.executable!r}, [{sys.executable!r}] + args)\n"
        "if args and args[0].endswith('chat_setup/verify.py'):\n"
        "    (root / 'setup-called').write_text('yes')\n"
        "    sys.exit(int(os.environ.get('FIXTURE_SETUP_EXIT', '0')))\n"
        "marker = root / ('worker-' + str(os.getpid()))\n"
        "marker.write_text(json.dumps(args))\n"
        "signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))\n"
        "try:\n"
        "    while True: time.sleep(.05)\n"
        "finally: marker.unlink(missing_ok=True)\n",
    )
    executable(
        tmp_path / "bin/curl",
        f"#!{sys.executable}\n"
        "import os, pathlib, sys, json\n"
        "root = pathlib.Path(os.environ['FIXTURE_ROOT'])\n"
        "ready = (root / 'http-ready').exists() or len(list(root.glob('worker-*'))) >= int(os.environ.get('FIXTURE_MIN_WORKERS', '4'))\n"
        "commands = [json.loads(p.read_text()) for p in root.glob('worker-*') if p.stat().st_size]\n"
        "if sys.argv[-1].endswith('/api/health') and ':8088/' in sys.argv[-1]: ready |= any('qwenpaw_data_auth.server:app' in args for args in commands)\n"
        "if sys.argv[-1].endswith('/api/auth/status'): ready |= any('qwenpaw_data.host.core.api.app:app' in args for args in commands)\n"
        "sys.exit(0 if ready else 7)\n",
    )
    # Stub configuration at the launcher's shell callback boundary; the separate
    # setup integration tests exercise real Bash orchestration over HTTP.
    setup_entry = tmp_path / "scripts/setup_chat.sh"
    with setup_entry.open("a") as handle:
        handle.write('\nchat_setup_configure() { "$1" "chat_setup/verify.py"; }\n')
    for name in ("uv", "npm"):
        executable(tmp_path / f"bin/{name}", "#!/bin/bash\nexit 0\n")
    executable(
        tmp_path / "bin/lsof",
        '#!/bin/bash\nprintf "%s\\n" "$*" >>"$FIXTURE_ROOT/ports"\nexit 1\n',
    )
    executable(
        tmp_path / "scripts/start_context.sh",
        '#!/bin/bash\nexec "$QWENPAW_DATA_PYTHON" context\n',
    )
    for package in ("qwenpaw-data-context", "qwenpaw-data-console"):
        frontend = tmp_path / f"packages/{package}/frontend"
        executable(frontend / "node_modules/.bin/vite", stub.read_text())
        (frontend / "package.json").write_text("{}")
    (tmp_path / ".env").write_text(
        "QWENPAW_DATA_AUTH_MODE=multi-user\n"
        f"QWENPAW_DATA_PYTHON='{stub}'\n"
        "GRAPH_BACKEND=holo_age\n"
        "HOLO_AGE_HOST=example.invalid\n"
        "HOLO_AGE_DATABASE=example\n"
        "HOLO_AGE_USER=test\n"
        "HOLO_AGE_PASSWORD=test\n"
    )
    env = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "FIXTURE_ROOT": str(tmp_path),
    }
    processes = []

    def start(service=None, **extra):
        command = ["bash", str(tmp_path / "scripts/setup_chat.sh")]
        if service is not None:
            command = ["bash", str(tmp_path / "scripts/start.sh"), "--service", service]
        proc = subprocess.Popen(
            command,
            cwd=tmp_path,
            env=env | extra,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        processes.append(proc)
        return proc

    yield tmp_path, start
    for proc in processes:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()


def wait_for(path: Path) -> None:
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() > deadline:
            pytest.fail(f"Timed out waiting for {path.name}")
        time.sleep(0.05)


def test_invalid_config_exits_before_starting_services(stack):
    root, start = stack
    (root / ".env.chat-setup.json").write_text("[]")
    proc = start()
    output, _ = proc.communicate(timeout=10)
    assert proc.returncode != 0
    assert "Expected a JSON object" in output
    assert not (root / ".env.services.lock").exists()
    assert not list(root.glob("worker-*"))


def test_reuses_running_services_and_leaves_owner_alone(stack):
    root, start = stack
    (root / "http-ready").touch()
    proc = start()
    output, _ = proc.communicate(timeout=8)
    assert proc.returncode == 0, output
    assert (root / "setup-called").exists()
    assert not list(root.glob("worker-*"))
    assert "复用的服务继续由原启动终端管理" in output


def test_waits_for_launcher_handoff_even_when_endpoints_are_healthy(stack):
    root, start = stack
    lock = root / ".env.services.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    (root / "http-ready").touch()
    proc = start()
    time.sleep(0.5)
    assert not (root / "setup-called").exists()
    (lock / "ready").touch()
    output, _ = proc.communicate(timeout=8)
    assert proc.returncode == 0, output
    assert (root / "setup-called").exists()
    assert (lock / "pid").read_text() == str(os.getpid())


def test_timeout_does_not_initialize_or_stop_existing_launcher(stack):
    root, start = stack
    lock = root / ".env.services.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()))
    proc = start(FIXTURE_START_TIMEOUT="2")
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode == 1, output
    assert "尚未执行聊天初始化" in output
    assert not (root / "setup-called").exists()
    assert (lock / "pid").exists()


def test_cold_start_initializes_then_hosts_services_until_stopped(stack):
    root, start = stack
    proc = start()
    wait_for(root / "setup-called")
    assert (root / ".env.services.json").exists()
    assert (root / ".env.services.lock/ready").exists()
    assert proc.poll() is None
    proc.terminate()
    output, _ = proc.communicate(timeout=8)
    assert "全部服务已就绪" in output
    assert not (root / ".env.services.lock").exists()
    assert not list(root.glob("worker-*"))


@pytest.mark.parametrize("reuse", [False, True])
def test_setup_failure_preserves_exit_code_and_cleans_only_owned_services(stack, reuse):
    root, start = stack
    if reuse:
        (root / "http-ready").touch()
    proc = start(FIXTURE_SETUP_EXIT="7")
    output, _ = proc.communicate(timeout=30)
    assert proc.returncode == 7, output
    assert (root / "setup-called").exists()
    assert not (root / ".env.services.lock").exists()
    assert not list(root.glob("worker-*"))
    assert "聊天配置完成" not in output


@pytest.mark.parametrize("service,count", [("cm", 3), ("host", 2)])
def test_start_selected_service_owns_only_its_processes(stack, service, count):
    root, start = stack
    proc = start(service=service, FIXTURE_MIN_WORKERS="2")
    wait_for(root / f".env.{service}.services.lock/ready")
    markers = list(root.glob("worker-*"))
    assert len(markers) == count
    commands = [json.loads(path.read_text()) for path in markers]
    is_host = any("qwenpaw_data.host.core.api.app:app" in args for args in commands)
    is_auth = any("qwenpaw_data_auth.server:app" in args for args in commands)
    assert is_host == (service == "host")
    assert is_auth == (service == "cm")
    assert not (root / "setup-called").exists()
    host_fingerprint = (
        root
        / "packages/qwenpaw-data-console/frontend/node_modules/.qwenpaw-data-dependency-fingerprint"
    )
    context_fingerprint = (
        root
        / "packages/qwenpaw-data-context/frontend/node_modules/.qwenpaw-data-dependency-fingerprint"
    )
    assert context_fingerprint.exists()
    assert host_fingerprint.exists() == (service == "host")
    checked_ports = (root / "ports").read_text()
    assert ("8088" in checked_ports) == (service == "cm")
    assert ("8089" in checked_ports) == (service == "host")
    proc.terminate()
    output, _ = proc.communicate(timeout=15)
    assert proc.returncode == 143, output
    assert not list(root.glob("worker-*"))
    assert not (root / f".env.{service}.services.lock").exists()
    assert not (root / f".env.{service}.services.json").exists()


def test_separate_services_coexist_and_setup_reuses_both(stack):
    root, start = stack
    cm = start(service="cm", FIXTURE_MIN_WORKERS="2")
    host = start(service="host", FIXTURE_MIN_WORKERS="2")
    wait_for(root / ".env.cm.services.lock/ready")
    wait_for(root / ".env.host.services.lock/ready")
    assert len(list(root.glob("worker-*"))) == 5
    for selection in ("all", "host"):
        duplicate = start(service=selection, FIXTURE_MIN_WORKERS="2")
        output, _ = duplicate.communicate(timeout=15)
        assert duplicate.returncode == 1 and "overlaps" in output
        assert cm.poll() is None and host.poll() is None
    setup = start()
    output, _ = setup.communicate(timeout=15)
    assert setup.returncode == 0, output
    assert (root / "setup-called").exists()
    host.terminate()
    host.communicate(timeout=15)
    assert cm.poll() is None and len(list(root.glob("worker-*"))) == 3
    replacement = start(service="host", FIXTURE_MIN_WORKERS="2")
    wait_for(root / ".env.host.services.lock/ready")
    assert replacement.poll() is None and cm.poll() is None
    cm.terminate()
    cm.communicate(timeout=15)
    assert replacement.poll() is None and len(list(root.glob("worker-*"))) == 2


def test_selected_service_detects_child_exit_and_cleans_up(stack):
    root, start = stack
    proc = start(service="host", FIXTURE_MIN_WORKERS="2")
    wait_for(root / ".env.host.services.lock/ready")
    pid = json.loads((root / ".env.host.services.json").read_text())["pids"][0]
    os.kill(pid, signal.SIGTERM)
    output, _ = proc.communicate(timeout=15)
    assert proc.returncode == 1 and "exited unexpectedly" in output
    assert not list(root.glob("worker-*"))


@pytest.mark.parametrize(
    "arguments", [["--service"], ["--service", "auth"], ["--service", "host", "extra"]]
)
def test_start_rejects_invalid_selection_before_environment_changes(
    tmp_path, arguments
):
    script = Path(__file__).resolve().parents[1] / "scripts/start.sh"
    env = os.environ | {"QWENPAW_DATA_ENV_FILE": str(tmp_path / "missing.env")}
    result = subprocess.run(
        ["bash", str(script), *arguments],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert not list(tmp_path.iterdir())


def test_setup_checks_each_independent_service_manifest(tmp_path):
    script = Path(__file__).resolve().parents[1] / "scripts/common/service_manifest.py"
    env = os.environ | {
        "QWENPAW_DATA_ENV_FILE": str(tmp_path / "custom.env"),
        "QWENPAW_DATA_DB_URL": "sqlite:///shared.db",
    }
    for service, addresses in [
        (
            "cm",
            [
                "--auth-url",
                "http://localhost:8088",
                "--context-url",
                "http://localhost:8765",
            ],
        ),
        ("host", ["--host-url", "http://localhost:8089"]),
    ]:
        subprocess.run(
            [
                sys.executable,
                str(script),
                "write",
                "--service",
                service,
                "--pid",
                str(os.getpid()),
                *addresses,
            ],
            env=env,
            check=True,
        )
    check = [
        sys.executable,
        str(script),
        "check",
        "--auth-url",
        "http://localhost:8088",
        "--context-url",
        "http://localhost:8765",
        "--host-url",
        "http://localhost:8089",
    ]
    subprocess.run(check, env=env, check=True)
    path = tmp_path / "custom.env.host.services.json"
    payload = json.loads(path.read_text())
    payload["database"] = "different"
    path.write_text(json.dumps(payload))
    result = subprocess.run(check, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "Control database differs" in result.stderr
    assert (tmp_path / "custom.env.cm.services.json").exists()

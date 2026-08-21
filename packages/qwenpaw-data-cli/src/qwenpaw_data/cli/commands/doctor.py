"""Doctor command: environment self-check for QwenPaw Data."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from typing import Callable

_OK = "OK"
_WARN = "WARN"
_FAIL = "FAIL"
_INFO = "INFO"


class CheckResult:
    def __init__(
        self, name: str, status: str, detail: str, hints: list[str] | None = None
    ) -> None:
        self.name = name
        self.status = status
        self.detail = detail
        self.hints = hints or []


def register(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "doctor",
        help="Check local prerequisites, configuration, and services",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable report without secret values",
    )
    parser.set_defaults(handler=handle)


def _port_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_python() -> CheckResult:
    version = platform.python_version()
    supported = (3, 11) <= sys.version_info[:2] <= (3, 13)
    return CheckResult(
        "Python",
        _OK if supported else _FAIL,
        f"{version} ({sys.executable})",
        [] if supported else ["Install Python 3.11 through 3.13."],
    )


def _command_version(command: str, *args: str) -> tuple[str | None, str | None]:
    executable = shutil.which(command)
    if executable is None:
        return None, None
    try:
        completed = subprocess.run(
            [executable, *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return executable, None
    value = (completed.stdout or completed.stderr).strip().splitlines()
    return executable, value[0] if completed.returncode == 0 and value else None


def _check_uv() -> CheckResult:
    executable, version = _command_version("uv", "--version")
    if executable and version:
        return CheckResult("uv", _OK, f"{version} ({executable})")
    return CheckResult(
        "uv",
        _FAIL,
        "not available",
        ["Install uv: https://docs.astral.sh/uv/getting-started/installation/"],
    )


def _check_node() -> CheckResult:
    executable, version = _command_version("node", "--version")
    if not executable or not version:
        return CheckResult(
            "Node.js",
            _FAIL,
            "not available",
            ["Install Node.js 22.22 or newer on the Node 22 LTS line."],
        )
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    supported = bool(match and tuple(map(int, match.groups())) >= (22, 22, 0))
    return CheckResult(
        "Node.js",
        _OK if supported else _FAIL,
        f"{version} ({executable})",
        [] if supported else ["Upgrade to Node.js 22.22 or newer."],
    )


def _check_model_config() -> CheckResult:
    model = (os.getenv("QWENPAW_DATA_MODEL_NAME") or os.getenv("LLM_MODEL") or "").strip()
    api_key = (
        os.getenv("QWENPAW_DATA_MODEL_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    ).strip()
    provider = (os.getenv("QWENPAW_DATA_MODEL_PROVIDER") or "openai").strip().lower()
    missing = [
        name for name, value in (("model", model), ("API key", api_key)) if not value
    ]
    if missing:
        return CheckResult(
            "Model config",
            _FAIL,
            f"provider={provider}; missing {', '.join(missing)}",
            ["Set LLM_MODEL and OPENAI_API_KEY (or QWENPAW_DATA_MODEL_* overrides)."],
        )
    return CheckResult(
        "Model config", _OK, f"provider={provider}; model={model}; credentials=set"
    )


def _check_auth_config() -> CheckResult:
    server_token = bool((os.getenv("QWENPAW_DATA_API_TOKEN") or "").strip())
    scoped_keys = bool((os.getenv("QWENPAW_DATA_API_KEYS") or "").strip())
    client_token = bool((os.getenv("QWENPAW_DATA_CLIENT_API_TOKEN") or "").strip())
    server_auth = server_token or scoped_keys
    if not server_auth:
        return CheckResult(
            "API authentication",
            _WARN,
            "server authentication is not configured (acceptable only on loopback)",
            ["Set QWENPAW_DATA_API_KEYS before exposing any service beyond 127.0.0.1."],
        )
    if not client_token:
        return CheckResult(
            "API authentication",
            _WARN,
            "server credentials are configured; CLI client token is missing",
            ["Set QWENPAW_DATA_CLIENT_API_TOKEN to a matching scoped key."],
        )
    mode = "scoped keys" if scoped_keys else "legacy full-scope token"
    return CheckResult("API authentication", _OK, f"{mode}; client credentials=set")


def _check_qwenpaw_data_home() -> CheckResult:
    from qwenpaw_data.host.core import resolve_qwenpaw_data_home

    home = resolve_qwenpaw_data_home()
    probe = home
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    writable = probe.is_dir() and os.access(probe, os.W_OK)
    return CheckResult(
        "QwenPaw Data home",
        _OK if writable else _FAIL,
        f"{home} ({'writable' if writable else 'not writable'})",
        [] if writable else [f"Grant write access to {probe} or set QWENPAW_DATA_HOME."],
    )


def _check_mcp_config() -> CheckResult:
    from qwenpaw_data.host.core import resolve_qwenpaw_data_home

    path = resolve_qwenpaw_data_home() / "host" / "workspace" / ".mcp"
    if not path.is_file():
        return CheckResult(
            "DataBridge MCP",
            _WARN,
            f"configuration not found at {path}",
            [
                "Run scripts/init_local.ps1 on Windows or "
                "scripts/init_local.sh on macOS/Linux."
            ],
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            "DataBridge MCP", _FAIL, f"invalid configuration ({type(exc).__name__})"
        )
    names = {
        str(item.get("name"))
        for item in payload
        if isinstance(payload, list) and isinstance(item, dict)
    }
    if "databridge" not in names:
        return CheckResult("DataBridge MCP", _WARN, "no 'databridge' client configured")
    return CheckResult("DataBridge MCP", _OK, f"configured at {path}")


def _check_docker_daemon() -> CheckResult:
    """Probe the active Docker CLI context, then known local sockets."""
    docker, _ = _command_version("docker", "--version")
    if docker:
        try:
            result = subprocess.run(
                [docker, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
            version = result.stdout.strip()
            # Some docker CLI builds exit 0 with an empty template result
            # when the daemon is unreachable, so require real output.
            if result.returncode == 0 and version:
                return CheckResult(
                    "Docker daemon", _OK, f"reachable (server {version})"
                )
        except (OSError, subprocess.TimeoutExpired):
            pass
    docker_host = (os.getenv("DOCKER_HOST") or "").strip()
    candidates: list[str] = []
    if docker_host:
        candidates.append(docker_host)
    elif platform.system() != "Windows":
        home = os.path.expanduser("~")
        candidates.extend(
            f"unix://{p}"
            for p in (
                "/var/run/docker.sock",
                f"{home}/.docker/run/docker.sock",
                f"{home}/.colima/default/docker.sock",
            )
        )

    for candidate in candidates:
        if candidate.startswith("unix://"):
            path = candidate[len("unix://") :]
            if not os.path.exists(path):
                continue
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(2.0)
                    s.connect(path)
                return CheckResult(
                    "Docker daemon",
                    _OK,
                    f"reachable via {candidate}",
                )
            except OSError:
                continue
        elif candidate.startswith("tcp://"):
            rest = candidate[len("tcp://") :]
            host, _, port = rest.partition(":")
            if port and _port_reachable(host, int(port)):
                return CheckResult("Docker daemon", _OK, f"reachable via {candidate}")

    hints: list[str] = []
    if platform.system() == "Windows":
        hints.append(
            "Windows: start Docker Desktop with Linux containers; WSL2 is the fallback.",
        )
    elif platform.system() == "Darwin":
        hints.append("macOS 无人值守安装（免 Docker Desktop 企业授权）:")
        if shutil.which("brew"):
            hints.append("  brew install colima docker && colima start")
        else:
            hints.append(
                "  先安装 Homebrew，再执行: brew install colima docker && colima start"
            )
        hints.append(
            '  然后: export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"'
        )
    else:
        hints.append(
            "Linux: sudo apt-get install docker-ce && sudo usermod -aG docker $USER"
        )
    try:
        from qwenpaw_data.cli.util import resolve_workspace_type

        workspace_type = resolve_workspace_type()
    except ValueError:
        workspace_type = "docker"
    if workspace_type == "local":
        hints.append("已显式选择 local workspace；其 shell 命令不受沙箱隔离。")
    else:
        hints.append(
            "如需临时绕过 Docker，只能显式设置 QWENPAW_DATA_WORKSPACE=local（不安全）。"
        )
    return CheckResult(
        "Docker daemon",
        _FAIL if workspace_type == "docker" else _WARN,
        "not reachable (required by selected docker workspace)",
        hints,
    )


def _check_docker_compose() -> CheckResult:
    executable, version = _command_version("docker", "compose", "version")
    if executable and version:
        return CheckResult("Docker Compose", _OK, version)
    return CheckResult(
        "Docker Compose",
        _FAIL,
        "v2 plugin not available",
        [
            "Install Docker Compose v2; on Windows, enable Linux containers "
            "in Docker Desktop."
        ],
    )


def _check_neo4j() -> CheckResult:
    port = int(os.getenv("NEO4J_BOLT_PORT", "7687"))
    if _port_reachable("127.0.0.1", port):
        return CheckResult("Neo4j (bolt)", _OK, f"127.0.0.1:{port} reachable")
    return CheckResult(
        "Neo4j (bolt)",
        _FAIL,
        f"127.0.0.1:{port} not reachable",
        [
            "Start with scripts/start_local.ps1 on Windows or "
            "scripts/start_local.sh on macOS/Linux."
        ],
    )


def _check_databridge() -> CheckResult:
    base_url = (os.getenv("QWENPAW_DATA_CM_BASE_URL") or "http://127.0.0.1:8765").rstrip("/")
    import httpx

    try:
        resp = httpx.get(f"{base_url}/api/health", timeout=5.0)
        payload = resp.json()
        if resp.status_code == 200 and (
            payload.get("ok") is True or payload.get("status") == "ok"
        ):
            return CheckResult("DataBridge API", _OK, f"{base_url}/api/health ok")
        return CheckResult(
            "DataBridge API",
            _FAIL,
            f"{base_url}/api/health returned HTTP {resp.status_code}",
        )
    except Exception as exc:
        return CheckResult(
            "DataBridge API",
            _FAIL,
            f"{base_url} not reachable ({type(exc).__name__})",
            [
                "Start with scripts/start_local.ps1 on Windows or "
                "scripts/start_local.sh on macOS/Linux."
            ],
        )


def _check_workspace_backend() -> CheckResult:
    from qwenpaw_data.cli.util import resolve_workspace_type

    try:
        workspace_type = resolve_workspace_type()
    except ValueError as exc:
        return CheckResult("Workspace backend", _FAIL, str(exc))
    return CheckResult(
        "Workspace backend",
        _INFO,
        f"{workspace_type} (override via --workspace / QWENPAW_DATA_WORKSPACE)",
    )


_CHECKS: list[Callable[[], CheckResult]] = [
    _check_python,
    _check_uv,
    _check_node,
    _check_model_config,
    _check_auth_config,
    _check_qwenpaw_data_home,
    _check_workspace_backend,
    _check_mcp_config,
    _check_docker_daemon,
    _check_docker_compose,
    _check_neo4j,
    _check_databridge,
]


def handle(args: argparse.Namespace) -> int:
    results = [check() for check in _CHECKS]
    if getattr(args, "json", False):
        print(
            json.dumps(
                {
                    "ok": not any(result.status == _FAIL for result in results),
                    "checks": [
                        {
                            "name": result.name,
                            "status": result.status,
                            "detail": result.detail,
                            "hints": result.hints,
                        }
                        for result in results
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if any(result.status == _FAIL for result in results) else 0
    width = max(len(r.name) for r in results)
    failed = False
    for r in results:
        print(f"[{r.status:>4}] {r.name.ljust(width)}  {r.detail}")
        for hint in r.hints:
            print(f"       {hint}", file=sys.stderr)
        if r.status == _FAIL:
            failed = True
    return 1 if failed else 0

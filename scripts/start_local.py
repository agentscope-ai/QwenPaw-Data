#!/usr/bin/env python3
"""Start Neo4j, the DataBridge API, and its frontend on Windows, macOS, or Linux."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from _local_common import (
    ensure_environment,
    port_reachable,
    process_group_options,
    repository_root,
    resolve_command,
    run,
    terminate_process_tree,
    url_host,
    venv_executable,
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--context-host", default=os.getenv("CONTEXT_HOST", "127.0.0.1")
    )
    result.add_argument(
        "--context-port", type=int, default=int(os.getenv("CONTEXT_PORT", "8765"))
    )
    result.add_argument(
        "--context-log-level", default=os.getenv("CONTEXT_LOG_LEVEL", "info")
    )
    result.add_argument("--context-reload", action="store_true")
    result.add_argument("--skip-frontend", action="store_true")
    result.add_argument(
        "--skip-neo4j",
        action="store_true",
        help="Start only DataBridge processes; intended for CI or an external Neo4j.",
    )
    result.add_argument("--npm", default=os.getenv("NPM", "npm"))
    return result


def start_neo4j(root: Path, env_file: Path) -> None:
    bolt_port = int(os.getenv("NEO4J_BOLT_PORT", "7687"))
    if port_reachable("127.0.0.1", bolt_port):
        print(f"Reusing already-running Neo4j at 127.0.0.1:{bolt_port}.")
        return
    docker = resolve_command("docker")
    if subprocess.run(
        [docker, "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode:
        raise RuntimeError(
            "Docker daemon is not running. Start Docker Desktop/Engine, then retry."
        )
    compose = root / "packages" / "qwenpaw-data-context" / "docker-compose.yml"
    run(
        [docker, "compose", "--env-file", env_file, "-f", compose, "up", "-d"], cwd=root
    )


def start_child(
    argv: list[str], *, cwd: Path, env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    print("+", subprocess.list2cmdline(argv))
    return subprocess.Popen(argv, cwd=cwd, env=env, **process_group_options())


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = repository_root()
    env_file = ensure_environment(root)
    context = root / "packages" / "qwenpaw-data-context"
    frontend = context / "frontend"
    configured_python = os.getenv("CONTEXT_PYTHON", "").strip()
    try:
        context_python = (
            Path(resolve_command(configured_python))
            if configured_python
            else venv_executable(context / ".venv", "python")
        )
    except FileNotFoundError as exc:
        print(exc, file=sys.stderr)
        return 1
    if not context_python.exists():
        print(
            f"DataBridge Python environment not found: {context_python}",
            file=sys.stderr,
        )
        print("Run scripts/init_local.py first.", file=sys.stderr)
        return 1
    if port_reachable("127.0.0.1", args.context_port):
        print(
            f"DataBridge API port {args.context_port} is already in use; refusing to stop an unrelated process.",
            file=sys.stderr,
        )
        return 1
    if not args.skip_frontend and port_reachable("127.0.0.1", 3000):
        print(
            "Frontend port 3000 is already in use; refusing to stop an unrelated process.",
            file=sys.stderr,
        )
        return 1

    children: list[subprocess.Popen[bytes]] = []
    try:
        npm = resolve_command(args.npm) if not args.skip_frontend else args.npm
        if not args.skip_neo4j:
            start_neo4j(root, env_file)
        api_command = [
            str(context_python),
            "scripts/serve.py",
            "--host",
            args.context_host,
            "--port",
            str(args.context_port),
            "--log-level",
            args.context_log_level,
        ]
        if args.context_reload:
            api_command.append("--reload")
        frontend_host = os.getenv("FRONTEND_HOST", "127.0.0.1")
        if not args.skip_frontend and frontend_host not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            api_command.append("--require-auth")
        children.append(start_child(api_command, cwd=context))
        if not args.skip_frontend:
            frontend_env = os.environ.copy()
            frontend_env["VITE_API_BASE_URL"] = ""
            frontend_env["SERVICE_BASE_URL"] = (
                f"http://{url_host(args.context_host)}:{args.context_port}"
            )
            children.append(
                start_child(
                    [
                        npm,
                        "run",
                        "dev",
                        "--",
                        "--host",
                        frontend_host,
                        "--port",
                        "3000",
                        "--strictPort",
                    ],
                    cwd=frontend,
                    env=frontend_env,
                )
            )
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        for child in reversed(children):
            terminate_process_tree(child)
        print(exc, file=sys.stderr)
        return 1

    stopping = False

    def request_stop(_signum: int, _frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    try:
        while not stopping:
            for child in children:
                status = child.poll()
                if status is not None:
                    print(
                        f"Local service exited with status {status}.", file=sys.stderr
                    )
                    return status or 1
            time.sleep(0.25)
    finally:
        for child in reversed(children):
            terminate_process_tree(child)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

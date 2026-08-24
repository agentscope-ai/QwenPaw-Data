#!/usr/bin/env python3
"""Run a deterministic QwenPaw Data CLI smoke test without an external model."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from init_demo import configure_demo_bundle, load_repo_environment, seed_postgres


REPO_ROOT = Path(__file__).resolve().parent.parent
SUCCESS_MARKER = "QWENPAW_DATA_DEMO_SMOKE_OK"
DEMO_SQL = (
    "SELECT ds, ROUND(AVG(gaap_val), 2) AS average_gaap "
    "FROM dws_gaap_di WHERE product = 'X' AND ytd_gaap >= 10 "
    "GROUP BY ds ORDER BY ds"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(url: str, process: subprocess.Popen[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not attempted"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"DataBridge exited early with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"DataBridge did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class _ModelStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _ModelHandler)
        self.saw_execute_sql = False
        self.saw_expected_result = False


class _ModelHandler(BaseHTTPRequestHandler):
    server: _ModelStub
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            self._respond_to_chat(body)
        except (json.JSONDecodeError, OSError, ValueError) as exc:
            payload = json.dumps({"error": {"message": str(exc)}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    def _respond_to_chat(self, body: dict[str, Any]) -> None:
        messages = body.get("messages", [])
        tool_messages = (
            [
                message
                for message in messages
                if isinstance(message, dict) and message.get("role") == "tool"
            ]
            if isinstance(messages, list)
            else []
        )
        serialized = json.dumps(tool_messages, ensure_ascii=False)
        has_tool_result = bool(tool_messages)
        if has_tool_result:
            self.server.saw_execute_sql = True
            expected_values = (
                "2026-03-01",
                "4.64",
                "2026-03-10",
                "45.89",
                "2026-03-15",
                "3.62",
            )
            if not all(value in serialized for value in expected_values):
                raise ValueError(
                    "execute_sql did not return the expected demo aggregates: "
                    f"{serialized[-3000:]}",
                )
            self.server.saw_expected_result = True
            self._send_sse(
                [
                    self._chunk({"role": "assistant", "content": SUCCESS_MARKER}),
                    self._chunk({}, finish_reason="stop"),
                ],
            )
            return

        tool_names = []
        for tool in body.get("tools", []):
            if not isinstance(tool, dict):
                continue
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                tool_names.append(function["name"])
        execute_tool = next(
            (name for name in tool_names if name.endswith("__execute_sql")),
            None,
        )
        if execute_tool is None:
            raise ValueError("DataBridge execute_sql tool was not exposed to the model")

        call_id = "call_qwenpaw_data_demo_sql"
        self._send_sse(
            [
                self._chunk(
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": execute_tool,
                                    "arguments": "",
                                },
                            },
                        ],
                    },
                ),
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": json.dumps({"sql": DEMO_SQL}),
                                },
                            },
                        ],
                    },
                ),
                self._chunk({}, finish_reason="tool_calls"),
            ],
        )

    @staticmethod
    def _chunk(
        delta: dict[str, Any], finish_reason: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": "chatcmpl-qwenpaw-data-demo",
            "object": "chat.completion.chunk",
            "created": 1785940800,
            "model": "qwenpaw-data-demo-model",
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                },
            ],
        }

    def _send_sse(self, chunks: list[dict[str, Any]]) -> None:
        lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
        lines.append("data: [DONE]\n\n")
        payload = "".join(lines).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _write_mcp_config(home: Path, cm_port: int) -> None:
    target = home / "host" / "workspace" / ".mcp"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            [
                {
                    "name": "databridge",
                    "is_stateful": False,
                    "mcp_config": {
                        "type": "http_mcp",
                        "url": f"http://127.0.0.1:{cm_port}/mcp/v1/cm",
                        "headers": {},
                        "timeout": 30.0,
                    },
                    "enable_tools": ["execute_sql"],
                    "disable_tools": None,
                    "execution_timeout": 30.0,
                },
            ],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _postgres_config(dsn: str) -> dict[str, Any]:
    parsed = urllib.parse.urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise ValueError("--postgres-dsn must use the postgres or postgresql scheme")
    if not parsed.hostname or not parsed.path.lstrip("/") or not parsed.username:
        raise ValueError("--postgres-dsn must include host, database, and user")
    return {
        "host": parsed.hostname,
        "port": parsed.port or 5432,
        "dbname": urllib.parse.unquote(parsed.path.lstrip("/")),
        "user": urllib.parse.unquote(parsed.username),
        "password": urllib.parse.unquote(parsed.password or ""),
    }


def _run_cli(
    env: dict[str, str], *args: str, timeout: float = 90
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "qwenpaw_data.cli.main", *args]
    completed = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command)} failed ({completed.returncode})\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
    return completed


def run_smoke(args: argparse.Namespace) -> None:
    seed_postgres(args.postgres_dsn)
    postgres = _postgres_config(args.postgres_dsn)
    cm_port = _free_port()
    model_port = _free_port()
    model_server = _ModelStub(("127.0.0.1", model_port))
    model_thread = threading.Thread(target=model_server.serve_forever, daemon=True)
    model_thread.start()

    with tempfile.TemporaryDirectory(prefix="qwenpaw-data-demo-smoke-") as temp_name:
        temp_root = Path(temp_name)
        home = temp_root / "home"
        log_path = temp_root / "databridge.log"
        _write_mcp_config(home, cm_port)
        env = dict(os.environ)
        env.update(
            {
                "QWENPAW_DATA_HOME": str(home),
                "QWENPAW_DATA_CM_BASE_URL": f"http://127.0.0.1:{cm_port}",
                "QWENPAW_DATA_MODEL_PROVIDER": "openai",
                "QWENPAW_DATA_MODEL_NAME": "qwenpaw-data-demo-model",
                "QWENPAW_DATA_MODEL_API_KEY": "local-demo-key",
                "QWENPAW_DATA_MODEL_BASE_URL": f"http://127.0.0.1:{model_port}/v1",
                "QWENPAW_DATA_WORKSPACE": "local",
                # The deterministic local stub can request only the fixed SQL
                # call below. Bypass is explicit because unattended dont_ask
                # correctly denies executable MCP tools.
                "QWENPAW_DATA_PERMISSION_MODE": "bypass",
                "QWENPAW_DATA_SPAWN_SUBAGENT_ENABLED": "0",
                "NEO4J_URI": args.neo4j_uri,
                "NEO4J_USER": args.neo4j_user,
                "NEO4J_PASSWORD": args.neo4j_password,
                "SQL_EXEC_BACKEND": "direct",
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "QWENPAW_DATA_API_TOKEN": "",
                "QWENPAW_DATA_API_KEYS": "",
                "QWENPAW_DATA_CLIENT_API_TOKEN": "",
                "NEO4J_DATABASE": "",
            },
        )

        with log_path.open("w", encoding="utf-8") as log_file:
            bridge = subprocess.Popen(
                [
                    sys.executable,
                    str(
                        REPO_ROOT
                        / "packages"
                        / "qwenpaw-data-context"
                        / "scripts"
                        / "serve.py"
                    ),
                    "--port",
                    str(cm_port),
                    "--log-level",
                    "warning",
                ],
                cwd=REPO_ROOT,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait_for_health(
                    f"http://127.0.0.1:{cm_port}/api/health",
                    bridge,
                    args.startup_timeout,
                )
                datasource_id = configure_demo_bundle(
                    f"http://127.0.0.1:{cm_port}",
                    int(postgres["port"]),
                    host=str(postgres["host"]),
                    dbname=str(postgres["dbname"]),
                    user=str(postgres["user"]),
                    password=str(postgres["password"]),
                )
                listed = _run_cli(env, "datasource", "list")
                listing = json.loads(listed.stdout)
                if not any(
                    item.get("datasource_id") == datasource_id
                    for item in listing.get("items", [])
                    if isinstance(item, dict)
                ):
                    raise RuntimeError(
                        "registered demo datasource was not listed by the CLI"
                    )

                result = _run_cli(
                    env,
                    "run",
                    (
                        "Analyze the average GAAP value of valid users for product X "
                        "during March 2026 and identify the largest spike."
                    ),
                    "--no-stream",
                    "--workspace",
                    "local",
                    "--permission-mode",
                    "bypass",
                    "--datasource-id",
                    datasource_id,
                )
                if SUCCESS_MARKER not in result.stdout:
                    raise RuntimeError(
                        f"CLI response did not contain {SUCCESS_MARKER!r}"
                    )
                if (
                    not model_server.saw_execute_sql
                    or not model_server.saw_expected_result
                ):
                    raise RuntimeError(
                        "CLI did not execute and verify the demo SQL tool call"
                    )

                sessions = list(
                    (home / "host" / "workspace" / "sessions").rglob("*.json")
                )
                if not sessions:
                    raise RuntimeError("CLI run did not persist a session trace")
                print(listed.stdout.strip())
                print(result.stdout.strip())
                print(f"Session files: {len(sessions)}")
            except BaseException as exc:
                try:
                    log_file.flush()
                    bridge_log = log_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    bridge_log = "<unavailable>"
                raise RuntimeError(
                    f"{exc}\nDataBridge log:\n{bridge_log[-8000:]}"
                ) from exc
            finally:
                _stop_process(bridge)
                model_server.shutdown()
                model_server.server_close()
                model_thread.join(timeout=5)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-dsn",
        default=("postgresql://qwenpaw_data:qwenpaw-data-demo@127.0.0.1:55432/qwenpaw_data_demo"),
    )
    parser.add_argument("--neo4j-uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--neo4j-user", default="neo4j")
    parser.add_argument(
        "--neo4j-password",
        default=os.getenv("NEO4J_PASSWORD", "qwenpaw-data-demo"),
    )
    parser.add_argument("--startup-timeout", type=float, default=30.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    load_repo_environment()
    try:
        run_smoke(parse_args(argv))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"QwenPaw Data demo smoke failed: {exc}", file=sys.stderr)
        return 1
    print("QwenPaw Data deterministic demo smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

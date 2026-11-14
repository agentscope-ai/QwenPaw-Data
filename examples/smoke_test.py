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
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from init_demo import _request_json, seed_postgres


REPO_ROOT = Path(__file__).resolve().parent.parent
SUCCESS_MARKER = "QWENPAW_DATA_DEMO_SMOKE_OK"
EMBEDDING_DIMENSIONS = 1024  # Current Neo4j Store catalog contract.
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
                f"Service at {url} exited early with code {process.returncode}"
            )
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.2)
    raise RuntimeError(f"Service at {url} did not become healthy: {last_error}")


def _stop_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _smoke_environment(
    root: Path, cm_port: int, args: argparse.Namespace
) -> dict[str, str]:
    """Use disposable shared SQL state, never the caller's runtime or .env."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            (
                "QWENPAW_DATA_",
                "CM_",
                "NEO4J_",
                "HOLO_",
                "TRACING_",
                "OTEL_",
                "OPENAI_",
                "ANTHROPIC_",
                "DASHSCOPE_",
                "EMBED_",
                "LLM_",
                "RERANK_",
                "FC_",
                "ACS_",
            )
        )
        and key not in {"PYTHONPATH", "GRAPH_BACKEND"}
    }
    env.update(
        {
            "QWENPAW_DATA_HOME": str(root / "home"),
            "QWENPAW_DATA_ENV_FILE": str(root / "absent.env"),
            "QWENPAW_DATA_DB_URL": f"sqlite+aiosqlite:///{root / 'control.sqlite'}",
            "QWENPAW_DATA_AUTH_MODE": "local",
            "QWENPAW_DATA_CM_BASE_URL": f"http://127.0.0.1:{cm_port}",
            # Only our scripted SQL request runs; Docker isolation has separate CI.
            "QWENPAW_DATA_WORKSPACE_BACKEND": "local",
            "QWENPAW_DATA_TOOL_CONFIRMATION_DEFAULT_LEVEL": "off",
            "QWENPAW_DATA_FOLLOWUP_ENABLED": "0",
            "QWENPAW_DATA_SETTLEMENT_ENABLED": "0",
            "QWENPAW_DATA_BIZ_LINK_ENABLED": "0",
            "QWENPAW_DATA_BIZ_TRACE_ENABLED": "0",
            "QWENPAW_DATA_TRACE2SEGMENT_ENABLED": "0",
            "NEO4J_URI": args.neo4j_uri,
            "NEO4J_USER": args.neo4j_user,
            "NEO4J_PASSWORD": args.neo4j_password,
            "NEO4J_DATABASE": "",
            "GRAPH_BACKEND": "neo4j",
            "SQL_EXEC_BACKEND": "direct",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


@contextmanager
def _host_service(env: dict[str, str], root: Path, timeout: float):
    """Host owns the shared Auth/Host schema and must start before Context."""
    port = _free_port()
    env["QWENPAW_DATA_HOST_URL"] = f"http://127.0.0.1:{port}"
    path = root / "host.log"
    with path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "qwenpaw_data.host.core.api.app:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for_health(env["QWENPAW_DATA_HOST_URL"] + "/health", process, timeout)
            yield
        except Exception as exc:
            log.flush()
            raise RuntimeError(
                f"{exc}\nHost log:\n{path.read_text(encoding='utf-8', errors='replace')[-8000:]}"
            ) from exc
        finally:
            _stop_process(process)


def _host_json(env: dict[str, str], path: str, *, payload: Any = None) -> Any:
    request = urllib.request.Request(
        env["QWENPAW_DATA_HOST_URL"] + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "PUT",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


class _ModelStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, _ModelHandler)
        self.saw_execute_read = False
        self.datasource_id = ""
        self.saw_expected_result = False
        self.embedding_calls = 0


class _ModelHandler(BaseHTTPRequestHandler):
    server: _ModelStub
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        if self.path.rstrip("/") not in {"/v1/chat/completions", "/v1/embeddings"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("request body must be an object")
            if self.path.rstrip("/") == "/v1/embeddings":
                inputs = body["input"]
                if isinstance(inputs, str):
                    inputs = [inputs]
                self.server.embedding_calls += len(inputs)
                payload = json.dumps(
                    {
                        "object": "list",
                        "model": body["model"],
                        "data": [
                            {
                                "object": "embedding",
                                "index": i,
                                "embedding": [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
                            }
                            for i in range(len(inputs))
                        ],
                        "usage": {
                            "prompt_tokens": len(inputs),
                            "total_tokens": len(inputs),
                        },
                    }
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
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
            self.server.saw_execute_read = True
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
                    "execute_read did not return the expected demo aggregates: "
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
            (name for name in tool_names if name.endswith("__execute_read")),
            None,
        )
        if execute_tool is None:
            raise ValueError(
                "DataBridge execute_read tool was not exposed to the model"
            )

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
                                    "arguments": json.dumps(
                                        {
                                            "sql": DEMO_SQL,
                                            "datasource_id": self.server.datasource_id,
                                        }
                                    ),
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
        usage = self._chunk({})
        usage["choices"] = []
        usage["usage"] = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        chunks = [*chunks, usage]
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


@contextmanager
def _embedding_fixture(env: dict[str, str]):
    """Use deterministic vectors; publication still uses the real graph backend."""
    server = _ModelStub(("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/v1"
        _host_json(
            env,
            "/api/preferences/providers/openai",
            payload={"api_key": "fixture-embedding", "base_url": base},
        )
        _request_json(
            env["QWENPAW_DATA_CM_BASE_URL"] + "/api/v1/config/embed",
            method="PUT",
            headers={"Content-Type": "application/json"},
            payload={
                "provider_id": "openai",
                "model": "fixture-embedding",
                "dim": EMBEDDING_DIMENSIONS,
            },
        )
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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
            f"CLI {args[0] if args else 'command'} failed ({completed.returncode})\n"
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

    with ExitStack() as cleanup:
        cleanup.callback(model_thread.join, timeout=5)
        cleanup.callback(model_server.server_close)
        cleanup.callback(model_server.shutdown)
        temp_name = cleanup.enter_context(
            tempfile.TemporaryDirectory(prefix="qwenpaw-data-demo-smoke-")
        )
        temp_root = Path(temp_name)
        log_path = temp_root / "databridge.log"
        env = _smoke_environment(temp_root, cm_port, args)
        cleanup.enter_context(_host_service(env, temp_root, args.startup_timeout))
        for path, payload in [
            (
                "providers/openai",
                {
                    "api_key": "local-demo-key",
                    "base_url": f"http://127.0.0.1:{model_port}/v1",
                },
            ),
            (
                "providers/openai/models/qwenpaw-data-demo-model",
                {"source": "extra", "name": "Smoke fixture", "context_size": 32000},
            ),
            (
                "active-models",
                {
                    "default_provider_id": "openai",
                    "default_model_id": "qwenpaw-data-demo-model",
                },
            ),
        ]:
            _host_json(env, "/api/preferences/" + path, payload=payload)
        _host_json(
            env,
            "/api/mcp/context-manager/tools",
            payload={"enable_tools": ["execute_read"]},
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
                created = _run_cli(
                    env,
                    "datasource",
                    "create",
                    "--name",
                    "Demo PG - GAAP smoke",
                    "--type",
                    "postgresql",
                    "--config",
                    json.dumps(postgres),
                )
                datasource_id = json.loads(created.stdout)["datasource_id"]
                model_server.datasource_id = datasource_id
                _run_cli(
                    env,
                    "semantic",
                    "import",
                    "--file",
                    str(REPO_ROOT / "examples/demo_semantic_config.xlsx"),
                    "--datasource-id",
                    datasource_id,
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
                    "--datasource-id",
                    datasource_id,
                )
                if SUCCESS_MARKER not in result.stdout:
                    raise RuntimeError(
                        f"CLI response did not contain {SUCCESS_MARKER!r}"
                    )
                if (
                    not model_server.saw_execute_read
                    or not model_server.saw_expected_result
                ):
                    raise RuntimeError(
                        "CLI did not execute and verify the demo SQL tool call"
                    )

                session_id = next(
                    line.removeprefix("Session: ")
                    for line in result.stderr.splitlines()
                    if line.startswith("Session: ")
                )
                snapshot = _host_json(env, f"/api/sessions/{session_id}/snapshot")
                if (
                    len(snapshot["chats"]) != 1
                    or snapshot["chats"][0]["status"] != "completed"
                ):
                    raise RuntimeError("CLI run did not persist a completed SQL chat")
                print(listed.stdout.strip())
                print(result.stdout.strip())
                print("Persisted SQL chats: 1")
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


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--postgres-dsn",
        default=(
            "postgresql://qwenpaw_data:qwenpaw-data-demo@127.0.0.1:55432/qwenpaw_data_demo"
        ),
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
    try:
        run_smoke(parse_args(argv))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"QwenPaw Data demo smoke failed: {exc}", file=sys.stderr)
        return 1
    print("QwenPaw Data deterministic demo smoke passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

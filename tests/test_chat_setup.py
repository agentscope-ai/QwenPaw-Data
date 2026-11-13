"""Run the Bash orchestrator over real Auth APIs and isolated model/data stand-ins."""

from __future__ import annotations

import importlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import unquote

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return importlib.import_module("chat_setup.verify")


@pytest.mark.parametrize(
    "status,error,answer,passes",
    [
        ("completed", None, "ready", True),
        ("completed", None, "", False),
        ("failed", {"code": "CONFIG"}, "", False),
    ],
)
def test_smoke_requires_completed_visible_answer(setup, status, error, answer, passes):
    session = Mock()
    session.post.side_effect = [
        setup.Result({"session": {"id": "s"}}),
        setup.Result({"chat": {"id": "c"}}),
    ]
    session.get.return_value = setup.Result(
        {
            "chats": [
                {
                    "id": "c",
                    "status": status,
                    "error": error,
                    "segments": [{"conclusion": answer}],
                }
            ],
        }
    )
    ctx = SimpleNamespace(log=Mock())
    if passes:
        assert setup.smoke_chat(ctx, session, "ds", "user@One") == "s"
    else:
        with pytest.raises(RuntimeError):
            setup.smoke_chat(ctx, session, "ds", "user@One")


def test_smoke_timeout_stops_its_own_chat(setup, monkeypatch):
    session = Mock()
    session.post.side_effect = [
        setup.Result({"session": {"id": "s"}}),
        setup.Result({"chat": {"id": "c"}}),
        setup.Result({"chat": {"id": "c", "status": "canceled"}}),
    ]
    session.get.return_value = setup.Result(
        {"chats": [{"id": "c", "status": "running"}]}
    )
    monkeypatch.setattr(setup, "CHAT_TIMEOUT", 10)
    times = iter([0, 11])
    monkeypatch.setattr(setup.time, "monotonic", lambda: next(times))
    with pytest.raises(RuntimeError, match="timed out"):
        setup.smoke_chat(SimpleNamespace(log=Mock()), session, "ds", "user")
    assert session.post.call_args.kwargs["params"] == {
        "session_id": "s",
        "chat_id": "c",
    }


@pytest.fixture
def stack(monkeypatch, tmp_path, request):
    """Real Auth/SQLite; fake only the external provisioner and model/data services."""
    import uvicorn
    from qwenpaw_data_auth import auth_api
    from qwenpaw_data_auth.database import get_db, query_one
    from qwenpaw_data_auth.server import create_app
    from starlette.responses import JSONResponse

    dsn = f"sqlite:///{tmp_path / 'auth.db'}"
    # Each server models a separate deployment; process-global rate-limit
    # counters must not accumulate across otherwise isolated databases.
    monkeypatch.setattr(auth_api, "login_fails_by_ip", defaultdict(list))
    monkeypatch.setattr(auth_api, "login_fails_by_user", defaultdict(list))
    for key, value in {
        "QWENPAW_DATA_DB_URL": dsn,
        "QWENPAW_DATA_ENV": "development",
        "QWENPAW_DATA_AUTH_SECRET": "isolated-chat-setup-secret",
        "QWENPAW_DATA_PLATFORM_BOOTSTRAP_TOKEN": "bootstrap-test",
        "QWENPAW_DATA_AUTH_ENABLED": "1",
        "QWENPAW_DATA_AUTH_MODE": "multi-user",
        "ACL_ENABLED": "false",
        "GRAPH_BACKEND": "neo4j",
    }.items():
        monkeypatch.setenv(key, value)
    app = create_app()
    state = SimpleNamespace(
        calls=[],
        providers={},
        active={},
        configs={},
        spaces={},
        sources={},
        metadata=set(),
        tasks={},
        imports=[],
        sync=set(),
        defaults={},
        chats=0,
        import_error=False,
        weave_status="SUCCESS",
        conflict=False,
        provision_error=False,
        polls={},
        connections=True,
    )

    @app.middleware("http")
    async def fake_services(request, call_next):
        path = request.url.path
        method = request.method
        if path.startswith(
            (
                "/api/preferences",
                "/api/v1/",
                "/api/semantic-config",
                "/api/datasources",
                "/api/datasets",
                "/api/sessions",
                "/api/console",
            )
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get(
                    "/api/auth/me",
                    headers={"cookie": request.headers.get("cookie", "")},
                )
            if response.status_code != 200:
                return JSONResponse({"detail": "login required"}, status_code=401)
            me = response.json()
            me.update(me.get("user", {}))
            uid, wid = me["user_id"], me["active_workspace_id"]
            scoped = (uid, wid)
            body = (
                await request.json()
                if "application/json" in request.headers.get("content-type", "")
                else {}
            )
            state.calls.append((method, path, uid, wid, body))
            result = None
            if path == "/api/preferences/providers":
                result = {
                    "providers": [
                        {
                            "id": p,
                            "configured": (uid, p) in state.providers,
                            "api_key_masked": (
                                "masked"
                                if state.providers.get((uid, p), {}).get("api_key")
                                else ""
                            ),
                            "base_url": state.providers.get((uid, p), {}).get(
                                "base_url", ""
                            ),
                            "models": [
                                {"id": model, "context_size": 8192}
                                for model in ("qwen3.7-max", "qwen3.6-plus")
                            ],
                        }
                        for p in ("openai", "dashscope")
                    ]
                }
            elif path.startswith("/api/preferences/providers/"):
                provider = path.split("/")[4]
                if path.endswith("/models"):
                    result = {
                        "models": [
                            {
                                "id": model,
                                "source": "builtin",
                                "context_size": 8192,
                            }
                            for model in ("qwen3.7-max", "qwen3.6-plus")
                        ]
                    }
                elif "/models/" in path:
                    result = {}
                else:
                    state.providers[uid, provider] = body
                    result = {}
            elif path == "/api/preferences/active-models":
                if method == "PUT":
                    state.active[uid] = body
                result = {
                    "active_models": state.active.get(
                        uid, {"default_provider_id": "", "default_model_id": ""}
                    )
                }
            elif path == "/api/v1/config":
                config = state.configs.setdefault(scoped, {})
                result = {
                    "llm": config.get("llm", {"model": ""}),
                    "embedding": config.get("embed", {"model": ""}),
                    "errors": {},
                    "configured": all(k in config for k in ("llm", "embed")),
                    "embedding_space": state.spaces.get(
                        wid, {"adopted": False, "aligned": False}
                    ),
                }
            elif path.startswith("/api/v1/config/"):
                channel = path.split("/")[4]
                if path.endswith("/test"):
                    result = {"success": True}
                else:
                    assert (uid, body["provider_id"]) in state.providers
                    state.configs.setdefault(scoped, {})[channel] = body
                    if channel == "embed":
                        state.spaces[wid] = body | {"adopted": True, "aligned": True}
                    result = {}
            elif path == "/api/semantic-config/datasource":
                if method == "POST":
                    assert me["role"] == "tenant_admin"
                    state.sources[wid] = body | {"datasource_id": "ds-" + wid}
                    result = {"datasource_id": "ds-" + wid}
                else:
                    result = {
                        "items": [state.sources[wid]] if wid in state.sources else []
                    }
            elif path.endswith("/explore-scope"):
                result = {}
            elif path.endswith("/test-connection"):
                result = {"success": state.connections}
            elif path == "/api/semantic-config/dataset-meta":
                result = {"total": int(wid in state.metadata)}
            elif path == "/api/semantic-config/import/excel":
                form = await request.form()
                state.imports.append(
                    (
                        form["file"].filename,
                        await form["file"].read(),
                        form["datasource_id"],
                    )
                )
                if state.import_error:
                    result = {"success": False, "errors": ["Invalid workbook"]}
                else:
                    task = "auto-" + wid
                    state.metadata.add(wid)
                    state.tasks[task] = wid
                    result = {
                        "success": True,
                        "weave_task_id": task,
                        "summary": {"dataset": 1},
                    }
            elif path == "/api/semantic-config/weave-task":
                rows = [
                    {
                        "task_id": task,
                        "datasource_id": "ds-" + wid,
                        "status": "FAILED"
                        if task == getattr(state, "failed_task", None)
                        else (
                            state.weave_status
                            if state.polls.get(task, 0) > 1
                            else "RUNNING"
                        ),
                    }
                    for task, workspace in reversed(list(state.tasks.items()))
                    if workspace == wid
                ]
                result = {"records": rows, "total": len(rows)}
            elif path == "/api/semantic-config/weave-task/submit":
                task = "manual-" + wid
                state.tasks[task] = wid
                state.sync.clear()
                result = {"task_id": task, "status": "RUNNING"}
            elif path.startswith("/api/semantic-config/weave-task/"):
                task = path.rsplit("/", 1)[1]
                assert task in state.tasks
                state.polls[task] = state.polls.get(task, 0) + 1
                result = {
                    "status": state.weave_status
                    if state.polls[task] > 1
                    else "RUNNING",
                    "error_msg": "weave failure",
                }
            elif path == "/api/v1/mg/userspace/sync":
                state.sync.add(scoped)
                result = {"conflict_count": int(state.conflict)}
            elif path == "/api/v1/mg/userspace/sync-status":
                result = {"needs_sync": scoped not in state.sync}
            elif path == "/api/datasources/active":
                if method == "PUT":
                    state.defaults[scoped] = body["datasource_id"]
                result = {"datasource_id": state.defaults.get(scoped)}
            elif path == "/api/datasources":
                result = {
                    "items": [{"id": state.sources[wid]["datasource_id"]}]
                    if wid in state.sources
                    else []
                }
            elif path == "/api/datasets":
                result = {"items": [{"id": "dataset"}] if wid in state.metadata else []}
            elif path == "/api/sessions":
                state.chats += 1
                result = {"session": {"id": str(state.chats)}}
            elif path == "/api/console/chat":
                result = {"chat": {"id": "chat"}}
            elif path.endswith("/snapshot"):
                result = {
                    "chats": [
                        {
                            "id": "chat",
                            "status": "completed",
                            "segments": [{"conclusion": "Ready"}],
                        }
                    ]
                }
            assert result is not None, (method, path)
            return JSONResponse(result)
        state.calls.append((method, path, None, None, {}))
        response = await call_next(request)
        if path.endswith("/provision") and response.status_code < 300:
            with get_db(dsn) as conn:
                ws = query_one(
                    conn, "SELECT * FROM workspaces WHERE instance_status='queued'"
                )
                assert ws
                if state.provision_error:
                    conn.execute(
                        "UPDATE workspaces SET instance_status='failed' WHERE id=?",
                        (ws["id"],),
                    )
                else:
                    conn.execute(
                        "UPDATE workspaces SET status='active', instance_status='ready' WHERE id=?",
                        (ws["id"],),
                    )
                    conn.execute(
                        "UPDATE tenants SET status='active', onboarding_state='ready' WHERE id=?",
                        (ws["tenant_id"],),
                    )
                    conn.execute(
                        "UPDATE users SET status='active' WHERE tenant_id=?",
                        (ws["tenant_id"],),
                    )
                conn.commit()
        return response

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    origin = f"http://{getattr(request, 'param', '127.0.0.1')}:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        assert thread.is_alive() and time.monotonic() < deadline, (
            "Auth test server did not start"
        )
        time.sleep(0.01)
    env = os.environ | {
        "QWENPAW_DATA_ENV_FILE": str(tmp_path / ".env"),
        "QWENPAW_DATA_AUTH_URL": origin,
        "QWENPAW_DATA_CM_BASE_URL": origin,
        "QWENPAW_DATA_HOST_BASE_URL": origin,
        "VITE_HOST_FRONTEND_URL": origin,
        "OPENAI_API_KEY": 'key"$test',
        "EMBED_MODEL": "text-embedding-v3",
        "EMBED_DIM": "1024",
        "QWENPAW_DATA_SETUP_HTTP_TIMEOUT": "10",
        "TMPDIR": str(tmp_path),
        "QWENPAW_DATA_MODEL_BASE_URL": "",
        "QWENPAW_DATA_MODEL_NAME": "",
        "QWENPAW_DATA_MODEL_API_KEY": "",
        "QWENPAW_DATA_MODEL_PROVIDER": "",
        "OPENAI_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "LLM_MODEL": "qwen3.7-max",
        "EMBED_OPENAI_BASE_URL": "",
        "EMBED_OPENAI_API_KEY": "",
    }

    def run(*, prompt_answers=None, **extra):
        command = 'source "$1"; chat_setup_configure "$2"'
        if prompt_answers is not None:
            command = """source "$1"
            cs_prompt_secret() {
                printf '%s\\n' "$1" >&2
                case "$1" in
                    "API key for "*) cs_secret="$TEST_MODEL_KEY" ;;
                    "Existing password for acme-admin"*) cs_secret="$TEST_ADMIN_PASSWORD" ;;
                    "Existing password for alice"*) cs_secret="$TEST_MEMBER_PASSWORD" ;;
                    *) return 1 ;;
                esac
                [[ -n "$cs_secret" ]]
            }
            chat_setup_configure "$2"
            """
            extra |= prompt_answers
        return subprocess.run(
            [
                "bash",
                "-c",
                command,
                "_",
                str(ROOT / "scripts/setup_chat.sh"),
                sys.executable,
            ],
            cwd=tmp_path,
            env=env | extra,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    state.run = run
    state.env = env
    state.tmp = tmp_path
    state.db = dsn
    try:
        yield state
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        assert not thread.is_alive()


def assert_success(stack, result):
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Chat ready" in result.stdout
    assert stack.env["OPENAI_API_KEY"] not in result.stdout + result.stderr
    assert "DP_CHAT_READY=1" in (stack.tmp / ".env.chat-setup/state.env").read_text()
    assert not list(stack.tmp.glob("qwenpaw-data-chat-setup.*")), "cookie jars leaked"


def test_default_credentials_survive_reruns_and_missing_passwords_are_prompted(stack):
    example = ROOT / "scripts/chat_setup/fixtures/config.example.json"
    (stack.tmp / ".env.chat-setup.json").write_bytes(example.read_bytes())
    inputs = {"OPENAI_API_KEY": ""}
    model_key = 'interactive-key"$'
    result = stack.run(prompt_answers={"TEST_MODEL_KEY": model_key}, **inputs)
    assert_success(stack, result)
    assert result.stderr.count("API key for ") == 1
    assert model_key not in result.stdout + result.stderr
    assert not stack.sources and not stack.imports
    directory = stack.tmp / ".env.chat-setup/credentials"
    assert str(directory) in result.stdout
    files = {path.name: path.read_text().strip() for path in directory.iterdir()}
    assert set(files) == {
        "platform-platform_admin.password",
        "account-acme-admin.password",
        "account-alice.password",
    }
    assert len(set(files.values())) == 3
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())
    assert all(value not in result.stdout + result.stderr for value in files.values())

    stack.calls.clear()
    assert_success(stack, stack.run(**inputs))
    assert not any(path.startswith("/api/platform") for _, path, *_ in stack.calls)
    assert {
        path.name: path.read_text().strip() for path in directory.iterdir()
    } == files

    for filename in ("account-acme-admin.password", "account-alice.password"):
        (directory / filename).unlink()
    stack.calls.clear()
    result = stack.run(
        prompt_answers={
            "TEST_ADMIN_PASSWORD": files["account-acme-admin.password"],
            "TEST_MEMBER_PASSWORD": files["account-alice.password"],
        },
        **inputs,
    )
    assert_success(stack, result)
    assert "Existing password for acme-admin" in result.stderr
    assert "Existing password for alice" in result.stderr
    assert not any(path.startswith("/api/platform") for _, path, *_ in stack.calls)
    assert {path.name for path in directory.iterdir()} == {
        "platform-platform_admin.password"
    }


@pytest.mark.parametrize("stack", ["localhost"], indirect=True)
def test_bash_onboards_real_auth_and_reuses_without_platform_login(stack):
    from qwenpaw_data_auth.database import get_db, query_one

    assert_success(stack, stack.run())
    first_writes = [
        (m, p)
        for m, p, *_ in stack.calls
        if m in {"PUT", "POST"} and p.startswith("/api/preferences")
    ]
    assert len(stack.active) == 2 and stack.chats == 2 and not stack.sources
    assert len({uid for uid, wid in stack.configs}) == 2
    original_providers = dict(stack.providers)
    stack.calls.clear()
    assert_success(stack, stack.run(**(model_env_empty() | {"EMBED_DIM": "unused"})))
    assert sum(m == "POST" and p == "/api/auth/login" for m, p, *_ in stack.calls) == 2
    assert stack.providers == original_providers
    assert not any(p.startswith("/api/platform") for _, p, *_ in stack.calls)
    assert not any(
        m in {"PUT", "POST"} and p.startswith("/api/preferences")
        for m, p, *_ in stack.calls
    )
    assert first_writes
    with get_db(stack.db) as conn:
        assert query_one(conn, "SELECT count(*) AS n FROM tenants")["n"] == 1
        assert query_one(conn, "SELECT count(*) AS n FROM users")["n"] == 2
    admin_file = stack.tmp / ".env.chat-setup/credentials/account-acme-admin.password"
    admin_file.write_text("Incorrect123!\n")
    result = stack.run()
    assert result.returncode != 0 and "acme-admin: cannot log in" in result.stderr
    assert "DP_CHAT_READY=0" in (stack.tmp / ".env.chat-setup/state.env").read_text()
    assert not list(stack.tmp.glob("qwenpaw-data-chat-setup.*"))


def configure_data(stack, workbook=True):
    config = {
        "datasource": {
            "host": "db.test",
            "database": "database",
            "user": "user",
            "password": 'password"$',
            "name": 'sales "east" & west',
        }
    }
    if workbook:
        path = stack.tmp / 'work;book "one".xlsx'
        path.write_bytes(b"selected-workbook")
        config["workbook"] = path.name
    (stack.tmp / ".env.chat-setup.json").write_text(json.dumps(config))


def test_bash_imports_selected_file_once_and_does_not_duplicate_auto_weave(stack):
    configure_data(stack)
    assert_success(stack, stack.run())
    assert unquote(stack.imports[0][0]) == 'work;book "one".xlsx'
    assert stack.imports[0][1] == b"selected-workbook"
    assert len(stack.imports) == 1 and len(stack.sync) == 2
    assert not any(p.endswith("/weave-task/submit") for _, p, *_ in stack.calls)
    assert all(count == 2 for count in stack.polls.values())
    stack.calls.clear()
    assert_success(stack, stack.run())
    assert len(stack.imports) == 1
    assert not any(
        m == "POST"
        and p in {"/api/semantic-config/weave-task/submit", "/api/v1/mg/userspace/sync"}
        for m, p, *_ in stack.calls
    )
    assert not any(
        m == "PUT" and p.endswith("/explore-scope") for m, p, *_ in stack.calls
    )
    assert not any(
        m == "POST" and p == "/api/semantic-config/datasource"
        for m, p, *_ in stack.calls
    )


@pytest.mark.parametrize("previous", ["RUNNING", "FAILED"])
def test_existing_weave_is_resumed_or_retried(stack, previous):
    configure_data(stack)
    assert_success(stack, stack.run())
    task = next(iter(stack.tasks))
    if previous == "RUNNING":
        stack.polls[task] = 0
    else:
        # The list reports the failed old task; its replacement completes normally.
        stack.failed_task = task
    stack.calls.clear()
    assert_success(stack, stack.run())
    assert len(stack.imports) == 1
    submits = [
        p
        for m, p, *_ in stack.calls
        if m == "POST" and p.endswith("/weave-task/submit")
    ]
    assert bool(submits) == (previous == "FAILED")


def test_bash_host_without_workbook_only_creates_connection(stack):
    configure_data(stack, workbook=False)
    assert_success(stack, stack.run())
    assert stack.sources and not stack.imports and not stack.sync
    assert not any(p.endswith("/explore-scope") for _, p, *_ in stack.calls)


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("import_error", "Invalid workbook"),
        ("conflict", "MG conflicts"),
        ("provision_error", "provisioning failed"),
        ("weave", "DEGRADED"),
    ],
)
def test_bash_stops_at_failed_stage_and_removes_cookies(stack, failure, expected):
    if failure == "weave":
        stack.weave_status = "DEGRADED"
    else:
        setattr(stack, failure, True)
    configure_data(stack)
    result = stack.run()
    assert result.returncode != 0 and expected in result.stderr, (
        result.stdout + result.stderr
    )
    assert stack.chats == 0
    assert not list(stack.tmp.glob("qwenpaw-data-chat-setup.*"))
    if failure == "import_error":
        assert not stack.tasks


def test_bash_wait_has_timeout_without_replaying_write(tmp_path):
    script = """source "$1"
cs_phase=test; cs_auth=http://unused; cs_sid=0
cs_api() { cs_body='{"status":"RUNNING"}'; }
cs_wait_weave task
"""
    result = subprocess.run(
        ["bash", "-c", script, "_", str(ROOT / "scripts/setup_chat.sh")],
        env=os.environ | {"QWENPAW_DATA_SETUP_WEAVE_TIMEOUT": "0"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and "timed out" in result.stderr


def test_datasource_connection_failure_stops_before_chat(stack):
    stack.connections = False
    configure_data(stack, workbook=False)
    result = stack.run()
    assert result.returncode != 0 and "connection test failed" in result.stderr
    assert "failed at datasource" in result.stderr
    assert stack.sources and not stack.chats
    assert "DP_CHAT_READY=0" in (stack.tmp / ".env.chat-setup/state.env").read_text()
    assert not list(stack.tmp.glob("qwenpaw-data-chat-setup.*"))


def test_deployment_requires_explicit_accounts_before_any_api_call(tmp_path):
    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; cs_root=$PWD; cs_env_dir=$PWD; cs_config_file=$PWD/.env.chat-setup.json; cs_inputs',
            "_",
            str(ROOT / "scripts/setup_chat.sh"),
        ],
        env=os.environ | {"QWENPAW_DATA_ENV": "production"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and "accounts is required" in result.stderr


def model_env_empty():
    return dict.fromkeys(
        (
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
            "LLM_MODEL",
            "EMBED_OPENAI_API_KEY",
            "EMBED_OPENAI_BASE_URL",
            "EMBED_MODEL",
            "EMBED_DIM",
            "QWENPAW_DATA_MODEL_API_KEY",
            "QWENPAW_DATA_MODEL_BASE_URL",
            "QWENPAW_DATA_MODEL_NAME",
            "QWENPAW_DATA_MODEL_PROVIDER",
        ),
        "",
    )


def test_missing_embedding_requires_its_env_and_preserves_existing_models(stack):
    assert_success(stack, stack.run())
    scoped = next(iter(stack.configs))
    del stack.configs[scoped]["embed"]
    providers = dict(stack.providers)
    result = stack.run(**(model_env_empty() | {"EMBED_DIM": "invalid"}))
    assert (
        result.returncode != 0
        and "embedding: incomplete model settings" in result.stderr
    )
    assert stack.providers == providers and stack.chats == 2
    result = stack.run(
        EMBED_DIM="2048",
        EMBED_OPENAI_BASE_URL="https://wrong.test/v1",
        EMBED_OPENAI_API_KEY="wrong-key",
    )
    assert result.returncode != 0 and "embedding space differs" in result.stderr
    assert stack.spaces[scoped[1]]["dim"] == 1024
    assert "embed" not in stack.configs[scoped]
    assert stack.providers == providers
    assert_success(stack, stack.run(OPENAI_API_KEY=""))
    assert stack.providers == providers and "embed" in stack.configs[scoped]
    del stack.configs[scoped]["embed"]
    provider = stack.configs[scoped]["llm"]["provider_id"]
    stack.providers[scoped[0], provider] = {
        **stack.providers[scoped[0], provider],
        "api_key": "",
    }
    assert_success(stack, stack.run(OPENAI_API_KEY="replacement-key"))
    assert stack.providers[scoped[0], provider]["api_key"] == "replacement-key"


def test_new_channels_with_different_keys_do_not_silently_share_provider(stack):
    (stack.tmp / ".env.chat-setup.json").write_text(
        json.dumps({"models": {"embedding": {"api_key": "separate-embedding-key"}}})
    )
    assert_success(stack, stack.run())
    for (uid, _), config in stack.configs.items():
        assert config["llm"]["provider_id"] != config["embed"]["provider_id"]
        assert (
            stack.providers[uid, config["embed"]["provider_id"]]["api_key"]
            == "separate-embedding-key"
        )


def test_empty_accounts_cannot_report_chat_ready(stack):
    path = stack.tmp / ".env.chat-setup.json"
    path.write_text(json.dumps({"accounts": {"tenants": [], "users": []}}))
    result = stack.run()
    assert result.returncode != 0 and "Accounts must contain" in result.stderr
    assert not stack.calls and not stack.chats
    assert "DP_CHAT_READY=0" in (stack.tmp / ".env.chat-setup/state.env").read_text()

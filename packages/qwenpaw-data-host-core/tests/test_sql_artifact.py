from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from qwenpaw_data.host.core.db.engine import get_session_factory
from qwenpaw_data.host.core.integrations.cm.sql_artifact import (
    SqlArtifactError,
    is_execute_sql_tool,
    materialize_execute_sql_result,
)
from qwenpaw_data.host.core.settings import clear_settings


def _sql_result(*, download_url: str | None) -> str:
    payload: dict = {
        "exec_status": "success",
        "sql": "select 1",
        "rows": [[1]],
        "preview_row_count": 1,
        "truncated": False,
        "total_row_count": 1,
    }
    if download_url is not None:
        payload["download_url"] = download_url
    return json.dumps(payload)


def _handler(
    captured: list[httpx.Request], *, status: int = 200, body: bytes = b"day,dau\n"
):
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status, content=body)

    return handler


@pytest.mark.asyncio
async def test_materialize_writes_csv_and_rewrites_to_file_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "secret")
    clear_settings()
    captured: list[httpx.Request] = []
    artifact_dir = tmp_path / "artifacts" / "ses_1"

    rewritten = await materialize_execute_sql_result(
        _sql_result(
            download_url="/api/v1/cm/downloads/54814f8b.csv",
        ),
        artifact_dir=artifact_dir,
        access_token="tok-user",
        transport=httpx.MockTransport(_handler(captured)),
    )

    payload = json.loads(rewritten)
    dest = artifact_dir / "data" / "raw" / "54814f8b.csv"
    assert dest.read_bytes() == b"day,dau\n"
    assert payload["file_path"] == str(dest.resolve())
    assert "download_url" not in payload
    assert payload["rows"] == [[1]]
    assert len(captured) == 1
    assert str(captured[0].url) == "http://cm.test/api/v1/cm/downloads/54814f8b.csv"
    assert captured[0].headers["authorization"] == "Bearer tok-user"


@pytest.mark.asyncio
async def test_materialize_skips_when_no_download_url(tmp_path: Path) -> None:
    original = _sql_result(download_url=None)
    rewritten = await materialize_execute_sql_result(
        original,
        artifact_dir=tmp_path,
        access_token="tok-user",
    )
    assert rewritten == original


@pytest.mark.asyncio
async def test_materialize_fails_without_token_when_auth_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "secret")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_MODE", "multi-user")
    with pytest.raises(SqlArtifactError, match="access_token"):
        await materialize_execute_sql_result(
            _sql_result(
                download_url="http://127.0.0.1:8080/api/v1/cm/downloads/54814f8b.csv",
            ),
            artifact_dir=tmp_path,
            access_token=None,
        )


@pytest.mark.asyncio
async def test_materialize_fails_on_http_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "secret")
    clear_settings()
    with pytest.raises(SqlArtifactError, match="HTTP 401"):
        await materialize_execute_sql_result(
            _sql_result(
                download_url="http://127.0.0.1:8080/api/v1/cm/downloads/54814f8b.csv",
            ),
            artifact_dir=tmp_path,
            access_token="tok-user",
            transport=httpx.MockTransport(_handler([], status=401, body=b"no")),
        )


def _agent(*, access_token: str = "tok"):
    from types import SimpleNamespace

    return SimpleNamespace(_request_context={"access_token": access_token})


def test_is_execute_sql_tool_requires_cm_name() -> None:
    assert is_execute_sql_tool("mcp__context-manager__execute_read")
    assert not is_execute_sql_tool("mcp__context-manager__execute_sql")
    assert not is_execute_sql_tool("mcp__other__execute_read")
    assert not is_execute_sql_tool("mcp__context-manager__search_context")


@pytest.mark.asyncio
async def test_middleware_skips_non_sql_tools(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    from qwenpaw_data.host.core.agent.middleware.sql_artifact import SqlArtifactMiddleware
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.paths import UserAgentPaths
    from qwenpaw_data.host.core.runtime.context import RunContext

    response = ToolResponse(content=[TextBlock(text='{"download_url":"x"}')])

    async def next_handler(**_kwargs):
        yield response

    middleware = SqlArtifactMiddleware(
        run_context=RunContext(
            session_id="ses_1",
            chat_id="chat-a",
            workspace=SimpleNamespace(),
            paths=UserAgentPaths(
                home=tmp_path,
                tenant_id="tenant-a",
                workspace_id="ws-a",
                user_id="user-a",
                agent_id="agent-a",
                session_id="ses_1",
            ),
            user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
            identity=Identity(
                user_id="user-a", tenant_id="tenant-a", workspace_id="ws-a"
            ),
            session_factory=get_session_factory(),
        )
    )
    items = [
        item
        async for item in middleware.on_acting(
            _agent(),  # type: ignore[arg-type]
            {"tool_call": SimpleNamespace(name="mcp__context-manager__search_context")},
            next_handler,
        )
    ]
    assert items[0] is response
    assert response.content[0].text == '{"download_url":"x"}'


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["local", "docker", "fc-sandbox"])
async def test_middleware_rewrites_execute_sql_tool_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
) -> None:
    from types import SimpleNamespace

    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    from qwenpaw_data.host.core.agent.middleware.sql_artifact import SqlArtifactMiddleware
    from qwenpaw_data.host.core.domain.identity import Identity
    from qwenpaw_data.host.core.paths import UserAgentPaths
    from qwenpaw_data.host.core.runtime.context import RunContext

    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    monkeypatch.setenv("QWENPAW_DATA_AUTH_SECRET", "secret")
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
    monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", "https://gateway.example.test")
    clear_settings()
    captured: list[httpx.Request] = []
    paths = UserAgentPaths(
        home=tmp_path,
        tenant_id="tenant-a",
        workspace_id="ws-a",
        user_id="user-a",
        agent_id="agent-a",
        session_id="ses_1",
    )
    workdir = {
        "local": str(paths.workspace),
        "docker": "/workspace",
        "fc-sandbox": "/custom-fc/workspace",
    }[backend]
    run_context = RunContext(
        session_id="ses_1",
        chat_id="chat-a",
        workspace=SimpleNamespace(workdir=workdir),
        paths=paths,
        user_runtime_config=SimpleNamespace(),  # type: ignore[arg-type]
        identity=Identity(user_id="user-a", tenant_id="tenant-a", workspace_id="ws-a"),
        session_factory=get_session_factory(),
    )
    agent = _agent(access_token="tok-user")
    response = ToolResponse(
        content=[
            TextBlock(
                text=_sql_result(
                    download_url="/api/v1/cm/downloads/54814f8b.csv",
                ),
            ),
        ],
    )

    async def next_handler(**_kwargs):
        yield response

    from qwenpaw_data.host.core.agent.middleware import sql_artifact as mw_mod

    async def _materialize(result_text: str, **kwargs):
        return await materialize_execute_sql_result(
            result_text,
            **kwargs,
            transport=httpx.MockTransport(_handler(captured)),
        )

    middleware = SqlArtifactMiddleware(run_context=run_context)
    monkeypatch.setattr(mw_mod, "materialize_execute_sql_result", _materialize)

    items = [
        item
        async for item in middleware.on_acting(
            agent,  # type: ignore[arg-type]
            {"tool_call": SimpleNamespace(name="mcp__context-manager__execute_read")},
            next_handler,
        )
    ]

    dest = paths.artifact_dir / "data" / "raw" / "54814f8b.csv"
    payload = json.loads(items[0].content[0].text)
    assert dest.read_bytes() == b"day,dau\n"
    expected = f"{workdir}/artifacts/ses_1/data/raw/54814f8b.csv"
    assert payload["file_path"] == expected
    assert "download_url" not in payload
    assert str(captured[0].url) == "http://cm.test/api/v1/cm/downloads/54814f8b.csv"

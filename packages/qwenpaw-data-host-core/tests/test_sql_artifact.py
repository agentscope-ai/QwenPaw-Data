# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from qwenpaw_data.host.core.agent.middleware import sql_artifact as mw_mod
from qwenpaw_data.host.core.agent.middleware.sql_artifact import SqlArtifactMiddleware
from qwenpaw_data.host.core.cm_sql_artifact import (
    SqlArtifactError,
    is_execute_sql_tool,
    materialize_execute_sql_result,
)


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
    captured: list[httpx.Request],
    *,
    status: int = 200,
    body: bytes = b"day,dau\n",
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
    captured: list[httpx.Request] = []
    artifact_dir = tmp_path / "artifacts" / "ses_1"

    rewritten = await materialize_execute_sql_result(
        _sql_result(download_url="/api/v1/cm/downloads/54814f8b.csv"),
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
async def test_materialize_uses_env_token_when_no_access_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    monkeypatch.setenv("QWENPAW_DATA_CLIENT_API_TOKEN", "tok-env")
    captured: list[httpx.Request] = []

    await materialize_execute_sql_result(
        _sql_result(download_url="/api/v1/cm/downloads/54814f8b.csv"),
        artifact_dir=tmp_path,
        transport=httpx.MockTransport(_handler(captured)),
    )

    assert captured[0].headers["authorization"] == "Bearer tok-env"


@pytest.mark.asyncio
async def test_materialize_omits_auth_header_without_any_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    monkeypatch.delenv("QWENPAW_DATA_CLIENT_API_TOKEN", raising=False)
    monkeypatch.delenv("QWENPAW_DATA_API_TOKEN", raising=False)
    captured: list[httpx.Request] = []

    await materialize_execute_sql_result(
        _sql_result(download_url="/api/v1/cm/downloads/54814f8b.csv"),
        artifact_dir=tmp_path,
        transport=httpx.MockTransport(_handler(captured)),
    )

    assert "authorization" not in captured[0].headers


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
async def test_materialize_skips_non_json_text(tmp_path: Path) -> None:
    rewritten = await materialize_execute_sql_result(
        "not json",
        artifact_dir=tmp_path,
    )
    assert rewritten == "not json"


@pytest.mark.asyncio
async def test_materialize_rejects_foreign_download_url(tmp_path: Path) -> None:
    with pytest.raises(SqlArtifactError, match="not a CM CSV download"):
        await materialize_execute_sql_result(
            _sql_result(download_url="http://evil.test/steal.csv"),
            artifact_dir=tmp_path,
            access_token="tok-user",
        )


@pytest.mark.asyncio
async def test_materialize_fails_on_http_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    with pytest.raises(SqlArtifactError, match="HTTP 401"):
        await materialize_execute_sql_result(
            _sql_result(
                download_url="http://127.0.0.1:8080/api/v1/cm/downloads/54814f8b.csv",
            ),
            artifact_dir=tmp_path,
            access_token="tok-user",
            transport=httpx.MockTransport(_handler([], status=401, body=b"no")),
        )


def test_is_execute_sql_tool_requires_cm_prefix() -> None:
    prefixes = {"mcp__context-manager__"}
    assert is_execute_sql_tool("mcp__context-manager__execute_sql", prefixes)
    assert not is_execute_sql_tool("mcp__other__execute_sql", prefixes)
    assert not is_execute_sql_tool("mcp__context-manager__search_context", prefixes)
    assert not is_execute_sql_tool("", prefixes)


def _agent() -> SimpleNamespace:
    return SimpleNamespace(
        _request_context={"access_token": "tok-user"},
        _cm_mcp_tool_prefixes={"mcp__context-manager__"},
    )


@pytest.mark.asyncio
async def test_middleware_skips_non_sql_tools(tmp_path: Path) -> None:
    response = ToolResponse(
        content=[TextBlock(type="text", text='{"download_url":"x"}')],
    )

    async def next_handler(**_kwargs):
        yield response

    middleware = SqlArtifactMiddleware(artifact_dir=tmp_path)
    items = [
        item
        async for item in middleware.on_acting(
            _agent(),  # type: ignore[arg-type]
            {
                "tool_call": SimpleNamespace(
                    name="mcp__context-manager__search_context",
                ),
            },
            next_handler,
        )
    ]
    assert items[0] is response
    assert response.content[0].text == '{"download_url":"x"}'


@pytest.mark.asyncio
async def test_middleware_rewrites_execute_sql_tool_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://cm.test")
    captured: list[httpx.Request] = []

    async def _materialize(result_text: str, **kwargs):
        return await materialize_execute_sql_result(
            result_text,
            **kwargs,
            transport=httpx.MockTransport(_handler(captured)),
        )

    monkeypatch.setattr(mw_mod, "materialize_execute_sql_result", _materialize)

    response = ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=_sql_result(download_url="/api/v1/cm/downloads/54814f8b.csv"),
            ),
        ],
    )

    async def next_handler(**_kwargs):
        yield response

    middleware = SqlArtifactMiddleware(artifact_dir=tmp_path)
    items = [
        item
        async for item in middleware.on_acting(
            _agent(),  # type: ignore[arg-type]
            {
                "tool_call": SimpleNamespace(
                    name="mcp__context-manager__execute_sql",
                ),
            },
            next_handler,
        )
    ]

    payload = json.loads(items[0].content[0].text)
    dest = tmp_path / "data" / "raw" / "54814f8b.csv"
    assert dest.read_bytes() == b"day,dau\n"
    assert payload["file_path"] == str(dest.resolve())
    assert "download_url" not in payload
    assert captured[0].headers["authorization"] == "Bearer tok-user"

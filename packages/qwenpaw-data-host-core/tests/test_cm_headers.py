from __future__ import annotations

import pytest
from agentscope.mcp import MCPClient

from qwenpaw_data.host.core.integrations.cm import set_cm_mcp_headers


def _cm_client(*, headers: dict[str, str] | None = None) -> MCPClient:
    return MCPClient(
        name="context-manager",
        is_stateful=False,
        mcp_config={
            "type": "http_mcp",
            "url": "http://example.com/mcp/v1/cm",
            "headers": dict(headers or {}),
        },
    )


@pytest.mark.asyncio
async def test_set_cm_mcp_headers_writes_runtime_headers() -> None:
    client = _cm_client(headers={"X-Keep": "yes"})

    await set_cm_mcp_headers(
        client,
        "tok",
        datasource_id="postgresql-a",
        dataset_names=["orders", "users"],
        session_id="ses_1",
        chat_id="chat_1",
    )

    assert client.mcp_config.headers == {"X-Keep": "yes"}
    assert client._runtime_headers == {
        "Authorization": "Bearer tok",
        "datasource_id": "postgresql-a",
        "dataset_name": "orders,users",
        "session_id": "ses_1",
        "chat_id": "chat_1",
    }


@pytest.mark.asyncio
async def test_set_cm_mcp_headers_omits_empty_scope() -> None:
    client = _cm_client()

    await set_cm_mcp_headers(client, "tok", datasource_id="", dataset_names=[])

    assert client.mcp_config.headers == {}
    assert client._runtime_headers == {"Authorization": "Bearer tok"}


@pytest.mark.asyncio
async def test_set_cm_mcp_headers_replaces_stale_runtime_headers() -> None:
    client = _cm_client()
    await set_cm_mcp_headers(
        client,
        "old",
        datasource_id="old-ds",
        dataset_names=["stale"],
        session_id="ses_old",
        chat_id="chat_old",
    )

    await set_cm_mcp_headers(client, "fresh", datasource_id="postgresql-a")

    assert client.mcp_config.headers == {}
    assert client._runtime_headers == {
        "Authorization": "Bearer fresh",
        "datasource_id": "postgresql-a",
    }

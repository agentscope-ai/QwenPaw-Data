from __future__ import annotations

import json
from functools import partial

import httpx
import pytest

from qwenpaw_data.host.core.integrations.cm import (
    CMClientError,
    CMLockedError,
    ContextManagerClient,
    resolve_cm_base_url,
)
from qwenpaw_data.host.core.settings import CmSettings, clear_settings, get_settings
from qwenpaw_data.host.core.integrations.cm import client as cm_client


@pytest.mark.asyncio
async def test_fetch_global_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "workspace_id": "ws-a",
        "user_id": "user-a",
        "profile": None,
        "type_descriptions": {},
        "principles": [],
        "preferences": [],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://localhost:8765/api/v1/ug/me/global-memories"
        assert request.headers["Authorization"] == "Bearer user-token"
        assert request.content == b""
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        cm_client, "resolve_cm_base_url", lambda: "http://localhost:8765"
    )
    monkeypatch.setattr(
        cm_client.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    assert (
        await cm_client.fetch_global_memories(
            "user-token",
            chat_id="chat-a",
        )
        == payload
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "no_token",
        "timeout",
    ],
)
async def test_fetch_global_memories_logs_and_skips_failures(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise httpx.ReadTimeout("CM timed out", request=request)

    monkeypatch.setattr(
        cm_client.httpx,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    assert (
        await cm_client.fetch_global_memories(
            None if case == "no_token" else "secret-user-token",
            chat_id="chat-a",
        )
        is None
    )
    assert len(requests) == (0 if case == "no_token" else 1)
    assert len(caplog.records) == 1
    assert caplog.records[0].name == cm_client.__name__
    assert "Global memory skipped: chat=chat-a" in caplog.text
    assert "secret-user-token" not in caplog.text


def _page(records: list[dict], *, total: int, page: int, size: int = 2) -> dict:
    return {
        "records": records,
        "total": total,
        "page": page,
        "size": size,
    }


def test_resolve_cm_base_url_uses_environment_then_local_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QWENPAW_DATA_CM_BASE_URL", raising=False)
    clear_settings()
    assert resolve_cm_base_url() == CmSettings.DEFAULT_BASE_URL

    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "https://cm.example.test/")
    clear_settings()
    assert resolve_cm_base_url() == "https://cm.example.test"


def test_cm_frontend_url_uses_vite_env_then_local_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VITE_CONTEXT_FRONTEND_URL", raising=False)
    clear_settings()
    assert (
        get_settings().cm.client_cm_frontend_url() == CmSettings.DEFAULT_CM_FRONTEND_URL
    )
    assert get_settings().cm.client_cm_frontend_url() == "http://localhost:3000"

    monkeypatch.setenv("VITE_CONTEXT_FRONTEND_URL", "/")
    clear_settings()
    assert get_settings().cm.client_cm_frontend_url() == ""

    monkeypatch.setenv("VITE_CONTEXT_FRONTEND_URL", "https://context.example/")
    clear_settings()
    assert get_settings().cm.client_cm_frontend_url() == "https://context.example"


@pytest.mark.parametrize("value", [None, "", "   "])
def test_fc_requires_sandbox_cm_url(monkeypatch, value) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:8080")
    if value is None:
        monkeypatch.delenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", value)
    clear_settings()
    with pytest.raises(ValueError, match="QWENPAW_DATA_CM_SANDBOX_BASE_URL"):
        get_settings()


@pytest.mark.parametrize("backend", ["local", "docker"])
def test_non_fc_does_not_require_sandbox_cm_url(monkeypatch, backend) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", backend)
    monkeypatch.delenv("QWENPAW_DATA_CM_SANDBOX_BASE_URL", raising=False)
    clear_settings()
    assert get_settings().cm.sandbox_base_url is None


def test_fc_host_datasources_still_use_host_cm_url(monkeypatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_WORKSPACE_BACKEND", "fc-sandbox")
    monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", "http://127.0.0.1:8080")
    monkeypatch.setenv(
        "QWENPAW_DATA_CM_SANDBOX_BASE_URL",
        "  https://gateway.example.test/qwenpaw_data/  ",
    )
    clear_settings()
    assert get_settings().cm.sandbox_mcp_url() == (
        "https://gateway.example.test/qwenpaw_data/mcp/v1/cm"
    )
    assert resolve_cm_base_url() == "http://127.0.0.1:8080"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "http://127.0.0.1:8080/api/semantic-config/datasource?page=1&size=100"
        )
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    result = ContextManagerClient(
        transport=httpx.MockTransport(handler)
    ).list_datasources()
    assert result.total == 0


@pytest.mark.parametrize(
    "configured_url,expected_url",
    [
        (
            None,
            f"{CmSettings.DEFAULT_BASE_URL}/api/semantic-config/datasource?page=1&size=100",
        ),
        (
            "https://cm.example.test/root/",
            "https://cm.example.test/root/api/semantic-config/datasource?page=1&size=100",
        ),
    ],
)
def test_client_uses_environment_or_local_default(
    monkeypatch: pytest.MonkeyPatch,
    configured_url: str | None,
    expected_url: str,
) -> None:
    if configured_url is None:
        monkeypatch.delenv("QWENPAW_DATA_CM_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("QWENPAW_DATA_CM_BASE_URL", configured_url)
    clear_settings()

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    ContextManagerClient(transport=httpx.MockTransport(handler)).list_datasources()

    assert str(requests[0].url) == expected_url


def test_list_datasources_fetches_every_page_in_server_order() -> None:
    requests: list[httpx.Request] = []
    pages = {
        1: _page(
            [
                {
                    "datasource_id": "postgresql-a",
                    "datasource_name": "Primary",
                    "datasource_type": "postgresql",
                    "config": {"password": "secret-a"},
                    "id": 101,
                },
                {
                    "datasource_id": "mysql-b",
                    "datasource_name": "Replica",
                    "datasource_type": "mysql",
                    "config": {"password": "secret-b"},
                },
            ],
            total=3,
            page=1,
        ),
        2: _page(
            [
                {
                    "datasource_id": "odps-c",
                    "datasource_name": "Warehouse",
                    "datasource_type": "odps",
                    "config": {"access_key_secret": "secret-c"},
                },
            ],
            total=3,
            page=2,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[page])

    result = ContextManagerClient(
        base_url="https://cm.example.test/",
        page_size=2,
        transport=httpx.MockTransport(handler),
    ).list_datasources()

    assert [item.datasource_id for item in result.items] == [
        "postgresql-a",
        "mysql-b",
        "odps-c",
    ]
    assert result.total == 3
    assert [request.url.path for request in requests] == [
        "/api/semantic-config/datasource",
        "/api/semantic-config/datasource",
    ]
    assert [request.url.params["size"] for request in requests] == ["2", "2"]


def test_list_datasources_accepts_an_empty_page() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=_page([], total=0, page=1)),
    )

    result = ContextManagerClient(transport=transport).list_datasources()

    assert result.items == []
    assert result.total == 0


def test_list_datasources_does_not_fall_back_to_legacy_fields() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json=_page(
                [
                    {
                        "id": 42,
                        "datasource_code": "postgresql-legacy",
                        "datasource_name": "Legacy",
                        "datasource_type": "postgresql",
                        "config": {},
                    },
                ],
                total=1,
                page=1,
            ),
        ),
    )

    with pytest.raises(CMClientError, match="invalid datasource_id"):
        ContextManagerClient(transport=transport).list_datasources()


@pytest.mark.parametrize(
    "payload,error",
    [
        ({"records": [], "total": 0, "page": 1}, "invalid size"),
        (_page([], total=0, page=2), "unexpected page"),
        (_page([], total=1, page=1), "ended before total"),
        (_page([{"datasource_id": "a"}], total=0, page=1), "more records"),
    ],
)
def test_list_datasources_rejects_invalid_pagination(payload: dict, error: str) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))

    with pytest.raises(CMClientError, match=error):
        ContextManagerClient(transport=transport).list_datasources()


def test_list_datasources_rejects_non_json_without_echoing_body() -> None:
    secret = "must-not-leak"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=f"not-json {secret}"),
    )

    with pytest.raises(CMClientError) as exc_info:
        ContextManagerClient(transport=transport).list_datasources()

    assert secret not in str(exc_info.value)


def test_list_datasources_rejects_non_object_json() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=[]))

    with pytest.raises(CMClientError, match="not an object"):
        ContextManagerClient(transport=transport).list_datasources()


def test_list_datasources_rejects_http_error_without_echoing_body() -> None:
    secret = "must-not-leak"
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            500,
            content=json.dumps({"config": {"password": secret}}),
        ),
    )

    with pytest.raises(CMClientError, match="HTTP 500") as exc_info:
        ContextManagerClient(transport=transport).list_datasources()

    assert secret not in str(exc_info.value)


def test_list_datasources_raises_locked_on_http_423() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(423))

    with pytest.raises(CMLockedError, match="图谱正在重建"):
        ContextManagerClient(transport=transport).list_datasources()


def test_get_active_datasource_id_returns_current_selection() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={"datasource_id": "postgresql-a"},
        ),
    )

    result = ContextManagerClient(
        base_url="https://cm.example.test/",
        transport=transport,
        access_token="token-1",
    ).get_active_datasource_id()

    assert result == "postgresql-a"


def test_get_active_datasource_id_treats_empty_or_missing_as_none() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"datasource_id": ""}),
    )

    assert ContextManagerClient(transport=transport).get_active_datasource_id() is None


def test_get_active_datasource_id_is_best_effort_on_http_error() -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(500, json={"error": "nope"})
    )

    assert ContextManagerClient(transport=transport).get_active_datasource_id() is None


def test_list_datasources_retries_once_after_timeout() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("upstream timeout", request=request)
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    result = ContextManagerClient(
        transport=httpx.MockTransport(handler)
    ).list_datasources()

    assert result.total == 0
    assert calls["n"] == 2


def test_list_datasources_wraps_timeout_without_request_details() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timeout", request=request)

    transport = httpx.MockTransport(timeout)

    with pytest.raises(CMClientError, match="timed out on page 1"):
        ContextManagerClient(transport=transport).list_datasources()


def test_list_datasources_wraps_connection_failure_without_request_details() -> None:
    def connection_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not connect to secret-host", request=request)

    transport = httpx.MockTransport(connection_failure)

    with pytest.raises(CMClientError, match="request failed on page 1") as exc_info:
        ContextManagerClient(transport=transport).list_datasources()

    assert "secret-host" not in str(exc_info.value)


@pytest.mark.parametrize(
    "record,error",
    [
        ({"datasource_id": "valid", "config": "secret"}, "invalid config"),
        ({"datasource_id": "valid", "datasource_name": 1}, "invalid datasource_name"),
        ({"datasource_id": "valid", "datasource_type": 1}, "invalid datasource_type"),
    ],
)
def test_list_datasources_rejects_invalid_item_fields(
    record: dict,
    error: str,
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=_page([record], total=1, page=1)),
    )

    with pytest.raises(CMClientError, match=error):
        ContextManagerClient(transport=transport).list_datasources()


def test_list_datasets_requires_datasource_id() -> None:
    with pytest.raises(ValueError, match="datasource_id is required"):
        ContextManagerClient().list_datasets("")


def test_list_datasets_queries_datasource_id_and_skips_source_sql() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_page(
                [
                    {
                        "dataset_id": 11,
                        "dataset_name": " orders ",
                        "description": "订单",
                        "source": "SELECT password FROM secrets",
                        "datasource_id": "postgresql-a",
                    },
                ],
                total=1,
                page=1,
                size=100,
            ),
        )

    result = ContextManagerClient(
        base_url="https://cm.example.test/",
        transport=httpx.MockTransport(handler),
        access_token="token-1",
    ).list_datasets("postgresql-a")

    assert [item.dataset_name for item in result.items] == ["orders"]
    assert result.items[0].dataset_id == 11
    assert result.items[0].description == "订单"
    assert not hasattr(result.items[0], "source")
    assert not hasattr(result.items[0], "domain_name")
    assert requests[0].url.path == "/api/semantic-config/dataset-meta"
    assert requests[0].url.params["datasource_id"] == "postgresql-a"
    assert requests[0].headers["Authorization"] == "Bearer token-1"
    assert "dataset_name" not in requests[0].headers
    assert "datasource_id" not in requests[0].headers


def test_list_datasets_fetches_every_page_in_server_order() -> None:
    requests: list[httpx.Request] = []
    pages = {
        1: _page(
            [
                {"dataset_id": 1, "dataset_name": "orders"},
                {"dataset_id": 2, "dataset_name": "users"},
            ],
            total=3,
            page=1,
        ),
        2: _page(
            [{"dataset_id": 3, "dataset_name": "payments"}],
            total=3,
            page=2,
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json=pages[page])

    result = ContextManagerClient(
        base_url="https://cm.example.test/",
        page_size=2,
        transport=httpx.MockTransport(handler),
    ).list_datasets("postgresql-a")

    assert [item.dataset_name for item in result.items] == [
        "orders",
        "users",
        "payments",
    ]
    assert result.total == 3
    assert [request.url.params["size"] for request in requests] == ["2", "2"]
    assert {request.url.params["datasource_id"] for request in requests} == {
        "postgresql-a",
    }


@pytest.mark.parametrize(
    "record,error",
    [
        ({"dataset_name": "orders"}, "invalid dataset_id"),
        ({"dataset_id": 1, "dataset_name": ""}, "invalid dataset_name"),
        (
            {"dataset_id": 1, "dataset_name": "orders", "description": 1},
            "invalid description",
        ),
        ({"dataset_id": True, "dataset_name": "orders"}, "invalid dataset_id"),
    ],
)
def test_list_datasets_rejects_invalid_item_fields(
    record: dict,
    error: str,
) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, json=_page([record], total=1, page=1)),
    )

    with pytest.raises(CMClientError, match=error):
        ContextManagerClient(transport=transport).list_datasets("postgresql-a")

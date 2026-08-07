from __future__ import annotations

import json

import httpx
import pytest

from datapaw.host.core.cm_client import (
    API_TOKEN_ENV,
    CM_BASE_URL_ENV,
    CMClientError,
    CLIENT_API_TOKEN_ENV,
    ContextManagerClient,
    DEFAULT_CM_BASE_URL,
    resolve_cm_base_url,
)


@pytest.fixture(autouse=True)
def _clear_client_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CLIENT_API_TOKEN_ENV, raising=False)


def _page(records: list[dict], *, total: int, page: int, size: int = 2) -> dict:
    return {
        "records": records,
        "total": total,
        "page": page,
        "size": size,
    }


def test_resolve_cm_base_url_uses_environment_then_local_default() -> None:
    assert resolve_cm_base_url({}) == DEFAULT_CM_BASE_URL
    assert (
        resolve_cm_base_url({CM_BASE_URL_ENV: "https://cm.example.test/"})
        == "https://cm.example.test"
    )


@pytest.mark.parametrize(
    "configured_url,expected_url",
    [
        (None, f"{DEFAULT_CM_BASE_URL}/api/v1/cm/datasources?page=1&size=100"),
        (
            "https://cm.example.test/root/",
            "https://cm.example.test/root/api/v1/cm/datasources?page=1&size=100",
        ),
    ],
)
def test_client_uses_environment_or_local_default(
    monkeypatch: pytest.MonkeyPatch,
    configured_url: str | None,
    expected_url: str,
) -> None:
    if configured_url is None:
        monkeypatch.delenv(CM_BASE_URL_ENV, raising=False)
    else:
        monkeypatch.setenv(CM_BASE_URL_ENV, configured_url)

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    ContextManagerClient(transport=httpx.MockTransport(handler)).list_datasources()

    assert str(requests[0].url) == expected_url


def test_client_sends_api_token_without_logging_or_persisting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, "cli-secret")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    ContextManagerClient(transport=httpx.MockTransport(handler)).list_datasources()
    assert requests[0].headers["Authorization"] == "Bearer cli-secret"


def test_scoped_client_token_takes_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, "server-admin-secret")
    monkeypatch.setenv(CLIENT_API_TOKEN_ENV, "scoped-reader-secret")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    ContextManagerClient(transport=httpx.MockTransport(handler)).list_datasources()
    assert requests[0].headers["Authorization"] == "Bearer scoped-reader-secret"


def test_explicit_client_token_overrides_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_TOKEN_ENV, "environment-secret")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_page([], total=0, page=1, size=100))

    ContextManagerClient(
        api_token="explicit-secret",
        transport=httpx.MockTransport(handler),
    ).list_datasources()
    assert requests[0].headers["Authorization"] == "Bearer explicit-secret"


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
    assert all(item.config is None for item in result.items)
    assert [request.url.path for request in requests] == [
        "/api/v1/cm/datasources",
        "/api/v1/cm/datasources",
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

from __future__ import annotations

import httpx
import pytest

from qwenpaw_data.host.core.semantic_config_client import (
    SemanticConfigClient,
    SemanticConfigClientError,
)


def _client(handler, **kwargs) -> SemanticConfigClient:
    return SemanticConfigClient(
        base_url="http://cm.test",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_request_sends_bearer_token_and_returns_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer token-1"
        assert request.url.path == "/api/semantic-config/datasource/x"
        return httpx.Response(200, json={"datasource_id": "x"})

    client = _client(handler, api_token="token-1")
    assert client.get("/api/semantic-config/datasource/x") == {"datasource_id": "x"}


def test_request_without_token_sends_no_auth_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={})

    assert _client(handler, api_token="").get("/x") == {}


def test_request_drops_none_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"page": "1"}
        return httpx.Response(200, json={})

    _client(handler, api_token="").get("/x", params={"page": 1, "name": None})


def test_error_protocol_message_is_surfaced() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "timestamp": "2026-08-10T00:00:00Z",
                "status": 400,
                "error": "Bad Request",
                "message": "数据源被引用，无法删除",
            },
        )

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="").delete("/x")
    assert "数据源被引用" in str(exc_info.value)
    assert exc_info.value.status_code == 400


def test_forbidden_error_appends_token_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="t").get("/x")
    message = str(exc_info.value)
    assert "forbidden" in message
    assert "QWENPAW_DATA_CLIENT_API_TOKEN" in message


def test_non_json_error_falls_back_to_status_line() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="bad gateway")

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="").get("/x")
    assert "HTTP 502" in str(exc_info.value)


def test_empty_success_body_returns_empty_dict() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    assert _client(handler, api_token="").delete("/x") == {}


def test_list_page_validates_page_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": "nope", "total": 1, "page": 1})

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="").list_page("/x")
    assert "invalid records" in str(exc_info.value)


def test_list_all_paginates_until_total() -> None:
    pages = {
        "1": {"records": [{"id": 1}, {"id": 2}], "total": 3, "page": 1, "size": 2},
        "2": {"records": [{"id": 3}], "total": 3, "page": 2, "size": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params["page"]])

    result = _client(handler, api_token="").list_all("/x", size=2)
    assert result == {"records": [{"id": 1}, {"id": 2}, {"id": 3}], "total": 3}


def test_list_all_detects_total_drift() -> None:
    pages = {
        "1": {"records": [{"id": 1}], "total": 3, "page": 1, "size": 1},
        "2": {"records": [{"id": 2}], "total": 4, "page": 2, "size": 1},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=pages[request.url.params["page"]])

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="").list_all("/x", size=1)
    assert "total changed" in str(exc_info.value)


def test_list_all_detects_truncated_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(
            200,
            json={"records": [], "total": 2, "page": page, "size": 100},
        )

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="").list_all("/x")
    assert "ended before total" in str(exc_info.value)


def test_multipart_upload_posts_file() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert b'filename="config.xlsx"' in request.read()
        return httpx.Response(200, json={"success": True})

    result = _client(handler, api_token="").post(
        "/api/semantic-config/import/excel",
        files={"file": ("config.xlsx", b"payload", "application/octet-stream")},
    )
    assert result == {"success": True}


def test_connection_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(SemanticConfigClientError) as exc_info:
        _client(handler, api_token="").get("/x")
    assert "request failed" in str(exc_info.value)


def test_token_resolution_prefers_client_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("QWENPAW_DATA_CLIENT_API_TOKEN", "scoped")
    monkeypatch.setenv("QWENPAW_DATA_API_TOKEN", "full")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer scoped"
        return httpx.Response(200, json={})

    SemanticConfigClient(
        base_url="http://cm.test",
        transport=httpx.MockTransport(handler),
    ).get("/x")
